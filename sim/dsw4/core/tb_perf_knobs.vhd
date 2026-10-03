-- Throughput testbench for vhsnunzip_unbuffered. Width-generic.
--
-- The loop measures every candidate design with THIS file, never with a copy
-- inside the candidate's worktree, so a candidate cannot change how it is
-- measured. The port widths are generics; the harness reads them out of the
-- candidate's RTL and passes them in. A candidate may therefore widen the
-- ports freely as long as it keeps the port NAMES and the count convention:
--
--   co_cnt / de_cnt   the number of valid bytes in the transfer. If the count
--                     field is exactly log2(width) bits wide, a count of zero
--                     means "all bytes valid" (the original 8-byte convention).
--                     If it is wider than that, the count is literal.
--   de_dvalid         '1' when the transfer carries at least one byte.
--
-- Stimulus: cs.tv in the current directory. One line per 8-byte granule:
-- 64 data bits (byte 0 first, MSB first inside a byte), then last(1), then
-- endi(3) = index of the last valid byte. The source packs CO_BYTES/8
-- granules into one transfer and never crosses a chunk boundary.
--
-- Output: out.hex, one line of hex per accepted transfer (framing discarded)
-- and a line "EOC" at the end of each chunk. perf.txt holds the counters.

library std;
use std.textio.all;

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;

entity vhsnunzip_perf_tc is
  generic (
    CO_BYTES      : positive := 8;
    CO_CNT_BITS   : positive := 3;
    DE_BYTES      : positive := 8;
    DE_CNT_BITS   : positive := 4;
    SRC_PCT       : natural  := 100;
    SNK_PCT       : natural  := 100;
    TEST_SLOTS    : natural  := 4;
    TEST_CUT      : boolean  := false;
    TEST_NOREP    : boolean  := false;
    TEST_LITP1    : boolean  := false;
    TEST_RETGT    : boolean  := false;
    TEST_ST_LINES : natural  := 32;
    TEST_STALL_PCT: natural  := 0;
    EXPECT_CHUNKS : natural  := 0
  );
end vhsnunzip_perf_tc;

architecture testcase of vhsnunzip_perf_tc is

  constant GRANULES    : positive := CO_BYTES / 8;
  constant CO_IMPLICIT : boolean  := (2**CO_CNT_BITS = CO_BYTES);
  constant DE_IMPLICIT : boolean  := (2**DE_CNT_BITS = DE_BYTES);

  signal clk        : std_logic := '0';
  signal reset      : std_logic := '1';
  signal done       : boolean := false;
  signal perf_stop  : boolean := false;

  signal co_valid   : std_logic := '0';
  signal co_ready   : std_logic := '0';
  signal co_data    : std_logic_vector(CO_BYTES*8-1 downto 0) := (others => '0');
  signal co_cnt     : std_logic_vector(CO_CNT_BITS-1 downto 0) := (others => '0');
  signal co_last    : std_logic := '0';

  signal de_valid   : std_logic := '0';
  signal de_ready   : std_logic := '0';
  signal de_data    : std_logic_vector(DE_BYTES*8-1 downto 0) := (others => '0');
  signal de_cnt     : std_logic_vector(DE_CNT_BITS-1 downto 0) := (others => '0');
  signal de_dvalid  : std_logic := '0';
  signal de_last    : std_logic := '0';

  -- Valid bytes in an output transfer under either count convention.
  function out_bytes(cnt : std_logic_vector; dvalid : std_logic) return natural is
    variable n : natural;
  begin
    n := to_integer(unsigned(cnt));
    if DE_IMPLICIT then
      if dvalid = '0' then
        return 0;
      end if;
      if n = 0 then
        return DE_BYTES;
      end if;
    end if;
    assert n <= DE_BYTES
      report "de_cnt exceeds the output port width" severity failure;
    return n;
  end function;

  -- Cycles without an output handshake before the run is declared dead.
  -- The longest real draw takes about 35k cycles in total; a stall this long
  -- means the pipeline has deadlocked, and failing here is much faster than
  -- waiting for the --stop-time guard.
  constant WATCHDOG_CYCLES : natural := 300000;

