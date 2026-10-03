library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 core (SPEC 7, 11): CO FIFO, CBUF, table parser (blkrd + pt +
-- WFIFO + walker, vhsnunzip_parser), ELQ, writer, agen, dpath, de FIFO and
-- credits. Replaces vhsnunzip_pipeline in step B2. The 32 history RAMs stay
-- in vhsnunzip_unbuffered.
--
--   co / co_ready      32-byte input beats into the CO FIFO. The top maps its
--                      port: data byte i = co_data(8i+7 downto 8i),
--                      endi = co_cnt - 1 (mod 32; co_cnt 0 means 32 bytes),
--                      last = co_last.
--   de / de_ready      output lines (cnt literal 0..32, one last per chunk,
--                      cnt = 0 / last = 1 for an empty chunk).
--   ram_a_cmd / resp   port a of RAM instance i (history writes).
--   ram_b_cmd / resp   port b of RAM instance i (LT reads).
--                      Instance i = m*8 + par*4 + w (SPEC 6).
--
-- TEST_* generics: see vhsnunzip_writer, vhsnunzip_agen, vhsnunzip_parser.
--
-- Interface notes (B2c): the writer's de_credit_ok is the de FIFO's
-- registered credit, its a_r port is the CBUF's GB (the writer adds both
-- registers of A_r itself), and the
-- writer command drives AG1 with no extra register (the de FIFO credit budget
-- in vhsnunzip_defifo counts exactly this pipe: raise DE_CRED if a stage is
-- added between the writer and S4). lwx goes from the dpath straight to the
-- CBUF, which registers it once more.
entity vhsnunzip_core is
  generic (
    TEST_SLOTS     : natural := 4;
    TEST_CUT       : boolean := false;
    TEST_NOREP     : boolean := false;
    TEST_LITP1     : boolean := false;
    TEST_RETGT     : boolean := false;
    TEST_ST_LINES  : natural := ST_LINES;
    TEST_STALL_PCT : natural := 0;
    TEST_PROBE     : boolean := false   -- sim only: probe_wr.txt / probe_wk.txt
  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    co          : in  cbeat_t;
    co_ready    : out std_logic;

    de          : out decompressed_stream;
    de_ready    : in  std_logic;

    ram_a_cmd   : out ram_command_array(0 to 31);
    ram_a_resp  : in  ram_response_array(0 to 31);
    ram_b_cmd   : out ram_command_array(0 to 31);
    ram_b_resp  : in  ram_response_array(0 to 31)
  );
end vhsnunzip_core;

architecture behavior of vhsnunzip_core is

  -- Infeed: CO FIFO -> CBUF.
  signal cof_rd        : cbeat_t;
  signal cbuf_ready    : std_logic;
  signal gb            : ga_t;
  signal a_r           : ga_t;
  signal csf_valid     : std_logic;
  signal csf_ga        : ga_t;
  signal csf_pop       : std_logic;
  signal cef_valid     : std_logic;
  signal cef_ga        : ga_t;
  signal cef_pop       : std_logic;
  signal rowA, rowB    : u5_arr(0 to 31);
  signal litA, litB    : byte_array(0 to 31);
  signal rowC          : u5_arr(0 to 31);
  signal lanesC        : byte_array(0 to 31);

  -- Parser -> ELQ -> writer.
  signal el            : element_arr(0 to 3);
  signal wcnt          : unsigned(2 downto 0);
  signal cred_ret      : unsigned(2 downto 0);
  signal pf            : element_arr(0 to 3);
  signal nvis          : unsigned(6 downto 0);
  signal pf_adv        : unsigned(2 downto 0);
  signal rp_adv        : unsigned(2 downto 0);

  -- Writer -> agen -> dpath -> de FIFO.
  signal wcmd          : element_stream;
  signal ag            : agcmd_t;
  signal lwx           : ga_t;
  signal push          : decompressed_stream;
  signal de_credit_ok  : std_logic;

