library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- DSW-4 step B1 input gearbox: splits a 32-byte compressed-stream beat into
-- up to four 8-byte sub-beats for a core that consumes 8 bytes per transfer.
--
-- Input (32-byte side), the top-level convention of SPEC section 1: every
-- transfer is full except a chunk's last; byte 0 is in_data(7:0); in_cnt is
-- the implicit 5-bit count (0 means 32).
--
-- Output (8-byte side): out_endi is the index of the last valid byte of the
-- sub-beat (7 unless out_last), out_last marks the chunk's final sub-beat.
-- Sub-beats entirely past the end of the chunk are skipped. Non-last input
-- beats always produce all four sub-beats.
--
-- The beat is held in a shift register so the output is always the low 8
-- bytes; one sub-beat leaves per cycle and the next 32-byte beat is accepted
-- in the same cycle the final sub-beat of the current one is taken, so the
-- 8-byte side sees no bubble between beats. Only plain std_logic types are
-- used on the ports so this file does not depend on the core's records.
entity vhsnunzip_co_split is
  port (
    clk       : in  std_logic;
    reset     : in  std_logic;

    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(255 downto 0);
    in_cnt    : in  std_logic_vector(4 downto 0);
    in_last   : in  std_logic;

    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(63 downto 0);
    out_endi  : out unsigned(2 downto 0);
    out_last  : out std_logic
  );
end vhsnunzip_co_split;

architecture behavior of vhsnunzip_co_split is

  signal buf      : std_logic_vector(255 downto 0);
  signal bval     : std_logic := '0';
  -- Number of sub-beats still to send after the current one.
  signal remain   : unsigned(1 downto 0) := "00";
  -- The held beat is the last of its chunk.
  signal blast    : std_logic := '0';
  -- endi of the final sub-beat of the held beat.
  signal bendi    : unsigned(2 downto 0) := "111";

  signal final    : std_logic;
  signal take     : std_logic;
  signal ready_s  : std_logic;

begin

  final <= '1' when remain = 0 else '0';
  take <= bval and out_ready;

  -- Accept a new beat when nothing is held, or when the final sub-beat of
  -- the held one leaves this cycle.
  ready_s <= (not bval) or (final and out_ready);
  in_ready <= ready_s;

  out_valid <= bval;
  out_data <= buf(63 downto 0);
  out_last <= blast and final;
  out_endi <= bendi when (blast and final) = '1' else "111";

  reg_proc: process (clk) is
    variable n1 : unsigned(4 downto 0);
  begin
    if rising_edge(clk) then

      if take = '1' then
        buf <= x"0000000000000000" & buf(255 downto 64);
        remain <= remain - 1;
        if final = '1' then
          bval <= '0';
        end if;
      end if;

      if in_valid = '1' and ready_s = '1' then
        buf <= in_data;
        bval <= '1';
        blast <= in_last;
        -- n1 = number of valid bytes - 1 (0..31); the implicit count 0 means
        -- 32 bytes, which wraps to 31 here.
        n1 := unsigned(in_cnt) - 1;
        if in_last = '1' then
          remain <= n1(4 downto 3);
          bendi <= n1(2 downto 0);
        else
          remain <= "11";
          bendi <= "111";
        end if;
      end if;

      if reset = '1' then
        bval <= '0';
      end if;

    end if;
  end process;

end behavior;
