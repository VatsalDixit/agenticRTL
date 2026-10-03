library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;
use work.vhsnunzip_int_pkg.vhsnunzip_ram;

-- Streaming toplevel for vhsnunzip. This version of the decompressor doesn't
-- include any large-scale input and output stream buffering, so the streams
-- are limited to the speed of the decompression engine. However, because the
-- long-term memory isn't also used for buffering, the decompression engine
-- will never be internally bandwidth-starved, so decompression will be a bit
-- faster.
entity vhsnunzip_unbuffered is
  generic (

    -- Unused since DSW-4 (kept for the component declaration in
    -- vhsnunzip_pkg): long chunks (>64kiB) are always supported.
    LONG_CHUNKS : boolean := true;

    -- This block uses 32 UltraRAMs or 32 x 8 Xilinx 36k block RAMs.
    -- Select "ultra" for UltraRAMs or "block" for block RAMs.
    RAM_STYLE   : string := "ultra";

    -- Simulation-only test knobs (SPEC 7), all off by default; every one
    -- must give bit-identical output. See vhsnunzip_core.
    TEST_SLOTS     : natural := 4;
    TEST_CUT       : boolean := false;
    TEST_NOREP     : boolean := false;
    TEST_LITP1     : boolean := false;
    TEST_RETGT     : boolean := false;
    TEST_ST_LINES  : natural := 32;
    TEST_STALL_PCT : natural := 0;
    TEST_PROBE     : boolean := false

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
    -- The input stream must be normalized; that is, all 32 bytes must be
    -- valid for all but the last transfer, and the last transfer must contain
    -- at least one byte. Each chunk starts on a new transfer. The number of
    -- valid bytes is indicated by cnt; 32 valid bytes is represented as 0
    -- (implicit MSB). The LSB of the first transfer corresponds to the first
    -- byte in the chunk. This is compatible with the stream library
    -- components in vhlib.
    co_valid    : in  std_logic;
    co_ready    : out std_logic;
    co_data     : in  std_logic_vector(255 downto 0);
    co_cnt      : in  std_logic_vector(4 downto 0);
    co_last     : in  std_logic;

    -- Decompressed output stream, 32 bytes per transfer. Valid bytes are
    -- packed from the LSB; cnt is the literal number of valid bytes (0..32)
    -- and dvalid is set when cnt is nonzero. This stream is almost
    -- normalized, with the exception of the last transfer; the size of this
    -- transfer may be zero, even if the packet is non-empty. There is exactly
    -- one last transfer per chunk, and an empty chunk is signalled using a
    -- single transfer with cnt=0, dvalid=0 and last=1. This is compatible with
    -- the stream library components in vhlib.
    de_valid    : out std_logic;
    de_ready    : in  std_logic;
    de_dvalid   : out std_logic;
    de_data     : out std_logic_vector(255 downto 0);
    de_cnt      : out std_logic_vector(5 downto 0);
    de_last     : out std_logic

  );
end vhsnunzip_unbuffered;

architecture behavior of vhsnunzip_unbuffered is

  -- Core interface.
  signal co           : cbeat_t;
  signal de           : decompressed_stream;

  -- History RAMs: 32 instances, index i = m*8 + par*4 + w (SPEC 6): mirror
  -- m = writer slot 0..3, par = output-line parity, w = 8-byte word of the
  -- 32-byte line. Port a = history write, port b = LT read.
  signal ram_a_cmd    : ram_command_array(0 to 31);
  signal ram_a_resp   : ram_response_array(0 to 31);
  signal ram_b_cmd    : ram_command_array(0 to 31);
  signal ram_b_resp   : ram_response_array(0 to 31);

begin

  -- Input beat: byte i = co_data(8i+7 downto 8i); co_cnt 0 means 32 bytes,
  -- so the index of the last valid byte is co_cnt - 1 (mod 32).
  co.valid <= co_valid;
  co.last  <= co_last;
  co.endi  <= unsigned(co_cnt) - 1;
  co_data_gen: for i in 0 to 31 generate
    co.data(i) <= co_data(8*i+7 downto 8*i);
  end generate;

  core_inst: entity work.vhsnunzip_core
    generic map (
      TEST_SLOTS     => TEST_SLOTS,
      TEST_CUT       => TEST_CUT,
      TEST_NOREP     => TEST_NOREP,
      TEST_LITP1     => TEST_LITP1,
      TEST_RETGT     => TEST_RETGT,
      TEST_ST_LINES  => TEST_ST_LINES,
      TEST_STALL_PCT => TEST_STALL_PCT,
      TEST_PROBE     => TEST_PROBE
    )
    port map (
      clk         => clk,
      reset       => reset,
      co          => co,
      co_ready    => co_ready,
      de          => de,
      de_ready    => de_ready,
      ram_a_cmd   => ram_a_cmd,
      ram_a_resp  => ram_a_resp,
      ram_b_cmd   => ram_b_cmd,
      ram_b_resp  => ram_b_resp
    );

  -- Output line: valid bytes packed from lane 0, cnt literal 0..32.
  de_valid  <= de.valid;
  de_last   <= de.last;
  de_cnt    <= std_logic_vector(de.cnt);
  de_dvalid <= '0' when de.cnt = 0 else '1';
  de_data_gen: for i in 0 to 31 generate
    de_data(8*i+7 downto 8*i) <= de.data(i);
  end generate;

  -- The RAMs holding the decompression history (4 mirrors x 2 parities x
  -- 4 words).
  ram_gen: for idx in 0 to 31 generate
  begin
    ram_inst: vhsnunzip_ram
      generic map (
        RAM_STYLE => RAM_STYLE
      )
      port map (
        clk       => clk,
        reset     => reset,
        a_cmd     => ram_a_cmd(idx),
        a_resp    => ram_a_resp(idx),
        b_cmd     => ram_b_cmd(idx),
        b_resp    => ram_b_resp(idx)
      );
  end generate;

end behavior;