begin

  -- CO FIFO (SPEC 3.1): 16 x 32-byte beats, SRL fall-through.
  cofifo_inst: entity work.vhsnunzip_cofifo
    port map (
      clk         => clk,
      reset       => reset,
      wr          => co,
      wr_ready    => co_ready,
      rd          => cof_rd,
      rd_ready    => cbuf_ready,
      level       => open
    );

  -- CBUF (SPEC 3.1): 1 KiB compressed-byte ring, credit from the dpath's LWX.
  cbuf_inst: entity work.vhsnunzip_cbuf
    port map (
      clk         => clk,
      reset       => reset,
      co          => cof_rd,
      co_ready    => cbuf_ready,
      lwx         => lwx,
      gb          => gb,
      a_r         => a_r,
      csf_valid   => csf_valid,
      csf_ga      => csf_ga,
      csf_pop     => csf_pop,
      cef_valid   => cef_valid,
      cef_ga      => cef_ga,
      cef_pop     => cef_pop,
      rowA        => rowA,
      litA        => litA,
      rowB        => rowB,
      litB        => litB,
      rowC        => rowC,
      lanesC      => lanesC
    );

  -- Table parser (SPEC 3.2-3.5): block reader, PT1-PT3, WFIFO, walker.
  parse_inst: entity work.vhsnunzip_parser
    generic map (TEST_RETGT => TEST_RETGT, PROBE => TEST_PROBE)
    port map (
      clk         => clk,
      reset       => reset,
      gb          => gb,
      csf_valid   => csf_valid,
      csf_ga      => csf_ga,
      csf_pop     => csf_pop,
      cef_valid   => cef_valid,
      cef_ga      => cef_ga,
      cef_pop     => cef_pop,
      rowC        => rowC,
      lanesC      => lanesC,
      el          => el,
      wcnt        => wcnt,
      cred_ret    => cred_ret,
      cred        => open
    );

  -- Element queue (SPEC 3.6).
  elq_inst: entity work.vhsnunzip_elq
    port map (
      clk         => clk,
      reset       => reset,
      el          => el,
      wcnt        => wcnt,
      pf          => pf,
      nvis        => nvis,
      pf_adv      => pf_adv,
      rp_adv      => rp_adv,
      cred_ret    => cred_ret
    );

  -- Writer, LOOP L1 (SPEC 3.7). de_credit_ok is the de FIFO's registered
  -- credit; its a_r port takes the CBUF's GB register itself: the writer
  -- registers it twice (A_r, SPEC 3.1) and uses the two earlier values to
  -- register every availability field one cycle ahead. The CBUF's own a_r
  -- output (GB registered twice) is equal to the writer's internal A_r.
  writer_inst: entity work.vhsnunzip_writer
    generic map (
      TEST_SLOTS     => TEST_SLOTS,
      TEST_CUT       => TEST_CUT,
      TEST_NOREP     => TEST_NOREP,
      TEST_LITP1     => TEST_LITP1,
      TEST_STALL_PCT => TEST_STALL_PCT,
      PROBE          => TEST_PROBE
    )
    port map (
      clk          => clk,
      reset        => reset,
      pf           => pf,
      nvis         => nvis,
      pf_adv       => pf_adv,
      rp_adv       => rp_adv,
      a_r          => gb,
      de_credit_ok => de_credit_ok,
      cmd          => wcmd
    );

  -- Address generation AG1-AG2 (SPEC 3.8); presents the LT reads on port b.
  agen_inst: entity work.vhsnunzip_agen
    generic map (
      TEST_ST_LINES => TEST_ST_LINES
    )
    port map (
      clk         => clk,
      reset       => reset,
      cmd         => wcmd,
      ag          => ag,
      ram_rd      => ram_b_cmd
    );

  -- Datapath D1..S4 and the history write on port a (SPEC 3.9-3.13).
  dpath_inst: entity work.vhsnunzip_dpath
    port map (
      clk         => clk,
      reset       => reset,
      ag          => ag,
      ram_rd_resp => ram_b_resp,
      rowA        => rowA,
      rowB        => rowB,
      litA        => litA,
      litB        => litB,
      lwx         => lwx,
      push        => push,
      ram_wr      => ram_a_cmd
    );

  -- de FIFO and the writer's de credit (SPEC 3.12).
  defifo_inst: entity work.vhsnunzip_defifo
    port map (
      clk          => clk,
      reset        => reset,
      push         => push,
      de           => de,
      de_ready     => de_ready,
      de_credit_ok => de_credit_ok,
      level        => open
    );

  -- pragma translate_off
  -- SPEC 7 "LT source line <= hw_ptr - 2", exactly: every history line an LT
  -- read uses (L0, and L0 + 1 when smod + n > 32) must have had its write
  -- presented on port a at least 2 cycles before the read is presented on
  -- port b (SPEC 1: a write presented in cycle P is seen by reads presented
  -- in P + 2 or later), in the same chunk, and be the newest copy of that
  -- line (written at most 4096 lines ago, so a row that still holds the line
  -- 8192 back is caught). ag and ram_b_cmd are both AG2 registers (cycle C).
  -- The write in cycle P belongs to the push of cycle P - 1.
  lt_chk: process (clk) is
    type int_arr is array (0 to 8191) of integer;
    variable wtime  : int_arr := (others => integer'low / 2);
    variable wchunk : int_arr := (others => -1);
    variable wser   : int_arr := (others => integer'low / 2);
    variable cyc    : integer := 0;
    variable nwr    : integer := 0;     -- history lines written so far
    variable pcnt   : integer := 0;     -- pushes with last = 1 so far
    variable wtag   : integer := 0;     -- chunk of this cycle's writes
    variable rcnt   : integer := 0;     -- AG2 commands with last = 1 so far
    variable ln, l0 : integer;
    variable sk, sn, nl : integer;
  begin
    if rising_edge(clk) then
      if reset = '1' then
        pcnt := 0; wtag := 0; rcnt := 0;
        wchunk := (others => -1);
      else
        -- History writes presented this cycle (one parity bank per line).
        for par in 0 to 1 loop
          if ram_a_cmd(par * 4).valid = '1' and ram_a_cmd(par * 4).wren = '1' then
            ln := to_integer(ram_a_cmd(par * 4).addr) * 2 + par;
            wtime(ln) := cyc;
            wchunk(ln) := wtag;
            wser(ln) := nwr;
            nwr := nwr + 1;
          end if;
        end loop;
        -- LT reads presented this cycle.
        if ag.valid = '1' then
          for k in 0 to 3 loop
            if ag.tier(k) = T_LT then
              l0 := to_integer(ram_b_cmd(k * 8 + 4).addr) * 2;
              if ag.l0p(k) = '1' then
                l0 := l0 + 1;
              end if;
              case k is
                when 0 => sk := 0;                       sn := to_integer(ag.s1);
                when 1 => sk := to_integer(ag.s1);       sn := to_integer(ag.s2);
                when 2 => sk := to_integer(ag.s2);       sn := to_integer(ag.s3);
                when others => sk := to_integer(ag.s3);  sn := to_integer(ag.d_total);
              end case;
              nl := 1;
              if to_integer(ag.smod(k)) + (sn - sk) > 32 then
                nl := 2;
              end if;
              for i in 0 to nl - 1 loop
                ln := (l0 + i) mod 8192;
                assert wchunk(ln) = rcnt and wtime(ln) <= cyc - 2 and wser(ln) >= nwr - 4096
                  report "core: LT read of history line " & integer'image(ln)
                         & " (slot " & integer'image(k) & ") before its write is visible: "
                         & "written in cycle " & integer'image(wtime(ln)) & " of chunk "
                         & integer'image(wchunk(ln)) & ", read in cycle " & integer'image(cyc)
                         & " of chunk " & integer'image(rcnt) & " (needs <= read - 2)"
                  severity failure;
              end loop;
            end if;
          end loop;
          if ag.last = '1' then
            rcnt := rcnt + 1;
          end if;
        end if;
        -- Next cycle's writes come from this cycle's push.
        wtag := pcnt;
        if push.valid = '1' and push.last = '1' then
          pcnt := pcnt + 1;
        end if;
      end if;
      cyc := cyc + 1;
    end if;
  end process;
  -- pragma translate_on

end behavior;
