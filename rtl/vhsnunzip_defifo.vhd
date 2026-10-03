library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 de FIFO and de credit (SPEC 3.12).
--
-- 2**DEPTH_LOG2 deep x (256 data + 6 cnt + 1 last), SRL fall-through (reuses
-- vhsnunzip_fifo). The credit is the writer's issue gate.
--
--   push           write side, from vhsnunzip_dpath; push.valid = write.
--                  There is no ready: a write into a full FIFO is an error
--                  (simulation assert), excluded by the credit.
--   de / de_ready  the de port stream (fall-through head).
--   de_credit_ok   registered: free entries >= DE_CRED, where "free" is
--                  taken AFTER this cycle's push/pop (the occupancy the FIFO
--                  will have next cycle), so the credit lags the FIFO by one
--                  register only.
--   level          occupancy, true count 0 .. 2**DEPTH_LOG2 (debug / probe),
--                  registered.
--
-- Credit budget with the B2 pipe (writer register at T+1 for a decision in T,
-- AG1 T+2, AG2 T+3 = C, PUSH visible C+6 = T+9): the credit visible in
-- decision cycle T counts every push written up to the end of T-1, i.e. from
-- decisions <= T-10. Uncounted are decisions T-9 .. T: <= 10 lines plus one
-- spill line (a spilling last command is followed by a bubble, so at most
-- the newest one adds a second line) = 11 <= DE_CRED = 12.
entity vhsnunzip_defifo is
  generic (
    DEPTH_LOG2  : natural := DE_DEPTH_LOG2
  );
  port (
    clk          : in  std_logic;
    reset        : in  std_logic;

    push         : in  decompressed_stream;

    de           : out decompressed_stream;
    de_ready     : in  std_logic;

    de_credit_ok : out std_logic;
    level        : out unsigned(DEPTH_LOG2 downto 0)
  );
end vhsnunzip_defifo;

architecture rtl of vhsnunzip_defifo is

  constant DEPTH : natural := 2**DEPTH_LOG2;

  signal wr_ready  : std_logic;
  signal rd_valid  : std_logic;
  signal rd_data   : byte_array(0 to 31);
  signal wr_ctrl   : std_logic_vector(6 downto 0);
  signal rd_ctrl   : std_logic_vector(6 downto 0);

  signal count_r   : unsigned(DEPTH_LOG2 downto 0) := (others => '0');
  signal credit_r  : std_logic := '0';

begin

  wr_ctrl <= push.last & std_logic_vector(push.cnt);

  fifo_inst: entity work.vhsnunzip_fifo
    generic map (
      DATA_WIDTH  => 32,
      CTRL_WIDTH  => 7,
      DEPTH_LOG2  => DEPTH_LOG2
    )
    port map (
      clk         => clk,
      reset       => reset,
      wr_valid    => push.valid,
      wr_ready    => wr_ready,
      wr_data     => push.data,
      wr_ctrl     => wr_ctrl,
      rd_valid    => rd_valid,
      rd_ready    => de_ready,
      rd_data     => rd_data,
      rd_ctrl     => rd_ctrl,
      level       => open,
      empty       => open,
      full        => open
    );

  de.valid <= rd_valid;
  de.last  <= rd_ctrl(6);
  de.cnt   <= unsigned(rd_ctrl(5 downto 0));
  de.data  <= rd_data;

  credit_proc: process (clk) is
    variable cnt_v : unsigned(DEPTH_LOG2 downto 0);
  begin
    if rising_edge(clk) then
      cnt_v := count_r;
      if rd_valid = '1' and de_ready = '1' then
        cnt_v := cnt_v - 1;
      end if;
      if push.valid = '1' and wr_ready = '1' then
        cnt_v := cnt_v + 1;
      end if;
      count_r <= cnt_v;
      if DEPTH - to_integer(cnt_v) >= DE_CRED then
        credit_r <= '1';
      else
        credit_r <= '0';
      end if;
      if reset = '1' then
        count_r  <= (others => '0');
        credit_r <= '0';
      end if;
    end if;
  end process;

  de_credit_ok <= credit_r;
  level        <= count_r;

  -- pragma translate_off
  assert_proc: process (clk) is
  begin
    if rising_edge(clk) then
      if reset = '0' then
        assert not (push.valid = '1' and wr_ready = '0')
          report "defifo: push into a full de FIFO (de credit violated)"
          severity failure;
      end if;
    end if;
  end process;
  -- pragma translate_on

end rtl;
