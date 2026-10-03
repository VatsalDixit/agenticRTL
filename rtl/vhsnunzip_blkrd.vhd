library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 block reader (SPEC 3.2, block reader part; build step B3).
--
-- Issues one 16-byte block per cycle from CBUF port C. Modes:
--   START  wait for the CSF head (the next chunk's start GA, 32-aligned):
--          bk := start / 16, the next block is first-of-chunk.
--   RUN    issue block bk when its bytes have arrived (16*bk + 16 <= GB,
--          modular) or the chunk end ce is known (CEF head valid). With ce
--          known: nvalid = clamp(ce - 16*bk, 0, 16); the block that reaches
--          ce (16*bk + 16 >= ce) is the chunk's last: go to PAD. A block at or
--          past ce (only after a retarget to ce) is itself the first pad.
--   PAD    emit pad blocks (nvalid = 0) until PAD_BLOCKS (= 3) nvalid = 0
--          blocks follow the last data block, then go to NEXT. Three, not the
--          SPEC's two: when ce is 16-aligned the walker needs window(ce),
--          which WA emits only when the third block after it enters.
--   NEXT   pop CSF and CEF, go to START. With SYNC_EOC (TEST_RETGT) NEXT
--          also waits until the walker has emitted the chunk's EOC (wk_eoc
--          count), so a retarget can never arrive after the pop.
--   retgt  (any mode, highest priority) bk := tgt / 16, epoch += 1, RUN.
--
-- Window credit: every issued block from the third one after reset completes
-- exactly one window in WA (window(b) is emitted when block b+2 enters, also
-- across chunks and retargets: the walker drops / drains the mixed ones), so
-- such a block is issued only while wused < WF_DEPTH, where wused counts the
-- windows completed by issued blocks and not yet popped by the walker (in
-- flight in blkrd / PT1..PT3, or in the WFIFO). The WFIFO never overflows.
--
-- All GA compares are modular (ga_sdiff, 32 b).
--
-- Without SYNC_EOC a retarget never comes after the chunk's pop: the walker
-- retargets only for dist >= 80 from its head window H. The pop waits for
-- wused <= POP_WUSED (4), so when the chunk is popped every issued block is
-- at most H+5 (wused counts all unpopped windows, stale ones included). Pad 3
-- of H's chunk was issued, so ce <= 16*H + 48 and the far target T <= ce
-- gives dist <= 48 (a step). Asserted below (walker EOCs against pops). With
-- WF_DEPTH <= 5 the credit alone guarantees it; deeper WFIFOs (B3c: 8) need
-- the pop condition.
--
--   gb                 GA of the next CBUF beat.
--   csf_* / cef_*      chunk start / end FIFO heads from vhsnunzip_cbuf.
--   retgt_v/retgt_tgt  walker retarget (tgt 16-aligned).
--   wf_pop             the walker popped a WFIFO window (credit return).
--   wk_eoc             the walker emitted an EOC (used with SYNC_EOC only).
--   rowC / lanesC      CBUF port C: all 32 rows = bk(5:1) (registered here);
--                      the block is lanes 16*bk(0) .. 16*bk(0)+15.
--   blk                the block (registered), to vhsnunzip_pt; valid 2
--                      cycles after the issue decision.
entity vhsnunzip_blkrd is
  generic (
    WF_DEPTH    : positive := 4;
    SYNC_EOC    : boolean  := false
  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    gb          : in  ga_t;
    csf_valid   : in  std_logic;
    csf_ga      : in  ga_t;
    csf_pop     : out std_logic;
    cef_valid   : in  std_logic;
    cef_ga      : in  ga_t;
    cef_pop     : out std_logic;

    retgt_v     : in  std_logic;
    retgt_tgt   : in  ga_t;
    wf_pop      : in  std_logic;
    wk_eoc      : in  std_logic;

    rowC        : out u5_arr(0 to 31);
    lanesC      : in  byte_array(0 to 31);

    blk         : out blk_t
  );
