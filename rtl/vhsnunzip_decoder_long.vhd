library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_int_pkg.all;

-- Snappy element decoder with support for chunks > 64kiB.
--
-- The offset recurrence used to be one long serial chain through the line
-- data: select the byte at off, decode the copy header, add its size, select
-- the byte at the resulting offset, decode the literal header, select the
-- literal length, and only then add everything up and check for the end of
-- the line. The second byte select sits behind the first one plus an adder,
-- and everything expensive (the literal length select, the length adder, the
-- end-of-line compare and the line wrap subtraction) sits behind that, which
-- makes this the deepest cloud in the core.
--
-- The literal half is now evaluated speculatively for all eight possible
-- header positions within the line. Those copies depend only on the
-- registered line data and endi, never on the current offset, so they run in
-- parallel with the copy header decode rather than behind it, and the copy
-- header decode ends in a single 8:1 select among the precomputed results.
-- The length addition is also folded into one adder (the literal header size
-- and the diminished-one +1 are merged into a base that is ready long before
-- the length itself), and both the end-of-line compare and the -8 line wrap
-- are precomputed per position, so the only thing left after the 8:1 select
-- is a 2:1 mux. Cycle-for-cycle behaviour is unchanged.
entity vhsnunzip_decoder_long is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    -- Double line compressed data stream input.
    cd          : in  compressed_stream_double;
    cd_ready    : out std_logic;

    -- Element information stream output.
    el          : out element_stream;
    el_ready    : in  std_logic

  );
end vhsnunzip_decoder_long;

