library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.vhsnunzip_dsw4_pkg.all;
-- review wrapper: writer + ELQ (the L1 loop and its window refill)
entity wr_elq is
  port (
    clk, reset   : in  std_logic;
    el           : in  element_arr(0 to 3);
    wcnt         : in  unsigned(2 downto 0);
    a_r          : in  ga_t;
    de_credit_ok : in  std_logic;
    cmd          : out element_stream;
    cred_ret     : out unsigned(2 downto 0));
end wr_elq;
architecture rtl of wr_elq is
  signal pf : element_arr(0 to 3);
  signal nvis : unsigned(6 downto 0);
  signal pf_adv, rp_adv : unsigned(2 downto 0);
begin
  elq: entity work.vhsnunzip_elq port map (clk, reset, el, wcnt, pf, nvis, pf_adv, rp_adv, cred_ret);
  wr: entity work.vhsnunzip_writer port map (clk => clk, reset => reset, pf => pf, nvis => nvis,
      pf_adv => pf_adv, rp_adv => rp_adv, a_r => a_r, de_credit_ok => de_credit_ok, cmd => cmd);
end rtl;