end vhsnunzip_blkrd;

architecture behavior of vhsnunzip_blkrd is

  constant POP_WUSED : natural := 4;
  type mode_t is (M_START, M_RUN, M_PAD, M_NEXT);
  signal mode     : mode_t := M_START;
  signal bk       : unsigned(27 downto 0) := (others => '0');
  signal bk1      : unsigned(27 downto 0) := to_unsigned(1, 28);  -- bk + 1
  -- Chunk end, registered from the CEF head (no add before the compares):
  -- cel = block of the last byte, cenv = its valid bytes (1..16), ce_ok =
  -- they belong to the current CEF head.
  signal cel      : unsigned(27 downto 0) := (others => '0');
  signal cenv     : unsigned(4 downto 0) := (others => '0');
  signal ce_ok    : std_logic := '0';
  signal epoch    : unsigned(1 downto 0) := "00";
  signal first_p  : std_logic := '0';
  signal pad_n    : unsigned(1 downto 0) := "00";   -- pads still to issue
  signal nblk     : unsigned(1 downto 0) := "00";   -- issued blocks, sat 2
  signal wused    : unsigned(3 downto 0) := (others => '0');
  signal eoc_cnt  : unsigned(1 downto 0) := "00";
  signal pop_s    : std_logic;

  signal rowC_r   : unsigned(4 downto 0) := (others => '0');
  signal t_valid  : std_logic := '0';
  signal t_first  : std_logic := '0';
  signal t_half   : std_logic := '0';
  signal t_epoch  : unsigned(1 downto 0) := "00";
  signal t_base   : ga_t := (others => '0');
  signal t_nvalid : unsigned(4 downto 0) := (others => '0');
  signal blk_r    : blk_t := BLK_INIT;

