library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 core (SPEC 7, 11): CO FIFO, CBUF, parser (B2: parse_serial; B3:
-- blkrd + pt + WFIFO + walker), ELQ, writer, agen, dpath, de FIFO and
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
-- TEST_* generics: see vhsnunzip_writer, vhsnunzip_agen, vhsnunzip_walker.
-- TEST_RETGT has no effect in B2 (parse_serial has no retarget).
--
-- Interface notes (B2c): the writer's de_credit_ok is the de FIFO's
-- registered credit, its a_r is the CBUF's GB registered twice, and the
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
    TEST_STALL_PCT : natural := 0
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

  -- Serial parser (B2 only; replaced by blkrd + pt + walker in B3).
  parse_inst: entity work.vhsnunzip_parse_serial
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
  -- credit; a_r is the CBUF arrival counter (GB registered twice).
  writer_inst: entity work.vhsnunzip_writer
    generic map (
      TEST_SLOTS     => TEST_SLOTS,
      TEST_CUT       => TEST_CUT,
      TEST_NOREP     => TEST_NOREP,
      TEST_LITP1     => TEST_LITP1,
      TEST_STALL_PCT => TEST_STALL_PCT
    )
    port map (
      clk          => clk,
      reset        => reset,
      pf           => pf,
      nvis         => nvis,
      pf_adv       => pf_adv,
      rp_adv       => rp_adv,
      a_r          => a_r,
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

end behavior;
