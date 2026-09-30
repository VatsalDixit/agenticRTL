library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_int_pkg.all;

-- Streaming toplevel for vhsnunzip. This version of the decompressor doesn't
-- include any large-scale input and output stream buffering, so the streams
-- are limited to the speed of the decompression engine. However, because the
-- long-term memory isn't also used for buffering, the decompression engine
-- will never be internally bandwidth-starved, so decompression will be a bit
-- faster.
entity vhsnunzip_unbuffered is
  generic (

    -- Whether long chunks (>64kiB) should be supported. If this is disabled,
    -- the core will be a couple hundred LUTs smaller.
    LONG_CHUNKS : boolean := true;

    -- This block can use either 2 UltraRAMs or 16 Xilinx 36k block RAMs.
    -- Select "ultra" for UltraRAMs or "block" for block RAMs.
    RAM_STYLE   : string := "ultra"

  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    -- Compressed input stream. Each last-delimited packet is interpreted as a
    -- chunk of Snappy data as described by:
    --
    --   https://github.com/google/snappy/blob/
    --     b61134bc0a6a904b41522b4e5c9e80874c730cef/format_description.txt
    --
    -- This unit should be able to handle any Snappy chunk compressed with a
    -- history buffer of 64kiB or less (the default for Snappy is 32kiB).
    -- Copies from further back in history will result in garbage for that
    -- copy.
    --
    -- The input stream must be normalized; that is, all 8 bytes must be valid
    -- for all but the last transfer, and the last transfer must contain at
    -- least one byte. The number of valid bytes is indicated by cnt; 8 valid
    -- bytes is represented as 0 (implicit MSB). The LSB of the first transfer
    -- corresponds to the first byte in the chunk. This is compatible with the
    -- stream library components in vhlib.
    co_valid    : in  std_logic;
    co_ready    : out std_logic;
    co_data     : in  std_logic_vector(63 downto 0);
    co_cnt      : in  std_logic_vector(2 downto 0);
    co_last     : in  std_logic;

    -- Decompressed output stream. This stream is almost normalized, with the
    -- exception of the last transfer; the size of this transfer may be zero,
    -- even if the packet is non-empty. An empty line is signalled using cnt=0
    -- and dvalid=0. This is compatible with the stream library components in
    -- vhlib. If you need a fully normalized stream, you could add a
    -- StreamReshaper with element size 1 on both the input and the output.
    de_valid    : out std_logic;
    de_ready    : in  std_logic;
    de_dvalid   : out std_logic;
    de_data     : out std_logic_vector(127 downto 0);
    de_cnt      : out std_logic_vector(4 downto 0);
    de_last     : out std_logic

  );
end vhsnunzip_unbuffered;

architecture behavior of vhsnunzip_unbuffered is

  -- Pipeline interface signals.
  signal co           : compressed_stream_single;
  signal de           : decompressed_stream;
  signal lt_rd_valid  : std_logic;
  signal lt_rd_val_r  : std_logic;
  signal lt_rd_adev   : unsigned(11 downto 0);
  signal lt_rd_adod   : unsigned(11 downto 0);
  signal lt_rd_next   : std_logic;
  signal lt_rd_even   : byte_array(0 to 15);
  signal lt_rd_odd    : byte_array(0 to 15);

  -- RAM interface signals. The decompression history is stored as 16-byte
  -- lines, but each history RAM is 8 bytes wide, so a line is split over a
  -- *pair* of RAMs: pair 0 (instances 0 and 1) holds the even lines, pair 1
  -- (instances 2 and 3) holds the odd lines. Reading one line from each pair
  -- yields the 32-byte window that contains any 16-byte line plus its lookahead
  -- line, in exactly the way two 8-byte RAMs did for the 8-byte line.
  signal wr_ptr       : unsigned(11 downto 0);
  signal wr_push      : std_logic;
  signal ram_wr_cmd   : ram_command_array(0 to 3);
  signal ram_rd_cmd   : ram_command_array(0 to 3);
  signal ram_rd_resp  : ram_response_array(0 to 3);

