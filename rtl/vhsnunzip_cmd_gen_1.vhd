library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_int_pkg.all;

-- Decompression datapath command generator stage 1.
entity vhsnunzip_cmd_gen_1 is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    -- Element information input stream.
    el          : in  element_stream;
    el_ready    : out std_logic;

    -- Command output stream.
    c1          : out partial_command_stream;
    c1_ready    : in  std_logic

  );
end vhsnunzip_cmd_gen_1;

architecture behavior of vhsnunzip_cmd_gen_1 is

  -- Simulation-only validation switch; see the hook in the process below.
  constant TEST_SP : boolean := false;

begin
  proc: process (clk) is

    -- Input holding register.
    variable elh    : element_stream := ELEMENT_STREAM_INIT;

    -- Remaining copy length, diminished-one. The sign bit is an inverted
    -- validity bit.
    variable cp_rem : signed(6 downto 0) := (others => '1');

    -- Replication period selector for power-of-two self-overlapping copies.
    variable rep    : unsigned(1 downto 0);

    -- Self-pair: the continuation of a clamped self-overlapping copy, issued
    -- in the same command through the second copy slot. sp_len is its length,
    -- diminished-one; the sign bit is an inverted validity bit.
    variable sp_len : signed(6 downto 0);
    variable sp_bnd : signed(6 downto 0);
    variable sp_val : std_logic;

    -- Total number of copy bytes issued this command.
    variable used   : signed(6 downto 0);

    -- Output holding register.
    variable c1h    : partial_command_stream := PARTIAL_COMMAND_STREAM_INIT;

  begin
    if rising_edge(clk) then

      -- Invalidate the output register if it was shifted out.
      if c1_ready = '1' then
        c1h.valid := '0';
      end if;

      -- Shift new data into the input when we can.
      if elh.valid = '0' then
        elh := el;
        if elh.valid = '1' then
          assert cp_rem = "1111111";
        end if;
        if elh.valid = '1' and elh.cp_val = '1' then
          cp_rem := signed(resize(elh.cp_len, 7));
        end if;
      end if;

      -- Decode when we have valid data and have room for the result.
      if elh.valid = '1' and c1h.valid = '0' then
        c1h.valid := '1';

        -- Record the current copy offset for the partial command. We might
        -- change it to increase the amount of bytes we can copy per cycle in
        -- a run-length-encoded copy.
        c1h.cp_off := elh.cp_off;

        -- Determine how many bytes we can write for the copy element. If there
        -- is no copy element, this becomes 0 automatically.
        if cp_rem < 16 then
          c1h.cp_len := cp_rem(4 downto 0);
        else
          c1h.cp_len := "01111";
        end if;

        -- Power-of-two self-overlap acceleration. A copy whose offset is 2, 4 or
        -- 8 produces its source pattern repeated with that period, and the
        -- datapath can take a lane index modulo 2, 4 or 8 for free (all three
        -- divide the eight-way lane-pair rotation, so both halves of the output
        -- line still want the same lane per pair). Such a copy therefore needs
        -- neither the clamp below nor the offset doubling that goes with it: it
        -- writes a full 16-byte line per command just like a non-overlapping
        -- copy, where before a 16-byte copy at offset 2 cost four commands
        -- (2 + 4 + 8 + 2 bytes) and one at offset 8 cost two. Offsets of 0 and 1
        -- keep the existing run-length path; 3, 5, 6, 7 and 9..15 cannot use this
        -- path at all, because a period that does not divide eight would need a
        -- per-destination-byte rotation rather than a per-lane-pair one (bytes b
        -- and b+8 would want source lanes that differ modulo eight, and the
        -- rotator shares one lane-pair index between the two halves of the line).
        -- Those offsets take the clamp below, which pairs them with themselves at
        -- the doubled offset instead.
        rep := "00";
        if elh.cp_off(15 downto 4) = 0 and c1h.cp_len(4) = '0' then
          case std_logic_vector(elh.cp_off(3 downto 0)) is
            when "0010" => rep := "01";
            when "0100" => rep := "10";
            when "1000" => rep := "11";
            when others => rep := "00";
          end case;
        end if;
        c1h.cp_rep := rep;

        sp_val := '0';
        sp_len := (others => '1');

        if elh.cp_off <= 1 then

          -- Special case for single-byte repetition, since it's relatively
          -- common and otherwise has worst-case 1-byte/cycle performance.
          -- Requires some extra logic in the address/rotation decoders
          -- though; cp_rol becomes an index rather than a rotation when
          -- cp_rle is set. Can be disabled by just not taking this branch.
          c1h.cp_rle := '1';
          c1h.cp_rep := "00";

        else

          -- Without run-length=1 acceleration or the power-of-two replication
          -- above, we can't copy more bytes at once than the copy offset,
          -- because we'd be reading beyond what we've written already.
          if rep = "00"
             and unsigned(c1h.cp_len(3 downto 0)) >= elh.cp_off
             and c1h.cp_len(4) = '0' then
            c1h.cp_len(3 downto 0) := signed(resize(elh.cp_off(4 downto 0) - 1, 4));

            -- Self-pair. The doubling below is not only good for the *next*
            -- command: the doubled offset is already usable in *this* one, for
            -- the bytes behind the cp_off bytes the clamp just allowed. Write
            -- P = cp_off for the destination bytes [base, base+P) at offset P
            -- (that is the clamp above), and the destination bytes
            -- [base+P, base+P+L2) at offset 2P. The latter read
            -- [base-P, base-P+L2), which is the copy's own original source
            -- window: entirely before base, so written in an earlier cycle and
            -- never a byte this command produces, and entirely at or after
            -- base-P, so still inside the P-byte pattern that the copy
            -- replicates. Both conditions together are L2 <= P. The datapath
            -- already has a second copy slot with its own short-term read port,
            -- its own rotation and its own lane-pair select (it is what the
            -- decoder's copy pairs use), and that slot is always free here,
            -- because the decoder only pairs copies whose offset is at least 16
            -- and which therefore never reach this clamp. So a self-overlapping
            -- copy of arbitrary offset gets 2*cp_off bytes per command instead
            -- of cp_off, which halves the number of commands the doubling
            -- sequence needs: offset 3 goes 6, 12, 16, ... rather than 3, 6, 12,
            -- 16, ..., and any offset of 9..15 now writes a full 16-byte line in
            -- one command rather than two (P + (16-P)).
            --
            -- The length of the second part is bounded by
            --  - P, the pattern length, as derived above;
            --  - 16 - P, what is left of the 16-byte line the command writes;
            --  - what remains of the copy. One byte more than that is kept back
            --    when the record still carries a literal, a literal-first flag
            --    or a copy pair of its own, because those ride on the command
            --    that retires the record and would clash with this slot (the
            --    literal and the second copy share the rotator) -- keeping a
            --    byte back means this command cannot be that one.
            if elh.cp_off(3 downto 0) <= 8 then
              sp_bnd := signed(resize(elh.cp_off(3 downto 0), 7)) - 1;
            else
              sp_bnd := 15 - signed(resize(elh.cp_off(3 downto 0), 7));
            end if;
            sp_len := cp_rem - signed(resize(elh.cp_off(3 downto 0), 7));
            if (elh.li_val or elh.sw_val or elh.cp2_val) = '1' then
              sp_len := sp_len - 1;
            end if;
            if sp_len > sp_bnd then
              sp_len := sp_bnd;
            end if;
            sp_val := not sp_len(6);

            -- We can however accelerate subsequent copies; after the first
            -- copy we have two consecutive copies in memory, after the
            -- second we have four, and so on. Note that cp_off bit 4 and above
            -- must be zero here, because len was larger and len can be at most
            -- 16, so we can ignore them in the leftshift. This is also exactly
            -- the offset the self-pair above reads at, so the second copy slot
            -- takes its offset from here.
            elh.cp_off(4 downto 0) := elh.cp_off(3 downto 0) & "0";

          end if;

          c1h.cp_rle := '0';

        end if;

        -- pragma translate_off
        -- Validation hook: split every copy that fits one command and has room
        -- to spare between the two copy slots, at the copy's own offset. The
        -- second half then reads the very same already-written source window the
        -- first half reads, just further along, so the output must stay
        -- bit-identical while the whole second-slot path that the self-pair above
        -- uses is exercised: the slot being filled from cmd_gen_1 rather than
        -- from the decoder, the byte accounting below, the record retiring on a
        -- paired command, chunk boundaries, and cmd_gen_2's short- and long-term
        -- addressing for a second copy whose offset is the first one's. The
        -- offset test (strictly more than the whole copy) is what keeps the
        -- second half from reading a byte this command writes.
        -- Simulation only; not part of the synthesised design.
        if TEST_SP and sp_val = '0' and c1h.cp_len(4) = '0' and c1h.cp_len >= 1
           and c1h.cp_rle = '0' and c1h.cp_rep = "00"
           and (elh.li_val or elh.sw_val or elh.cp2_val) = '0'
           and elh.cp_off > resize(unsigned(c1h.cp_len(3 downto 0)) + 1, 16) then
          sp_len := shift_right(resize(c1h.cp_len, 7) + 1, 1) - 1;
          c1h.cp_len := c1h.cp_len - resize(sp_len(4 downto 0), 5) - 1;
          sp_val := '1';
        end if;
        -- pragma translate_on

        -- Update state. The self-paired second copy takes its bytes from the
        -- same copy element, so it counts against the same remainder.
        used := resize(c1h.cp_len, 7) + 1;
        if sp_val = '1' then
          used := used + sp_len + 1;
        end if;
        cp_rem := cp_rem - used;

        -- Advance if there are no (more) bytes in the copy.
        if cp_rem(6) = '1' then
          elh.valid := '0';
          -- The decoder only pairs copies that need no splitting at all (the
          -- first copy's offset is at least 16 and the two lengths add up to at
          -- most 16), so a paired record always takes this branch on its first
          -- cycle and the second copy is passed straight on.
          c1h.li_val := elh.li_val;
          c1h.sw_val := elh.sw_val;
          c1h.ld_pop := elh.ld_pop;
          c1h.last := elh.last;
        else
          c1h.li_val := '0';
          c1h.sw_val := '0';
          c1h.ld_pop := '0';
          c1h.last := '0';
        end if;

        -- The second copy slot carries either the self-paired continuation of
        -- this very copy (at the doubled offset) or the decoder's own paired
        -- second copy; never both, because the decoder only pairs copies whose
        -- offset is at least 16 and those never reach the clamp that makes a
        -- self-pair. A record that still owes a literal, a literal-first flag or
        -- a paired copy keeps a byte back above, so the self-pair never shares a
        -- command with any of them.
        if sp_val = '1' then
          c1h.cp2_val := '1';
          c1h.cp2_off := elh.cp_off;
          c1h.cp2_len := unsigned(sp_len(3 downto 0));
        else
          c1h.cp2_val := elh.cp2_val and cp_rem(6);
          c1h.cp2_off := elh.cp2_off;
          c1h.cp2_len := elh.cp2_len;
        end if;
        c1h.li_off := elh.li_off;
        c1h.li_len := elh.li_len;

      end if;

      -- Handle reset.
      if reset = '1' then
        elh.valid := '0';
        c1h.valid := '0';
        cp_rem := (others => '1');
      end if;

      -- Assign outputs.
      el_ready <= not elh.valid;
      c1 <= c1h;

    end if;
  end process;
end behavior;
