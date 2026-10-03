library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 walker WK, LOOP L3, and emission PE1-PE3 (SPEC 3.4, 3.5; build step
-- B3). Same element / credit ports as vhsnunzip_parse_serial.
--
--   wf_head / wf_pop    WFIFO head window (wf_head.valid) and its pop.
--   el / wcnt           PE3 output: el(0 to wcnt-1), wcnt 0..4, to the ELQ.
--   retgt_v/retgt_tgt   retarget to the block reader (tgt = T and not 15).
--   cred_ret            credits returned by the ELQ (elements retired).
--   cred                credit count (reset ELQ_CRED; debug / probe).
--
-- TEST_RETGT (sim only): always retarget instead of stepping after a far
-- literal; must give bit-identical output.
entity vhsnunzip_walker is
  generic (
    TEST_RETGT  : boolean := false
  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    wf_head     : in  win_t;
    wf_pop      : out std_logic;

    el          : out element_arr(0 to 3);
    wcnt        : out unsigned(2 downto 0);

    retgt_v     : out std_logic;
    retgt_tgt   : out ga_t;

    cred_ret    : in  unsigned(2 downto 0);
    cred        : out unsigned(6 downto 0)
  );
end vhsnunzip_walker;

-- Interface stub: replace with the implementation.
architecture stub of vhsnunzip_walker is
begin
  wf_pop    <= '0';
  el        <= (others => ELEMENT_INIT);
  wcnt      <= (others => '0');
  retgt_v   <= '0';
  retgt_tgt <= (others => '0');
  cred      <= to_unsigned(ELQ_CRED, 7);
end stub;