begin

  -- wused <= 4 at the pop: see "Without SYNC_EOC" above (with WF_DEPTH > 5
  -- the credit alone would allow pad 3 beyond H+5).
  pop_s <= '1' when mode = M_NEXT and retgt_v = '0' and (not SYNC_EOC or eoc_cnt /= 0)
                    and wused <= POP_WUSED
           else '0';

  seq_proc: process (clk) is
    variable base   : ga_t;
    variable rel    : signed(27 downto 0);
    variable cem1   : ga_t;
    variable credok : boolean;
    variable issue  : boolean;
    variable nv     : unsigned(4 downto 0);
    variable eoc_v  : unsigned(1 downto 0);
  begin
    if rising_edge(clk) then
      base   := bk & "0000";
      credok := nblk /= 2 or wused < WF_DEPTH;
      issue  := false;
      nv     := (others => '0');

      cem1  := cef_ga - 1;
      cel   <= cem1(31 downto 4);
      cenv  <= resize(cem1(3 downto 0), 5) + 1;
      ce_ok <= cef_valid and not pop_s;

      if retgt_v = '1' then
        bk      <= retgt_tgt(31 downto 4);
        bk1     <= retgt_tgt(31 downto 4) + 1;
        epoch   <= epoch + 1;
        first_p <= '0';
        mode    <= M_RUN;
      else
        case mode is
          when M_START =>
            if csf_valid = '1' then
              bk      <= csf_ga(31 downto 4);
              bk1     <= csf_ga(31 downto 4) + 1;
              first_p <= '1';
              mode    <= M_RUN;
            end if;

          when M_RUN =>
            if credok then
              if cef_valid = '1' then
                -- Chunk end known (ce_ok one cycle after the CEF push).
                if ce_ok = '1' then
                  rel := signed(bk - cel);      -- modular, 28 b
                  issue := true;
                  if rel > 0 then
                    nv := (others => '0');      -- past the end: this is pad 1
                    pad_n <= to_unsigned(PAD_BLOCKS - 1, 2);
                    mode  <= M_PAD;
                  elsif rel = 0 then
                    nv := cenv;                 -- the chunk's last block
                    pad_n <= to_unsigned(PAD_BLOCKS, 2);
                    mode  <= M_PAD;
                  else
                    nv := to_unsigned(16, 5);
                  end if;
                end if;
              elsif signed(gb(31 downto 4) - bk1) >= 0 then
                -- 16*bk + 16 <= GB (modular; GB is 32-aligned).
                issue := true;
                nv := to_unsigned(16, 5);
              end if;
            end if;

          when M_PAD =>
            if credok then
              issue := true;
              nv := (others => '0');
              pad_n <= pad_n - 1;
              if pad_n = 1 then
                mode <= M_NEXT;
              end if;
            end if;

          when M_NEXT =>
            if pop_s = '1' then
              mode <= M_START;
            end if;
        end case;
      end if;

      -- Issue: register the port C row and the block tag.
      t_valid <= '0';
      if issue then
        rowC_r   <= bk(5 downto 1);
        t_half   <= bk(0);
        t_valid  <= '1';
        t_first  <= first_p;
        t_epoch  <= epoch;
        t_base   <= base;
        t_nvalid <= nv;
        bk       <= bk1;
        bk1      <= bk1 + 1;
        first_p  <= '0';
        if nblk /= 2 then
          nblk <= nblk + 1;
        end if;
      end if;

      -- Window credit.
      if issue and nblk = 2 then
        if wf_pop = '0' then
          wused <= wused + 1;
        end if;
      elsif wf_pop = '1' then
        wused <= wused - 1;
      end if;

      -- Walker EOCs not yet matched by a NEXT pop (SYNC_EOC only).
      eoc_v := eoc_cnt;
      if wk_eoc = '1' then eoc_v := eoc_v + 1; end if;
      if pop_s = '1' then eoc_v := eoc_v - 1; end if;
      if SYNC_EOC then
        eoc_cnt <= eoc_v;
      end if;

      -- Block register: the port C read of the previous issue.
      blk_r.valid  <= t_valid;
      blk_r.first  <= t_first;
      blk_r.epoch  <= t_epoch;
      blk_r.base   <= t_base;
      blk_r.nvalid <= t_nvalid;
      for j in 0 to 15 loop
        if t_half = '1' then
          blk_r.data(j) <= lanesC(16 + j);
        else
          blk_r.data(j) <= lanesC(j);
        end if;
      end loop;

      if reset = '1' then
        mode    <= M_START;
        bk      <= (others => '0');
        bk1     <= to_unsigned(1, 28);
        ce_ok   <= '0';
        epoch   <= "00";
        first_p <= '0';
        pad_n   <= "00";
        nblk    <= "00";
        wused   <= (others => '0');
        eoc_cnt <= "00";
        t_valid <= '0';
        blk_r.valid <= '0';
      end if;
    end if;
  end process;

  csf_pop <= pop_s;
  cef_pop <= pop_s;
  rowC    <= (others => rowC_r);
  blk     <= blk_r;

  -- pragma translate_off
  chk_proc: process (clk) is
    variable npop, neoc : natural := 0;   -- CSF/CEF pops, walker EOCs
  begin
    if rising_edge(clk) then
      if reset = '1' then
        npop := 0;
        neoc := 0;
      else
        -- The walker is in chunk neoc; chunks 0 .. npop-1 are popped.
        assert not (retgt_v = '1' and (mode = M_START or neoc < npop))
          report "blkrd: retarget after the chunk's CSF/CEF pop" severity failure;
        if pop_s = '1' then npop := npop + 1; end if;
        if wk_eoc = '1' then neoc := neoc + 1; end if;
        assert wused <= WF_DEPTH
          report "blkrd: window credit overflow" severity failure;
        assert not (mode = M_NEXT and cef_valid = '0')
          report "blkrd: NEXT without a chunk end" severity failure;
      end if;
    end if;
  end process;
  -- pragma translate_on

end behavior;
