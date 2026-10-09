library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 table parser (SPEC 3.2-3.5, build step B3): block reader, window
-- assembly + hop tables PT1-PT3, WFIFO, walker WK + emission PE.
--
-- Same ports as vhsnunzip_parse_serial (B2), so the core swaps the entity
-- name and maps TEST_RETGT:
--   gb             GA of the next CBUF beat (modular arrival compare).
--   csf_* / cef_*  chunk start / end FIFO heads from vhsnunzip_cbuf; popped
--                  by the block reader when it leaves a chunk.
--   rowC / lanesC  CBUF port C: rowC all equal (registered), lanesC async.
--   el / wcnt      elements to the ELQ: el(0 to wcnt-1), wcnt 0..4, registered.
--   cred_ret       credits returned by the ELQ (elements the writer retired).
--   cred           current credit count (reset ELQ_CRED; debug / probe).
-- Each chunk ends with one EOC element (len 0, lptr = the chunk end GA).
--
-- WFIFO: WF_DEPTH windows of registers (circular, head = entry rp). The issue
-- -> WFIFO round trip is about 7 cycles, so the SPEC's 4 windows starve the
-- walker (train-taxi: walker without a window 34% of cycles, 15.07 B/cycle);
-- 6 gives 19.23 and 8 gives 19.38 B/cycle (model 19.34). It never
-- overflows: the block reader issues a window-completing block only while
-- the windows in flight plus those queued are < WF_DEPTH.
--
-- Sim-only generics: TEST_RETGT (walker always retargets instead of stepping;
-- the block reader then waits for the walker's EOC before leaving a chunk),
-- GRP_FILE (walker group trace, gold groups.txt format).
entity vhsnunzip_parser is
  generic (
    TEST_RETGT  : boolean  := false;
    WF_DEPTH    : positive := 8;   -- B3c: 4 starved the walker (taxi 15.1 B/c)
    GRP_FILE    : string   := "-";
    PROBE       : boolean  := false   -- sim only: walker probe_wk.txt
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

    rowC        : out u5_arr(0 to 31);
    lanesC      : in  byte_array(0 to 31);

    el          : out element_arr(0 to 3);
    wcnt        : out unsigned(2 downto 0);
    cred_ret    : in  unsigned(2 downto 0);
    cred        : out unsigned(6 downto 0)
  );
end vhsnunzip_parser;

architecture behavior of vhsnunzip_parser is

  signal blk       : blk_t;
  signal win       : win_t;
  signal wf_head   : win_t;
  signal wf_pop    : std_logic;
  signal retgt_v   : std_logic;
  signal retgt_tgt : ga_t;
  signal eoc_p     : std_logic;

  -- The WFIFO is one flat vector per window (win_pack), so the head read is a
  -- distributed-RAM read port (1 LUT/bit) instead of a WF_DEPTH:1 mux over the
  -- whole record (~2 LUT/bit). Same asynchronous-read timing: the write is
  -- clocked and the read combinational, exactly the LUTRAM pattern.
  type wf_mem_t is array (0 to WF_DEPTH - 1) of std_logic_vector(WIN_BITS - 1 downto 0);
  signal wf_mem    : wf_mem_t := (others => (others => '0'));
  attribute ram_style : string;
  attribute ram_style of wf_mem : signal is "distributed";
  signal wf_wp     : natural range 0 to WF_DEPTH - 1 := 0;
  signal wf_rp     : natural range 0 to WF_DEPTH - 1 := 0;
  signal wf_cnt    : natural range 0 to WF_DEPTH := 0;

begin

  blkrd_inst: entity work.vhsnunzip_blkrd
    generic map (WF_DEPTH => WF_DEPTH, SYNC_EOC => TEST_RETGT)
    port map (
      clk => clk, reset => reset, gb => gb,
      csf_valid => csf_valid, csf_ga => csf_ga, csf_pop => csf_pop,
      cef_valid => cef_valid, cef_ga => cef_ga, cef_pop => cef_pop,
      retgt_v => retgt_v, retgt_tgt => retgt_tgt, wf_pop => wf_pop, wk_eoc => eoc_p,
      rowC => rowC, lanesC => lanesC, blk => blk);

  pt_inst: entity work.vhsnunzip_pt
    port map (clk => clk, reset => reset, blk => blk, win => win);

  wf_proc: process (clk) is
    variable c : natural range 0 to WF_DEPTH;
  begin
    if rising_edge(clk) then
      c := wf_cnt;
      if win.valid = '1' then
        wf_mem(wf_wp) <= win_pack(win);
        wf_wp <= (wf_wp + 1) mod WF_DEPTH;
        c := c + 1;
      end if;
      if wf_pop = '1' then
        wf_rp <= (wf_rp + 1) mod WF_DEPTH;
        c := c - 1;
      end if;
      wf_cnt <= c;
      if reset = '1' then
        wf_wp  <= 0;
        wf_rp  <= 0;
        wf_cnt <= 0;
      end if;
    end if;
  end process;

  head_proc: process (wf_mem, wf_rp, wf_cnt) is
    variable h : win_t;
  begin
    h := win_unpack(wf_mem(wf_rp));
    if wf_cnt /= 0 then
      h.valid := '1';
    else
      h.valid := '0';
    end if;
    wf_head <= h;
  end process;

  walker_inst: entity work.vhsnunzip_walker
    generic map (TEST_RETGT => TEST_RETGT, GRP_FILE => GRP_FILE, PROBE => PROBE)
    port map (
      clk => clk, reset => reset, wf_head => wf_head, wf_pop => wf_pop,
      el => el, wcnt => wcnt, retgt_v => retgt_v, retgt_tgt => retgt_tgt,
      eoc_p => eoc_p, cred_ret => cred_ret, cred => cred);

  -- pragma translate_off
  chk_proc: process (clk) is
  begin
    if rising_edge(clk) then
      if reset = '0' then
        assert not (win.valid = '1' and wf_cnt = WF_DEPTH and wf_pop = '0')
          report "parser: WFIFO overflow" severity failure;
      end if;
    end if;
  end process;
  -- pragma translate_on

end behavior;