begin

  -- Datapath.
  datapath_inst: vhsnunzip_pipeline
    generic map (
      LONG_CHUNKS => LONG_CHUNKS
    )
    port map (
      clk         => clk,
      reset       => reset,
      co          => co,
      co_ready    => co_ready,
      lt_rd_valid => lt_rd_valid,
      lt_rd_adev  => lt_rd_adev,
      lt_rd_adod  => lt_rd_adod,
      lt_rd_next  => lt_rd_next,
      lt_rd_even  => lt_rd_even,
      lt_rd_odd   => lt_rd_odd,
      de          => de,
      de_ready    => de_ready
    );

  -- To improve tool compatibility, avoid non-std_logic types on the toplevel.
  -- Also convert to/from vhlib's stream interface where applicable.
  co_connect_proc: process (co_valid, co_data, co_cnt, co_last) is
  begin
    co.valid <= co_valid;
    for byte in 0 to 7 loop
      co.data(byte) <= co_data(byte*8+7 downto byte*8);
    end loop;
    co.endi <= unsigned(co_cnt) - 1;
    co.last <= co_last;
  end process;

  de_connect_proc: process (de) is
  begin
    de_valid <= de.valid;
    for byte in 0 to 15 loop
      de_data(byte*8+7 downto byte*8) <= de.data(byte);
    end loop;
    de_cnt <= std_logic_vector(de.cnt);
    if de.cnt > 0 then
      de_dvalid <= '1';
    else
      de_dvalid <= '0';
    end if;
    de_last <= de.last;
  end process;

  -- Write the decompressed output to the memory for long-term history
  -- storage.
  -- A 16-byte line goes to one pair of RAMs, its low half in the first
  -- instance of the pair and its high half in the second.
  wr_push <= de.valid and de_ready;

  ram_wr_gen: for idx in 0 to 3 generate
    constant PAIR : natural := idx / 2;
    constant HALF : natural := idx mod 2;
    signal pair_sel : std_logic;
  begin
    pair_sel <= wr_ptr(0) when PAIR = 1 else not wr_ptr(0);
    ram_wr_cmd(idx) <= (
      valid => wr_push and pair_sel,
      addr  => resize(wr_ptr(11 downto 1), 12),
      wren  => '1',
      wdat  => de.data(HALF*8 to HALF*8+7),
      wctrl => "00000000");
  end generate;

  wr_ptr_proc: process (clk) is
  begin
    if rising_edge(clk) then
      if de.valid = '1' and de_ready = '1' then
        if de.last = '0' then
          wr_ptr <= wr_ptr + 1;
        else
          wr_ptr <= (others => '0');
        end if;
      end if;
      if reset = '1' then
        wr_ptr <= (others => '0');
      end if;
    end if;
  end process;

  -- Connect the long-term memory read request signals. Both halves of a pair
  -- always share an address, because they hold the two halves of one 16-byte
  -- history line.
  ram_rd_gen: for idx in 0 to 3 generate
    constant PAIR : natural := idx / 2;
    signal rd_addr : unsigned(11 downto 0);
  begin
    rd_addr <= lt_rd_adod when PAIR = 1 else lt_rd_adev;
    ram_rd_cmd(idx) <= (
      valid => lt_rd_valid,
      addr  => rd_addr,
      wren  => '0',
      wdat  => (others => X"00"),
      wctrl => "00000000");
  end generate;

  -- Two 8-byte halves make one 16-byte history line.
  lt_rd_even(0 to 7)   <= ram_rd_resp(0).rdat;
  lt_rd_even(8 to 15)  <= ram_rd_resp(1).rdat;
  lt_rd_odd(0 to 7)    <= ram_rd_resp(2).rdat;
  lt_rd_odd(8 to 15)   <= ram_rd_resp(3).rdat;

  lt_rd_next <= ram_rd_resp(3).valid_next;

  -- The four RAMs holding the decompression history.
  ram_gen: for idx in 0 to 3 generate
  begin
    ram_inst: vhsnunzip_ram
      generic map (
        RAM_STYLE => RAM_STYLE
      )
      port map (
        clk       => clk,
        reset     => reset,
        a_cmd     => ram_wr_cmd(idx),
        a_resp    => open,
        b_cmd     => ram_rd_cmd(idx),
        b_resp    => ram_rd_resp(idx)
      );
  end generate;

end behavior;
