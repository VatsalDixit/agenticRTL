library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_int_pkg.all;

-- AXI-stream FIFO using byte_array data.
--
-- The storage used to be a shift-on-write shift register (vhsnunzip_srl) read
-- through a combinational address mux driven by the fill level. That mux is
-- DEPTH_LOG2 levels of logic in front of *everything* downstream of the FIFO,
-- and downstream of the command FIFO in particular sits the entire stage-1
-- thermometer/lookahead/short-term-address cloud, while downstream of the
-- decompressed data FIFO sits the toplevel output port and the history RAM
-- write data. So instead we shift on *read*, towards slot 0, and write into a
-- decoded slot index. Slot 0 is then always the oldest entry, and the read
-- port is a plain flip-flop output with no logic in front of it at all.
--
-- The level counter, the empty/full/valid/ready semantics and the write-to-
-- read latency are exactly as they were, so this is cycle-for-cycle identical
-- to the old implementation.
entity vhsnunzip_fifo is
  generic (

    -- Data port width in bytes.
    DATA_WIDTH  : natural := 0;

    -- Control port width in bits.
    CTRL_WIDTH  : natural := 0;

    -- log2 of the memory depth.
    DEPTH_LOG2  : natural := 5

  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    -- Write data input stream.
    wr_valid    : in  std_logic;
    wr_ready    : out std_logic;
    wr_data     : in  byte_array(0 to DATA_WIDTH-1) := (others => X"00");
    wr_ctrl     : in  std_logic_vector(CTRL_WIDTH-1 downto 0) := (others => '0');

    -- Read data output stream.
    rd_valid    : out std_logic;
    rd_ready    : in  std_logic;
    rd_data     : out byte_array(0 to DATA_WIDTH-1);
    rd_ctrl     : out std_logic_vector(CTRL_WIDTH-1 downto 0);

    -- FIFO level. This is diminished-one-encoded! That is, -1 is empty, 0 is
    -- one valid entry, etc.
    level       : out unsigned(DEPTH_LOG2 downto 0);

    -- Empty and full status signals, derived from level.
    empty       : out std_logic;
    full        : out std_logic

  );
end vhsnunzip_fifo;

architecture behavior of vhsnunzip_fifo is

  -- Number of storage slots.
  constant DEPTH  : natural := 2**DEPTH_LOG2;

  -- Internal copy of the FIFO level, see level port for more info.
  signal level_s  : unsigned(DEPTH_LOG2 downto 0) := (others => '1');

  -- Registered copy of level_s + 1, which is the number of entries currently
  -- in the FIFO. Precomputed in a register so that the write slot index below
  -- is a 2:1 mux between two flip-flop outputs rather than an incrementer in
  -- front of the write address decoder.
  signal level_p1 : unsigned(DEPTH_LOG2 downto 0) := (others => '0');

  -- Internal copies of the empty and full status signals.
  signal empty_s  : std_logic;
  signal full_s   : std_logic := '0';

  -- Write- and read enable signals. These assert when an AXI handshake
  -- completes.
  signal wr_ena   : std_logic;
  signal rd_ena   : std_logic;

  -- Slot index that a new entry is written into. This is the number of entries
  -- that will still be in the FIFO after the read (if any) of this cycle.
  signal wr_idx   : unsigned(DEPTH_LOG2-1 downto 0);

  -- Concatenated versions of the wr_data and rd_data byte arrays.
  constant WIDTH  : natural := DATA_WIDTH*8 + CTRL_WIDTH;
  signal wr_data_concat : std_logic_vector(WIDTH-1 downto 0);
  signal rd_data_concat : std_logic_vector(WIDTH-1 downto 0);

  -- The storage itself. Slot 0 holds the oldest entry, i.e. the one presented
  -- on the read port.
  type slot_array is array (natural range <>) of std_logic_vector(WIDTH-1 downto 0);
  signal slots    : slot_array(0 to DEPTH-1) := (others => (others => '0'));

begin

  reg_proc: process (clk) is
    variable level_v  : unsigned(DEPTH_LOG2 downto 0);
  begin
    if rising_edge(clk) then

      -- Update the counter.
      level_v := level_s;
      if rd_ena = '1' then
        level_v := level_v - 1;
      end if;
      if wr_ena = '1' then
        level_v := level_v + 1;
      end if;
      level_s <= level_v;
      level_p1 <= level_v + 1;

      -- Precompute the full signal and store it in a register.
      if level_v = 2**DEPTH_LOG2-1 then
        full_s <= '1';
      else
        full_s <= '0';
      end if;

      -- Handle reset.
      if reset = '1' then
        level_s <= (others => '1');
        level_p1 <= (others => '0');
        full_s <= '0';
      end if;

    end if;
  end process;

  -- The empty signal is just the MSB of the FIFO level.
  empty_s <= level_s(DEPTH_LOG2);

  -- Handle the AXI-stream handshakes.
  wr_ready <= not full_s;
  wr_ena <= wr_valid and not full_s;

  rd_valid <= not empty_s;
  rd_ena <= rd_ready and not empty_s;

  -- Determine which slot a new entry goes into. With N entries in the FIFO
  -- before this cycle's transfers, the new entry belongs at index N when
  -- nothing is read and at index N-1 when something is read (because the
  -- read shifts everything one slot down). N is level_s + 1, which is
  -- available as a register, and N-1 is level_s itself, so this is a plain
  -- 2:1 mux between two flip-flop outputs.
  wr_idx <= level_s(DEPTH_LOG2-1 downto 0) when rd_ena = '1'
       else level_p1(DEPTH_LOG2-1 downto 0);

  -- Storage. On a read everything shifts one slot towards slot 0; a write
  -- overrides the shift for the one slot it targets (the shifted-in value
  -- there would be an invalid entry anyway).
  slot_proc: process (clk) is
  begin
    if rising_edge(clk) then
      for i in 0 to DEPTH-1 loop
        if wr_ena = '1' and to_integer(wr_idx) = i then
          slots(i) <= wr_data_concat;
        elsif rd_ena = '1' and i < DEPTH-1 then
          slots(i) <= slots(i+1);
        end if;
      end loop;
    end if;
  end process;

  rd_data_concat <= slots(0);

  -- Pack/unpack the data vectors.
  pack_proc: process (wr_data, wr_ctrl) is
  begin
    if CTRL_WIDTH > 0 then
      wr_data_concat(CTRL_WIDTH-1 downto 0) <= wr_ctrl;
    end if;
    for i in 0 to DATA_WIDTH-1 loop
      wr_data_concat(8*i+7+CTRL_WIDTH downto 8*i+CTRL_WIDTH) <= wr_data(i);
    end loop;
  end process;

  unpack_proc: process (rd_data_concat) is
  begin
    if CTRL_WIDTH > 0 then
      rd_ctrl <= rd_data_concat(CTRL_WIDTH-1 downto 0);
    end if;
    for i in 0 to DATA_WIDTH-1 loop
      rd_data(i) <= rd_data_concat(8*i+7+CTRL_WIDTH downto 8*i+CTRL_WIDTH);
    end loop;
  end process;

  -- Forward the internal signals.
  level <= level_s;
  empty <= empty_s;
  full <= full_s;

end behavior;