begin

  assert CO_BYTES mod 8 = 0
    report "CO_BYTES must be a multiple of 8" severity failure;

  uut: entity work.vhsnunzip_unbuffered
    generic map (
      TEST_SLOTS => TEST_SLOTS,
      TEST_CUT => TEST_CUT,
      TEST_NOREP => TEST_NOREP,
      TEST_LITP1 => TEST_LITP1,
      TEST_RETGT => TEST_RETGT,
      TEST_ST_LINES => TEST_ST_LINES,
      TEST_STALL_PCT => TEST_STALL_PCT
    )
    port map (
      clk           => clk,
      reset         => reset,
      co_valid      => co_valid,
      co_ready      => co_ready,
      co_data       => co_data,
      co_cnt        => co_cnt,
      co_last       => co_last,
      de_valid      => de_valid,
      de_ready      => de_ready,
      de_dvalid     => de_dvalid,
      de_data       => de_data,
      de_cnt        => de_cnt,
      de_last       => de_last
    );

  clk_proc: process is
  begin
    wait for 500 ps;
    clk <= '0';
    wait for 500 ps;
    clk <= '1';
    if done then
      wait;
    end if;
  end process;

  reset_proc: process is
  begin
    reset <= '1';
    wait until rising_edge(clk);
    wait until rising_edge(clk);
    wait until rising_edge(clk);
    reset <= '0';
    wait;
  end process;

  source_proc: process is
    file     fil   : text;
    variable lin   : line;
    variable s1    : positive := 1;
    variable s2    : positive := 1;
    variable rnd   : real;
    variable vec   : std_logic_vector(CO_BYTES*8-1 downto 0);
    variable tot   : natural;
    variable lst   : std_logic;
    variable endi  : natural;
    variable base  : integer;
    variable ch    : character;
  begin
    file_open(fil, "cs.tv", read_mode);
    co_valid <= '0';

    wait until reset = '0';
    wait until rising_edge(clk);

    while not endfile(fil) loop

      loop
        uniform(s1, s2, rnd);
        exit when rnd * 100.0 < real(SRC_PCT);
        wait until rising_edge(clk);
      end loop;

      vec := (others => '0');
      tot := 0;
      lst := '0';
      for k in 0 to GRANULES-1 loop
        if k = 0 or lst = '0' then
          readline(fil, lin);
          assert lin'length >= 68
            report "cs.tv: line shorter than 68 characters" severity failure;
          base := lin.all'left;
          for byte in 0 to 7 loop
            for bit in 0 to 7 loop
              ch := lin.all(base + byte*8 + bit);
              if ch = '1' then
                vec(k*64 + byte*8 + (7 - bit)) := '1';
              end if;
            end loop;
          end loop;
          lst := '0';
          if lin.all(base + 64) = '1' then
            lst := '1';
          end if;
          endi := 0;
          for bit in 0 to 2 loop
            if lin.all(base + 65 + bit) = '1' then
              endi := endi + 2**(2 - bit);
            end if;
          end loop;
          tot := k*8 + endi + 1;
        end if;
      end loop;

      co_data <= vec;
      if CO_IMPLICIT then
        co_cnt <= std_logic_vector(to_unsigned(tot mod CO_BYTES, CO_CNT_BITS));
      else
        co_cnt <= std_logic_vector(to_unsigned(tot, CO_CNT_BITS));
      end if;
      co_last <= lst;

      co_valid <= '1';
      loop
        wait until rising_edge(clk);
        exit when co_ready = '1';
      end loop;
      co_valid <= '0';

    end loop;
    file_close(fil);
    wait;
  end process;

  sink_proc: process is
    file     outf   : text;
    variable olin   : line;
    variable s1     : positive := 2;
    variable s2     : positive := 2;
    variable rnd    : real;
    variable chunks : natural := 0;
    variable n      : natural;
  begin
    done <= false;
    assert EXPECT_CHUNKS > 0
      report "EXPECT_CHUNKS generic must be set" severity failure;

    file_open(outf, "out.hex", write_mode);
    de_ready <= '0';

    wait until reset = '0';
    wait until rising_edge(clk);

    loop
      exit when chunks >= EXPECT_CHUNKS;

      loop
        uniform(s1, s2, rnd);
        exit when rnd * 100.0 < real(SNK_PCT);
        wait until rising_edge(clk);
      end loop;

      de_ready <= '1';
      loop
        wait until rising_edge(clk);
        exit when de_valid = '1';
      end loop;
      de_ready <= '0';

      n := out_bytes(de_cnt, de_dvalid);
      for byte in 0 to DE_BYTES-1 loop
        if byte < n then
          write(olin, to_hstring(de_data(byte*8+7 downto byte*8)));
        end if;
      end loop;
      writeline(outf, olin);
      if de_last = '1' then
        write(olin, string'("EOC"));
        writeline(outf, olin);
        chunks := chunks + 1;
      end if;

      if n = 0 then
        assert de_dvalid = '0'
          report "de_dvalid set on an empty transfer" severity failure;
      else
        assert de_dvalid = '1'
          report "de_dvalid clear on a non-empty transfer" severity failure;
      end if;

    end loop;
    file_close(outf);

    perf_stop <= true;

    de_ready <= '1';
    for i in 0 to 100 loop
      wait until rising_edge(clk);
      exit when de_valid = '1';
    end loop;
    de_ready <= '0';

    assert de_valid = '0' report "spurious data after the last chunk!" severity failure;

    done <= true;
    wait;
  end process;

  perf_proc: process is
    file     fil        : text;
    variable lin        : line;
    variable cycles     : natural := 0;
    variable bytes_out  : natural := 0;
    variable co_beats   : natural := 0;
    variable de_beats   : natural := 0;
    variable co_stall   : natural := 0;
    variable de_bubble  : natural := 0;
    variable idle_run   : natural := 0;
  begin
    wait until reset = '0';

    loop
      wait until rising_edge(clk);
      exit when perf_stop;

      cycles := cycles + 1;

      if de_valid = '1' and de_ready = '1' then
        idle_run := 0;
      else
        idle_run := idle_run + 1;
        assert idle_run < WATCHDOG_CYCLES
          report "DEADLOCK: no output handshake for " & integer'image(WATCHDOG_CYCLES) & " cycles"
          severity failure;
      end if;

      if co_valid = '1' then
        if co_ready = '1' then
          co_beats := co_beats + 1;
        else
          co_stall := co_stall + 1;
        end if;
      end if;

      if de_valid = '1' then
        if de_ready = '1' then
          de_beats := de_beats + 1;
          bytes_out := bytes_out + out_bytes(de_cnt, de_dvalid);
        end if;
      else
        de_bubble := de_bubble + 1;
      end if;
    end loop;

    file_open(fil, "perf.txt", write_mode);
    write(lin, string'("cycles="));    write(lin, cycles);    writeline(fil, lin);
    write(lin, string'("bytes_out=")); write(lin, bytes_out); writeline(fil, lin);
    write(lin, string'("co_beats="));  write(lin, co_beats);  writeline(fil, lin);
    write(lin, string'("de_beats="));  write(lin, de_beats);  writeline(fil, lin);
    write(lin, string'("co_stall="));  write(lin, co_stall);  writeline(fil, lin);
    write(lin, string'("de_bubble=")); write(lin, de_bubble); writeline(fil, lin);
    file_close(fil);

    wait;
  end process;

end testcase;
