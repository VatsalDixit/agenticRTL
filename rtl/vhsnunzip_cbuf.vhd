library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 infeed and compressed-byte buffer CBUF (SPEC 3.1).
--
-- 1 KiB ring of 32 lanes x 32 rows (lane j of row r holds the byte whose GA
-- is r*32 + j mod 1024), written one 32-byte beat per pop at row GB(9:5),
-- with three asynchronous read ports A, B and C. Every read port is per
-- lane: lane j of port P returns the byte at (rowP(j), lane j). The read rows
-- must come from registers in the reader.
--
--   co / co_ready  beat stream from the CO FIFO (fall-through). A beat pops
--                  when co.valid and co_ready. co_ready = credit_ok_r and
--                  CSF and CEF not full (registered flags; co_ready does not
--                  depend on co.valid). credit_ok_r is registered from
--                  ((GB' + 32 - LWX_r) mod 2^32) <= CBUF_WIN, where GB' is
--                  the GB this edge produces (GB + 32 on a pop), i.e. it is
--                  the credit of the beat that would be written next cycle;
--                  LWX_r = lwx registered once (monotonic, so conservative).
--                  CSF/CEF space is checked for every pop (SPEC 5.8).
--   lwx            low-water GA of the command that executed in S2a in the
--                  previous cycle (from vhsnunzip_dpath, registered there).
--   gb             GA of the next beat to be written (+32 per pop, reset 0).
--   a_r            GB registered twice (arrival counter for the writer).
--   csf_*          chunk-start FIFO head (GA of each chunk's first beat),
--                  fall-through; *_pop consumes the head at the clock edge.
--   cef_*          chunk-end FIFO head (GA one past each chunk's last byte).
--   rowA / litA    literal port A (dpath S1 rows -> S2a bytes).
--   rowB / litB    literal port B.
--   rowC / lanesC  parser port C. B2 parse_serial: any per-lane rows (a
--                  32-byte window at an arbitrary GA). B3 blkrd: all rows =
--                  bk(5:1); the block is lanes 16*bk(0) .. 16*bk(0)+15.
entity vhsnunzip_cbuf is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    co          : in  cbeat_t;
    co_ready    : out std_logic;

    lwx         : in  ga_t;
    gb          : out ga_t;
    a_r         : out ga_t;

    csf_valid   : out std_logic;
    csf_ga      : out ga_t;
    csf_pop     : in  std_logic;
    cef_valid   : out std_logic;
    cef_ga      : out ga_t;
    cef_pop     : in  std_logic;

    rowA        : in  u5_arr(0 to 31);
    litA        : out byte_array(0 to 31);
    rowB        : in  u5_arr(0 to 31);
    litB        : out byte_array(0 to 31);
    rowC        : in  u5_arr(0 to 31);
    lanesC      : out byte_array(0 to 31)
  );
end vhsnunzip_cbuf;

architecture behavior of vhsnunzip_cbuf is

  -- Write side.
  signal gb_r        : ga_t := (others => '0');   -- GA of the next beat
  signal lwx_r       : ga_t := (others => '0');   -- LWX_r (lwx registered once)
  signal credit_ok_r : std_logic := '1';          -- (GB + 32 - LWX_r) <= CBUF_WIN
  signal first_r     : std_logic := '1';          -- next beat starts a chunk
  signal a1_r, a2_r  : ga_t := (others => '0');   -- GB delayed 1 and 2 cycles
  signal pop         : std_logic;
  signal ready_s     : std_logic;
  signal wrow        : unsigned(4 downto 0);

  -- Chunk start / end FIFOs (CSF_DEPTH entries of GAs, flip-flops).
  type ga_fifo_t is array (0 to CSF_DEPTH - 1) of ga_t;
  signal csf_mem     : ga_fifo_t := (others => (others => '0'));
  signal cef_mem     : ga_fifo_t := (others => (others => '0'));
  signal csf_wp, csf_rp, cef_wp, cef_rp : unsigned(1 downto 0) := "00";
  signal csf_cnt, cef_cnt : unsigned(2 downto 0) := "000";
  signal csf_full_r, cef_full_r : std_logic := '0';
  signal csf_push, cef_push, csf_pop_i, cef_pop_i : std_logic;

  -- Storage: per lane j, 32 rows of 8 bits; lane j of row r holds the byte
  -- at GA r*32 + j (mod 1024). One write port (D) and three asynchronous
  -- read ports (A, B, C): 4 x RAM32M per lane.
  type lane_mem_t is array (0 to 31) of std_logic_vector(7 downto 0);

begin

  ready_s  <= credit_ok_r and not csf_full_r and not cef_full_r;
  co_ready <= ready_s;
  pop      <= co.valid and ready_s;
  wrow     <= gb_r(9 downto 5);

  csf_push  <= pop and first_r;
  cef_push  <= pop and co.last;
  csf_pop_i <= csf_pop when csf_cnt /= 0 else '0';
  cef_pop_i <= cef_pop when cef_cnt /= 0 else '0';

  reg_proc: process (clk) is
    variable diff   : ga_t;
    variable cnt_v  : unsigned(2 downto 0);
  begin
    if rising_edge(clk) then

      -- Credit for the beat that would be written NEXT cycle, at the GB this
      -- edge produces: pop ? GB + 64 : GB + 32, against LWX_r. LWX is
      -- monotonic, so the one-cycle-old LWX_r is conservative.
      diff := gb_r - lwx_r;
      if pop = '1' then
        if diff <= to_unsigned(CBUF_WIN - 64, 32) then
          credit_ok_r <= '1';
        else
          credit_ok_r <= '0';
        end if;
      else
        if diff <= to_unsigned(CBUF_WIN - 32, 32) then
          credit_ok_r <= '1';
        else
          credit_ok_r <= '0';
        end if;
      end if;
      lwx_r <= lwx;

      if pop = '1' then
        gb_r    <= gb_r + 32;
        first_r <= co.last;
      end if;
      a1_r <= gb_r;
      a2_r <= a1_r;

      -- CSF: GA of each chunk's first beat.
      if csf_push = '1' then
        csf_mem(to_integer(csf_wp)) <= gb_r;
        csf_wp <= csf_wp + 1;
      end if;
      if csf_pop_i = '1' then
        csf_rp <= csf_rp + 1;
      end if;
      cnt_v := csf_cnt;
      if csf_push = '1' then cnt_v := cnt_v + 1; end if;
      if csf_pop_i = '1' then cnt_v := cnt_v - 1; end if;
      csf_cnt <= cnt_v;
      if cnt_v = CSF_DEPTH then csf_full_r <= '1'; else csf_full_r <= '0'; end if;

      -- CEF: GA one past each chunk's last byte = GB + endi + 1.
      if cef_push = '1' then
        cef_mem(to_integer(cef_wp)) <= gb_r + resize(co.endi, 32) + 1;
        cef_wp <= cef_wp + 1;
      end if;
      if cef_pop_i = '1' then
        cef_rp <= cef_rp + 1;
      end if;
      cnt_v := cef_cnt;
      if cef_push = '1' then cnt_v := cnt_v + 1; end if;
      if cef_pop_i = '1' then cnt_v := cnt_v - 1; end if;
      cef_cnt <= cnt_v;
      if cnt_v = CSF_DEPTH then cef_full_r <= '1'; else cef_full_r <= '0'; end if;

      if reset = '1' then
        gb_r        <= (others => '0');
        lwx_r       <= (others => '0');
        credit_ok_r <= '1';
        first_r     <= '1';
        a1_r        <= (others => '0');
        a2_r        <= (others => '0');
        csf_wp      <= "00";
        csf_rp      <= "00";
        cef_wp      <= "00";
        cef_rp      <= "00";
        csf_cnt     <= "000";
        cef_cnt     <= "000";
        csf_full_r  <= '0';
        cef_full_r  <= '0';
      end if;
    end if;
  end process;

  gb        <= gb_r;
  a_r       <= a2_r;
  csf_valid <= '1' when csf_cnt /= 0 else '0';
  csf_ga    <= csf_mem(to_integer(csf_rp));
  cef_valid <= '1' when cef_cnt /= 0 else '0';
  cef_ga    <= cef_mem(to_integer(cef_rp));

  lane_gen: for j in 0 to 31 generate
    signal mem : lane_mem_t := (others => (others => '0'));
  begin
    wr_proc: process (clk) is
    begin
      if rising_edge(clk) then
        if pop = '1' then
          mem(to_integer(wrow)) <= co.data(j);
        end if;
      end if;
    end process;
    litA(j)   <= mem(to_integer(rowA(j)));
    litB(j)   <= mem(to_integer(rowB(j)));
    lanesC(j) <= mem(to_integer(rowC(j)));
  end generate;

  -- pragma translate_off
  chk_proc: process (clk) is
  begin
    if rising_edge(clk) then
      if reset = '0' then
        -- Live bytes are >= LWX and never past GB.
        assert ga_sdiff(gb_r, lwx) >= 0
          report "cbuf: LWX " & to_hstring(lwx) & " is past GB " & to_hstring(gb_r)
          severity failure;
        -- The beat at GB overwrites GAs GB-1024 .. GB-993: all must be < LWX.
        if pop = '1' then
          assert ga_sdiff(gb_r + 32, lwx) <= 1024
            report "cbuf: write at GB " & to_hstring(gb_r) & " overwrites live bytes (LWX "
                   & to_hstring(lwx) & ")"
            severity failure;
        end if;
        assert not (csf_pop = '1' and csf_cnt = 0)
          report "cbuf: CSF pop while empty" severity failure;
        assert not (cef_pop = '1' and cef_cnt = 0)
          report "cbuf: CEF pop while empty" severity failure;
      end if;
    end if;
  end process;
  -- pragma translate_on

end behavior;