architecture behavior of vhsnunzip_decoder_long is
begin
  proc: process (clk) is

    -- Input holding register.
    variable cdh    : compressed_stream_double := COMPRESSED_STREAM_DOUBLE_INIT;

    -- Offset of the next element with respect to cdh.data. The complete offset
    -- is encoded as off + (offh << 8); the counter carry is pipelined to meet
    -- timing.
    variable off    : unsigned(8 downto 0) := (others => '0');
    variable offh   : unsigned(23 downto 0) := (others => '0');

    -- Offset of the literal header with respect to cdh.data, i.e. the offset
    -- of the next element after the copy header (if any) was consumed.
    variable offns  : unsigned(LB_LOG2 downto 0) := (others => '0');

    -- Next offset, in case the data we're decoding actually consists of
    -- element headers and not literal data.
    variable offx   : unsigned(8 downto 0) := (others => '0');
    variable offnh  : unsigned(23 downto 0) := (others => '0');

    -- Same as `off`, but modulo the line width and converted to integer.
    variable ofi    : natural range 0 to LB-1 := 0;

    -- Output holding register.
    variable elh    : element_stream := ELEMENT_STREAM_INIT;

    -- Speculative literal decode result for one candidate header position.
    -- `offx` is already the next value of `off`: the offset after the literal
    -- header and its data, with the line wrap subtraction applied when `pop`
    -- says the offset moved past the end of the current line.
    type li_spec_type is record
      val   : std_logic;
      loff  : unsigned(LB_LOG2 downto 0);
      llen  : unsigned(31 downto 0);
      offx  : unsigned(8 downto 0);
      pop   : std_logic;
    end record;
    type li_spec_array is array (natural range <>) of li_spec_type;
    variable ld     : li_spec_array(0 to LB-1);

    -- Temporaries for the speculative decode above.
    variable ladv   : unsigned(LB_LOG2 downto 0);
    variable llen8  : unsigned(7 downto 0);
    variable lbase  : unsigned(8 downto 0);
    variable loffn  : unsigned(8 downto 0);
    variable lhz    : std_logic;

    -- Selected speculative result and the position it was selected from.
    variable jidx   : natural range 0 to LB-1 := 0;
    variable wrap   : std_logic;
    variable lv     : li_spec_type;

    -- Whether we were decoding literal data instead of element headers, and
    -- whether the new offset moved past the end of the current line.
    variable cond1  : std_logic;
    variable cond2  : std_logic;

  begin
    if rising_edge(clk) then

      -- Invalidate the output register if it was shifted out.
      if el_ready = '1' then
        elh.valid := '0';
      end if;

      -- Shift new data into the input when we can.
      if cdh.valid = '0' then
        cdh := cd;
        if cdh.valid = '1' and cdh.first = '1' then
          off := resize(cdh.start, 9);
        end if;
      end if;

      -- Decode when we have valid data and have room for the result.
      if cdh.valid = '1' and elh.valid = '0' then
        elh.valid := '1';

        if off(8 downto 7) = "11" then
          off(8) := '0';
          offh := offh - 1;
        end if;

        ---------------------------------------------------------------------
        -- Speculative literal element decode
        ---------------------------------------------------------------------
        -- One copy for every byte position the literal header could start at.
        -- These only depend on the registered line data and endi, so they do
        -- not wait for the copy header decode below. The case where the
        -- literal header would start on the lookahead line is not covered
        -- here; it is handled separately after the select, because it is
        -- trivial (such an element is always beyond endi, hence never valid).
        for j in 0 to LB-1 loop

          if to_unsigned(j, LB_LOG2) > cdh.endi then
            -- No element (for now); beyond end of stream.
            ld(j).val := '0';
            ladv := (others => '0');

          elsif cdh.data(j)(1 downto 0) /= "00" then
            -- Copy element.
            ld(j).val := '0';
            ladv := (others => '0');

          elsif cdh.data(j)(7 downto 4) = "1111" then
            -- Literal with 2- to 5-byte header.
            ld(j).val := '1';
            ladv := to_unsigned(2, LB_LOG2+1) + unsigned(cdh.data(j)(3 downto 2));

          else
            -- Literal with 1-byte header.
            ld(j).val := '1';
            ladv := to_unsigned(1, LB_LOG2+1);

          end if;

          ld(j).loff := to_unsigned(j, LB_LOG2+1) + ladv;

          if std_match(cdh.data(j), "111100--") then
            -- Literal with 2-byte header, or not a literal.
            ld(j).llen := X"000000"
                        & unsigned(cdh.data(j + 1));

          elsif std_match(cdh.data(j), "111101--") then
            -- Literal with 3-byte header, or not a literal.
            ld(j).llen := X"0000"
                        & unsigned(cdh.data(j + 2))
                        & unsigned(cdh.data(j + 1));

          elsif std_match(cdh.data(j), "111110--") then
            -- Literal with 4-byte header, or not a literal.
            ld(j).llen := X"00"
                        & unsigned(cdh.data(j + 3))
                        & unsigned(cdh.data(j + 2))
                        & unsigned(cdh.data(j + 1));

          elsif std_match(cdh.data(j), "111111--") then
            -- Literal with 5-byte header, or not a literal.
            ld(j).llen := unsigned(cdh.data(j + 4))
                        & unsigned(cdh.data(j + 3))
                        & unsigned(cdh.data(j + 2))
                        & unsigned(cdh.data(j + 1));

          else
            -- Literal with 1-byte header, or not a literal.
            ld(j).llen := X"000000"
                        & "00" & unsigned(cdh.data(j)(7 downto 2));

          end if;

          -- The offset adder below only needs the low byte of the literal
          -- length, and that byte is the same for all four of the multi-byte
          -- header forms, so it is a 2:1 select rather than the full 5:1 one
          -- above. Writing it out keeps the adder's late operand two levels
          -- earlier than the length itself.
          if cdh.data(j)(7 downto 4) = "1111" then
            llen8 := unsigned(cdh.data(j + 1));
          else
            llen8 := "00" & unsigned(cdh.data(j)(7 downto 2));
          end if;

          -- Seek past the literal data. The header position, the header size
          -- and the diminished-one correction are all known well before the
          -- literal length is, so they are folded into the base of a single
          -- adder instead of forming a second one behind it.
          lhz := '0';
          if ld(j).val = '1' then
            lbase := resize(ld(j).loff, 9) + 1;
            loffn := lbase + resize(llen8, 9);
            if ld(j).llen(31 downto 8) /= 0 then
              lhz := '1';
            end if;
          else
            loffn := resize(ld(j).loff, 9);
          end if;

          -- Precompute the end-of-line test and the line wrap subtraction, so
          -- that neither of them ends up behind the select below.
          if loffn > cdh.endi or lhz = '1' then
            ld(j).offx := loffn - LB;
            ld(j).pop  := '1';
          else
            ld(j).offx := loffn;
            ld(j).pop  := '0';
          end if;

        end loop;

        ---------------------------------------------------------------------
        -- Handle copy elements
        ---------------------------------------------------------------------
        offns := resize(off(LB_LOG2-1 downto 0), LB_LOG2+1);
        ofi := to_integer(off(LB_LOG2-1 downto 0));

        case cdh.data(ofi)(1 downto 0) is

          when "01" =>
            -- 2-byte copy element.
            elh.cp_val := '1';
            offns := offns + 2;

          when "10" =>
            -- 3-byte copy element.
            elh.cp_val := '1';
            offns := offns + 3;

          when "11" =>
            -- 5-byte copy element. Note that we ignore byte 4 and 5; they
            -- should be zero! Otherwise they'd encode an offset beyond
            -- 64kiB, which our memory is not long enough for.
            elh.cp_val := '1';
            offns := offns + 5;

          when others =>
            -- Literal element.
            elh.cp_val := '0';

        end case;

        if cdh.data(ofi)(1) = '0' then
          -- 2-byte copy element, or not a copy.
          elh.cp_off := "00000" & unsigned(cdh.data(ofi)(7 downto 5)) & unsigned(cdh.data(ofi + 1));
          elh.cp_len := resize(unsigned(cdh.data(ofi)(4 downto 2)), 6) + 3;

        else
          -- 3- or 5-byte copy element.
          elh.cp_off := unsigned(cdh.data(ofi + 2)) & unsigned(cdh.data(ofi + 1));
          elh.cp_len := unsigned(cdh.data(ofi)(7 downto 2));

        end if;

        ---------------------------------------------------------------------
        -- Handle literal elements
        ---------------------------------------------------------------------
        -- Pick the speculative result belonging to the position the copy
        -- header actually ended at. This 8:1 select is all that is left of
        -- the literal decode on the offset recurrence.
        jidx := to_integer(offns(LB_LOG2-1 downto 0));
        wrap := offns(LB_LOG2);
        lv   := ld(jidx);

        -- The literal length is a function of the same position in both
        -- cases, so it needs no correction for the wrapping case.
        elh.li_len := lv.llen;

        if wrap = '1' then

          -- The literal header would start on the lookahead line. That is
          -- always beyond endi, so there is no element, the offset does not
          -- advance past the header, and it is by definition past the end of
          -- the current line.
          elh.li_val := '0';
          elh.li_off := offns;
          offnh := (others => '0');
          offx  := resize(offns(LB_LOG2-1 downto 0), 9);
          cond2 := '1';

        else

          elh.li_val := lv.val;
          elh.li_off := lv.loff;
          offnh := (others => '0');
          if lv.val = '1' then
            offnh := lv.llen(31 downto 8);
          end if;
          offx  := lv.offx;
          cond2 := lv.pop;

        end if;

        ---------------------------------------------------------------------

        -- Invalidate the decoded elements if we were actually decoding
        -- literal data from a previously decoded literal, or if both elements
        -- were beyond the end of the stream. Note that in that case the
        -- offset does not take the decoded value, so it is still past the end
        -- of the line and the line is retired unconditionally.
        cond1 := '0';
        if off > cdh.endi or offh /= 0 then
          cond1 := '1';
        end if;

        if cond1 = '1' then
          elh.cp_val := '0';
          elh.li_val := '0';
          off := off - LB;
          cdh.valid := '0';
          elh.ld_pop := '1';
          elh.last := cdh.last;
        else
          off  := offx;
          offh := offnh;

          -- If our new offset is beyond the current line, invalidate the line
          -- (the offset was already decreased by 8 accordingly to prepare for
          -- the next line). Also indicate to the datapath that it should pop
          -- from the literal line stream after executing this command to stay
          -- in sync.
          if cond2 = '1' then
            cdh.valid := '0';
            elh.ld_pop := '1';
            elh.last := cdh.last;
          else
            elh.ld_pop := '0';
            elh.last := '0';
          end if;
        end if;

      end if;

      -- Handle reset.
      if reset = '1' then
        cdh.valid := '0';
        elh.valid := '0';
        off := (others => '0');
      end if;

      -- Assign outputs.
      cd_ready <= not cdh.valid;
      el <= elh;

    end if;
  end process;
end behavior;
