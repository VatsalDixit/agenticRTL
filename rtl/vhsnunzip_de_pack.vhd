library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

-- DSW-4 step B1 output gearbox: pairs 16-byte decompressed lines into
-- 32-byte transfers.
--
-- Input (16-byte side): in_cnt is the literal number of valid bytes (0..16).
-- Every line except a chunk's last must be full (16 bytes); the last line may
-- hold 0..16 bytes, and an empty chunk is a single line with cnt 0, last 1.
--
-- Output (32-byte side), the top-level convention of SPEC section 1: valid
-- bytes are packed from byte 0 (out_data(7:0)); out_cnt is the literal count
-- 0..32; exactly one out_last per chunk. A full line is held until its pair
-- arrives; the last line of a chunk flushes it (16 + cnt bytes), or is sent
-- alone (cnt bytes, possibly 0 for an empty chunk) when nothing is held.
--
-- The output is a register, so the 16-byte side sees no combinational path
-- from out_ready beyond in_ready. Only plain std_logic types are used on the
-- ports so this file does not depend on the core's records.
entity vhsnunzip_de_pack is
  port (
    clk       : in  std_logic;
    reset     : in  std_logic;

    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(127 downto 0);
    in_cnt    : in  unsigned(4 downto 0);
    in_last   : in  std_logic;

    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(255 downto 0);
    out_cnt   : out std_logic_vector(5 downto 0);
    out_last  : out std_logic
  );
end vhsnunzip_de_pack;

architecture behavior of vhsnunzip_de_pack is

  -- Held first line of a pair.
  signal hold     : std_logic_vector(127 downto 0);
  signal hval     : std_logic := '0';

  -- Output register.
  signal odata    : std_logic_vector(255 downto 0) := (others => '0');
  signal ocnt     : unsigned(5 downto 0) := (others => '0');
  signal olast    : std_logic := '0';
  signal oval     : std_logic := '0';

  signal ready_s  : std_logic;

begin

  ready_s <= (not oval) or out_ready;
  in_ready <= ready_s;

  out_valid <= oval;
  out_data <= odata;
  out_cnt <= std_logic_vector(ocnt);
  out_last <= olast;

  reg_proc: process (clk) is
  begin
    if rising_edge(clk) then

      if oval = '1' and out_ready = '1' then
        oval <= '0';
      end if;

      if in_valid = '1' and ready_s = '1' then

        -- pragma translate_off
        assert in_last = '1' or in_cnt = 16
          report "vhsnunzip_de_pack: non-last line is not full" severity failure;
        -- pragma translate_on

        if hval = '1' then
          odata <= in_data & hold;
          ocnt <= 16 + resize(in_cnt, 6);
          olast <= in_last;
          oval <= '1';
          hval <= '0';
        elsif in_last = '1' then
          odata <= x"00000000000000000000000000000000" & in_data;
          ocnt <= resize(in_cnt, 6);
          olast <= '1';
          oval <= '1';
        else
          hold <= in_data;
          hval <= '1';
        end if;

      end if;

      if reset = '1' then
        hval <= '0';
        oval <= '0';
      end if;

    end if;
  end process;

end behavior;
