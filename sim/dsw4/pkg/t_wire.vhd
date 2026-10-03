-- Interface wiring check: connects every DSW-4 skeleton the way vhsnunzip_core
-- will (B2 chain and B3 parser chain), plus the 32 history RAMs, and runs a
-- few cycles. Proves the port types connect; the stubs do nothing.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.vhsnunzip_dsw4_pkg.all;

entity t_wire is end entity;

architecture a of t_wire is
  signal clk, reset : std_logic := '1';
  signal co : cbeat_t := CBEAT_INIT;
  signal co_ready, csf_valid, cef_valid, csf_pop, cef_pop : std_logic;
  signal csf_ga, cef_ga, gb, a_r, lwx : ga_t;
  signal rowA, rowB, rowC : u5_arr(0 to 31);
  signal litA, litB, lanesC : byte_array(0 to 31);
  signal el : element_arr(0 to 3);
  signal wcnt, cred_ret, pf_adv, rp_adv : unsigned(2 downto 0);
  signal cred : unsigned(6 downto 0);
  signal pf : element_arr(0 to 3);
  signal nvis : unsigned(6 downto 0);
  signal cmd : element_stream;
  signal ag : agcmd_t;
  signal ram_a_cmd, ram_b_cmd : ram_command_array(0 to 31);
  signal ram_a_resp, ram_b_resp : ram_response_array(0 to 31);
  signal push, de : decompressed_stream;
  signal de_credit_ok : std_logic;
  signal level : unsigned(DE_DEPTH_LOG2 downto 0);
  -- B3 chain
  signal rowC3 : u5_arr(0 to 31);
  signal csf_pop3, cef_pop3, retgt_v, wf_pop : std_logic;
  signal retgt_tgt : ga_t;
  signal blk : blk_t;
  signal win : win_t;
  signal el3 : element_arr(0 to 3);
  signal wcnt3 : unsigned(2 downto 0);
  signal cred3 : unsigned(6 downto 0);
  -- core
  signal core_co_ready : std_logic;
  signal core_de : decompressed_stream;
  signal core_a_cmd, core_b_cmd : ram_command_array(0 to 31);
begin
  clk <= not clk after 5 ns;

  cbuf_i: entity work.vhsnunzip_cbuf port map (clk, reset, co, co_ready, lwx, gb, a_r,
    csf_valid, csf_ga, csf_pop, cef_valid, cef_ga, cef_pop,
    rowA, litA, rowB, litB, rowC, lanesC);
  ps_i: entity work.vhsnunzip_parse_serial port map (clk, reset, gb,
    csf_valid, csf_ga, csf_pop, cef_valid, cef_ga, cef_pop,
    rowC, lanesC, el, wcnt, cred_ret, cred);
  elq_i: entity work.vhsnunzip_elq port map (clk, reset, el, wcnt, pf, nvis, pf_adv, rp_adv, cred_ret);
  wr_i: entity work.vhsnunzip_writer
    generic map (TEST_SLOTS => 2, TEST_CUT => true, TEST_NOREP => true, TEST_LITP1 => true, TEST_STALL_PCT => 10)
    port map (clk, reset, pf, nvis, pf_adv, rp_adv, a_r, de_credit_ok, cmd);
  ag_i: entity work.vhsnunzip_agen generic map (TEST_ST_LINES => 12)
    port map (clk, reset, cmd, ag, ram_b_cmd);
  dp_i: entity work.vhsnunzip_dpath port map (clk, reset, ag, ram_b_resp, rowA, rowB, litA, litB,
    lwx, push, ram_a_cmd);
  de_i: entity work.vhsnunzip_defifo port map (clk, reset, push, de, '1', de_credit_ok, level);

  ram_gen: for i in 0 to 31 generate
    ram_i: entity work.vhsnunzip_ram
      port map (clk => clk, reset => reset, a_cmd => ram_a_cmd(i), a_resp => ram_a_resp(i),
                b_cmd => ram_b_cmd(i), b_resp => ram_b_resp(i));
  end generate;

  br_i: entity work.vhsnunzip_blkrd port map (clk, reset, gb, csf_valid, csf_ga, csf_pop3,
    cef_valid, cef_ga, cef_pop3, retgt_v, retgt_tgt, '1', rowC3, lanesC, blk);
  pt_i: entity work.vhsnunzip_pt port map (clk, reset, blk, win);
  wk_i: entity work.vhsnunzip_walker generic map (TEST_RETGT => true)
    port map (clk, reset, win, wf_pop, el3, wcnt3, retgt_v, retgt_tgt, cred_ret, cred3);

  core_i: entity work.vhsnunzip_core generic map (TEST_SLOTS => 3, TEST_ST_LINES => 12)
    port map (clk, reset, co, core_co_ready, core_de, '1', core_a_cmd, ram_a_resp, core_b_cmd, ram_b_resp);

  process
  begin
    wait for 22 ns;
    reset <= '0';
    for i in 1 to 10 loop
      wait until rising_edge(clk);
    end loop;
    assert cmd.valid = '0' and de.valid = '0' and push.valid = '0' report "stub outputs" severity failure;
    report "WIRE_OK";
    std.env.stop;
    wait;
  end process;
end architecture;
