library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 CO FIFO (SPEC 3.1): 32-byte compressed beats + last + endi(4:0),
-- 2**DEPTH_LOG2 deep (default 16), SRL-backed fall-through FIFO
-- (vhsnunzip_fifo, i35's idiom). 256 + 6 bits per entry.
--
--   wr / wr_ready  beat stream in (from the core's co port). A beat is taken
--                  at the clock edge when wr.valid and wr_ready.
--   rd / rd_ready  head beat out, fall-through: rd.valid = not empty, and the
--                  head is presented in the cycle after it was written. The
--                  head is popped at the clock edge when rd.valid and
--                  rd_ready (vhsnunzip_cbuf drives rd_ready = its co_ready).
--   level          diminished-one occupancy (vhsnunzip_fifo convention).
entity vhsnunzip_cofifo is
  generic (
    DEPTH_LOG2  : natural := CO_DEPTH_LOG2
  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    wr          : in  cbeat_t;
    wr_ready    : out std_logic;

    rd          : out cbeat_t;
    rd_ready    : in  std_logic;

    level       : out unsigned(DEPTH_LOG2 downto 0)
  );
end vhsnunzip_cofifo;

architecture behavior of vhsnunzip_cofifo is
  signal wr_ctrl  : std_logic_vector(5 downto 0);
  signal rd_ctrl  : std_logic_vector(5 downto 0);
  signal rd_valid : std_logic;
  signal rd_data  : byte_array(0 to 31);
begin

  wr_ctrl <= wr.last & std_logic_vector(wr.endi);

  fifo_inst: entity work.vhsnunzip_fifo
    generic map (
      DATA_WIDTH  => 32,
      CTRL_WIDTH  => 6,
      DEPTH_LOG2  => DEPTH_LOG2
    )
    port map (
      clk         => clk,
      reset       => reset,
      wr_valid    => wr.valid,
      wr_ready    => wr_ready,
      wr_data     => wr.data,
      wr_ctrl     => wr_ctrl,
      rd_valid    => rd_valid,
      rd_ready    => rd_ready,
      rd_data     => rd_data,
      rd_ctrl     => rd_ctrl,
      level       => level,
      empty       => open,
      full        => open
    );

  rd.valid <= rd_valid;
  rd.last  <= rd_ctrl(5);
  rd.endi  <= unsigned(rd_ctrl(4 downto 0));
  rd.data  <= rd_data;

end behavior;
