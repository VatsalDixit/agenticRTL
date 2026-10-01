library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_int_pkg.all;

-- Snappy element decoder with support for chunks > 64kiB.
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

  -- The byte positions at which the element behind a copy can start. The copy
  -- header is 2, 3 or 5 bytes long, so the candidate index is off(2..0)+2, +3
  -- or +5; the 5-byte form encodes an offset of 64kiB or more, which neither a
  -- valid Snappy stream with a 32kiB window nor this core's 64kiB history can
  -- produce, so it is not a pairing candidate and only two candidates remain.
  -- Both are derived from the `off` register alone, so both candidate decodes
  -- run in parallel with the first element's own decode and only a 2:1
  -- multiplexer selected by the first element's tag sits behind it -- exactly
  -- the structure the literal header byte `lhdr` already uses.
  type nat2 is array (0 to 1) of natural range 0 to 7;
  constant K1J : nat2 := (2, 3);

begin
  proc: process (clk) is

    -- Candidate decodes of the element behind the copy, one per candidate
    -- position. c2ok(p) is set when that candidate is a copy that may be paired
    -- with the first one; see the pairing conditions at its assignment. khdc(p)
    -- is the *total* header size of the paired transfer for that candidate,
    -- K1J(p) + the second copy's own header size, formed here so that the pair
    -- adds a multiplexer behind the k1+k2 adder rather than in front of it: the
    -- line-exhausted recurrence then sees one extra 4-bit select and no extra
    -- carry chain.
    type u4_arr is array (0 to 1) of unsigned(3 downto 0);
    type u16_arr is array (0 to 1) of unsigned(15 downto 0);
    variable c2ok   : std_logic_vector(0 to 1);
    variable khdc   : u4_arr;
    variable c2len  : u4_arr;
    variable c2off  : u16_arr;

    -- Whether the pair is issued this cycle, and the total header size it
    -- implies.
    variable pair   : std_logic;
    variable khdp   : unsigned(3 downto 0);

    -- Scratch for the candidate decodes.
    variable cj     : natural range 0 to 15;
    variable l1     : unsigned(6 downto 0);
    variable l2     : unsigned(6 downto 0);

    -- Input holding register.
    variable cdh    : compressed_stream_double := COMPRESSED_STREAM_DOUBLE_INIT;

    -- Offset of the next element with respect to cdh.data. The complete offset
    -- is encoded as off + (offh << 8); the counter carry is pipelined to meet
    -- timing.
    variable off    : unsigned(8 downto 0) := (others => '0');
    variable offh   : unsigned(23 downto 0) := (others => '0');

    -- Next offset, in case the data we're decoding actually consists of
    -- element headers and not literal data.
    variable offns  : unsigned(3 downto 0) := (others => '0');
    variable offn   : unsigned(8 downto 0) := (others => '0');
    variable offnh  : unsigned(23 downto 0) := (others => '0');

    -- Same as `offn`, but already stepped back by one line: the -8 is folded
    -- into the literal-length addend, which is not derived from `off`, so the
    -- exhausted and non-exhausted next offsets are two parallel adds selected by
    -- `exh` instead of an add followed by a borrow chain gated by `exh`.
    variable offnm  : unsigned(8 downto 0) := (others => '0');

    -- Same as `off`, but modulo the line width and converted to integer.
    variable ofi    : natural range 0 to 7 := 0;

    -- The literal element header byte, taken from the 16-byte pre-decoded
    -- window. It sits at off(2..0) + k, where k is 0 when this element has no
    -- copy in front of the literal and 2, 3 or 5 for the three copy header
    -- sizes, so it can be anywhere in 0..12 and may well land in the lookahead
    -- half of the window. Rather than indexing the window with the post-copy
    -- offset -- which would put an adder and a 16:1 byte mux in series behind
    -- the copy tag decode -- the four candidate bytes are selected with the
    -- register-derived index off(2..0) and then picked apart by the copy tag,
    -- so the header byte is available one select after the tag itself. Three of
    -- the four candidates (k = 0, 2 and the k = 3 byte, which doubles as a
    -- multi-byte length byte below) are muxes the copy decode needs anyway.
    variable lhdr   : std_logic_vector(7 downto 0) := (others => '0');

    -- Index of the element header currently being decoded, taken straight from
    -- the `off` register instead of from the post-copy offset. A multi-byte
    -- literal header is only ever decoded for an element that has no copy in
    -- front of it (see `hdr_ok` below), and for such an element the literal
    -- header sits exactly at off(2..0), so its length bytes can be selected
    -- with this register-derived index. That keeps those byte muxes (and hence
    -- the 24-bit `offnh /= 0` reduction behind them) off the
    -- tag -> offset -> line-exhausted path that owns the worst timing path.
    variable mbi    : natural range 0 to 7 := 0;

    -- Whether the literal element header in `lhdr` can be decoded this cycle.
    variable hdr_ok : boolean;

    -- Whether this line is exhausted by the element decoded this cycle, i.e.
    -- whether the holding register must be reloaded and the literal line
    -- popped. This is the head of the decode recurrence: it feeds cdh.valid,
    -- which selects the load multiplexer in front of the whole tag decode, and
    -- place-and-route names that self-loop as the worst path of the design.
    -- See the comment at its assignment for why it does not wait for the
    -- nine-bit literal seek adder.
    variable exh    : boolean;

    -- Set when the literal of this element is eight bytes or longer. Such an
    -- element always runs past the end of the current eight-byte line,
    -- whatever the offset it starts at, so this term settles the line-exhausted
    -- decision without the seek adder. It is a plain OR reduction of the high
    -- bits of the literal length, which for the common one-byte header is three
    -- bits of the header byte itself.
    variable bigli  : boolean;

    -- The seek of this cycle, split into its three addends instead of being
    -- accumulated by three adders in series. k1 is the copy header size (0, 2,
    -- 3 or 5 bytes), k2 the literal header size (0, 1 or 2..5 bytes) and kli3
    -- the low part of the literal data seek (0, or li_len(2..0)+1). khd is the
    -- total header size and ktot the whole five-bit seek. None of them involves
    -- `off`, so none of them is on the off -> off recurrence: they are pure
    -- functions of the window bytes.
    variable k1     : unsigned(2 downto 0);
    variable k2     : unsigned(2 downto 0);
    variable khd    : unsigned(3 downto 0);
    variable kli3   : unsigned(3 downto 0);
    variable kli9   : unsigned(8 downto 0);
    variable kli9m  : unsigned(8 downto 0);
    variable ktot   : unsigned(4 downto 0);

    -- How far the seek may go before it leaves the line (`dend`) and before it
    -- leaves the valid part of the 16-byte window (`dwen`). Both are differences
    -- of two registers -- cdh.endi/cdh.wendi against off(2..0) -- so they are
    -- available at the same time as the byte muxes that select the element tag.
    -- Comparing the *seek* against them replaces "add the header sizes to off,
    -- then compare the sum against endi", which put two to three carry chains
    -- in series behind the tag decode and in front of the line-exhausted
    -- decision. This is the carry-select form of that comparison: the addend
    -- that arrives late is the small one, so it is the one left in front of the
    -- comparator.
    variable dend   : signed(5 downto 0);
    variable dwen   : signed(5 downto 0);

    -- Set for the single cycle in which the first line of a chunk is shifted
    -- into the holding register. The chunk's start offset (the size of the
    -- varint-encoded uncompressed length header, 1 to 5) is loaded into `off`
    -- at the *end* of that cycle instead of at the start, and decoding is
    -- suppressed for it. The point is that `off` as read by the decode below is
    -- then purely the output of its own register: the chunk-start multiplexer
    -- used to sit in front of off(2..0), i.e. in front of the byte mux that
    -- selects the element tag, so it added a level of logic and a long route
    -- from the pre-decoder's `first` register to the *entire*
    -- tag -> offset -> line-exhausted recurrence. Place-and-route named exactly
    -- that path (pre_decoder cdh[first] -> decoder off[7]) as the worst path of
    -- the design. The cost is one dead cycle per chunk; chunks are hundreds of
    -- bytes to 15 MB, so that is far below measurement noise.
    variable ld_first : std_logic;

    -- Output holding register.
    variable elh    : element_stream := ELEMENT_STREAM_INIT;

  begin
    if rising_edge(clk) then

      -- Invalidate the output register if it was shifted out.
      if el_ready = '1' then
        elh.valid := '0';
      end if;

      -- Shift new data into the input when we can.
      ld_first := '0';
      if cdh.valid = '0' then
        cdh := cd;
        if cdh.valid = '1' and cdh.first = '1' then
          ld_first := '1';
        end if;
      end if;

      -- Decode when we have valid data and have room for the result.
      if cdh.valid = '1' and elh.valid = '0' and ld_first = '0' then
        elh.valid := '1';

        if off(8 downto 7) = "11" then
          off(8) := '0';
          offh := offh - 1;
        end if;

        ---------------------------------------------------------------------
        -- Handle copy elements
        ---------------------------------------------------------------------
        ofi := to_integer(off(2 downto 0));
        mbi := ofi;

        -- The two comparison bounds, relative to the current offset. Both are
        -- register minus register, so they resolve while the byte muxes below
        -- are still resolving; see the declarations.
        dend := signed(resize(cdh.endi, 6)) - signed(resize(off(2 downto 0), 6));
        dwen := signed(resize(cdh.wendi, 6)) - signed(resize(off(2 downto 0), 6));

        case cdh.data(ofi)(1 downto 0) is

          when "01" =>
            -- 2-byte copy element.
            elh.cp_val := '1';
            k1 := "010";
            lhdr := cdh.data(ofi + 2);

          when "10" =>
            -- 3-byte copy element.
            elh.cp_val := '1';
            k1 := "011";
            lhdr := cdh.data(ofi + 3);

          when "11" =>
            -- 5-byte copy element. Note that we ignore byte 4 and 5; they
            -- should be zero! Otherwise they'd encode an offset beyond
            -- 64kiB, which our memory is not long enough for.
            elh.cp_val := '1';
            k1 := "101";
            lhdr := cdh.data(ofi + 5);

          when others =>
            -- Literal element.
            elh.cp_val := '0';
            k1 := "000";
            lhdr := cdh.data(ofi);

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
        -- Handle the second copy of a copy pair
        ---------------------------------------------------------------------
        -- Decode the element that would sit behind the copy at each of the two
        -- pairable copy header sizes. Every byte index here is off(2..0) +
        -- constant, so none of this waits for the first element's tag; only the
        -- 2:1 selection below does.
        for p in 0 to 1 loop
          cj := ofi + K1J(p);

          -- The second copy's own header.
          if cdh.data(cj)(1 downto 0) = "01" then
            khdc(p) := to_unsigned(K1J(p) + 2, 4);
            c2off(p) := "00000" & unsigned(cdh.data(cj)(7 downto 5))
                                & unsigned(cdh.data(cj + 1));
            l2 := resize(unsigned(cdh.data(cj)(4 downto 2)), 7) + 3;

            -- The second copy must reach back at least 768 bytes. cmd_gen_2
            -- routes a copy to the long-term memory as soon as it reaches more
            -- than 31 lines (496 bytes) back from a destination position of at
            -- most 31, so anything from 528 bytes is guaranteed long-term; 768
            -- is the nearest bound that is a plain test on the high bits. Being
            -- long-term means the second copy needs no short-term SRL read port
            -- of its own, and reaching that far back also means it cannot read
            -- any byte that the first copy of the pair writes this very cycle,
            -- which is the data hazard that would otherwise forbid the pair.
            c2ok(p) := cdh.data(cj)(7)
                    or (cdh.data(cj)(6) and cdh.data(cj)(5));

          elsif cdh.data(cj)(1 downto 0) = "10" then
            khdc(p) := to_unsigned(K1J(p) + 3, 4);
            c2off(p) := unsigned(cdh.data(cj + 2)) & unsigned(cdh.data(cj + 1));
            l2 := resize(unsigned(cdh.data(cj)(7 downto 2)), 7);

            -- Same 768-byte bound; here the offset's high byte is a byte of its
            -- own, so the test is simply "high byte is 3 or more".
            if unsigned(cdh.data(cj + 2)) >= 3 then
              c2ok(p) := '1';
            else
              c2ok(p) := '0';
            end if;

          else
            -- A literal, or a 5-byte copy header.
            khdc(p) := to_unsigned(K1J(p), 4);
            c2off(p) := (others => '0');
            l2 := (others => '0');
            c2ok(p) := '0';

          end if;
          c2len(p) := l2(3 downto 0);

          -- The first copy's length and its "offset is at least 16" test. The
          -- candidate index p *is* the first copy's header size, so each
          -- candidate knows which of the two header layouts the first copy has.
          -- An offset of at least 16 means neither cmd_gen_1's overlapping-copy
          -- split nor its run-length acceleration can trigger, so the first
          -- copy is guaranteed to be issued whole in the pair's one cycle.
          if p = 0 then
            l1 := resize(unsigned(cdh.data(ofi)(4 downto 2)), 7) + 3;
            if unsigned(cdh.data(ofi)(7 downto 5)) = 0
               and unsigned(cdh.data(ofi + 1)(7 downto 4)) = 0 then
              c2ok(p) := '0';
            end if;
          else
            l1 := resize(unsigned(cdh.data(ofi)(7 downto 2)), 7);
            if unsigned(cdh.data(ofi + 2)) = 0
               and unsigned(cdh.data(ofi + 1)(7 downto 4)) = 0 then
              c2ok(p) := '0';
            end if;
          end if;

          -- Both copies together must fit one 16-byte line. The lengths are
          -- diminished-one, so this is "real lengths add up to 16 or less".
          if l1 + l2 > 14 then
            c2ok(p) := '0';
          end if;

          -- The whole of the second header must be inside the valid part of the
          -- 16-byte window. K1J(p) + 3 covers the largest paired header (3
          -- bytes) with one byte to spare.
          if signed(to_signed(K1J(p) + 3, 6)) > dwen then
            c2ok(p) := '0';
          end if;

        end loop;

        -- Select the candidate that matches the first element's header size.
        -- This is the only part of the second copy's decode that sits behind
        -- the first element's tag, and it is one multiplexer deep.
        if cdh.data(ofi)(1 downto 0) = "01" then
          pair := c2ok(0);
          khdp := khdc(0);
          elh.cp2_off := c2off(0);
          elh.cp2_len := c2len(0);
        elsif cdh.data(ofi)(1 downto 0) = "10" then
          pair := c2ok(1);
          khdp := khdc(1);
          elh.cp2_off := c2off(1);
          elh.cp2_len := c2len(1);
        else
          pair := '0';
          khdp := khdc(0);
          elh.cp2_off := c2off(0);
          elh.cp2_len := c2len(0);
        end if;
        elh.cp2_val := pair;

        ---------------------------------------------------------------------
        -- Handle literal elements
        ---------------------------------------------------------------------
        -- `lhdr` above is the header byte at the post-copy offset `offns`, i.e.
        -- what used to be written cdh.data(to_integer(offns)). Nothing is
        -- indexed by the post-copy offset any more: no byte mux sits behind the
        -- copy tag decode.
        --
        -- Originally, a literal header that started beyond the current line
        -- was simply not decoded: the element was emitted with only the copy,
        -- the line was popped, and the literal became an element (and hence a
        -- command) of its own on the next cycle. Since the copy header is 2,
        -- 3 or 5 bytes long, that happens for a sizeable fraction of the
        -- copy/literal pairs, and it costs a whole command whenever the copy
        -- and the literal would have fit in one 8-byte line together.
        --
        -- We can decode it out of the lookahead half instead. The datapath's
        -- literal source window is 16 bytes wide as well (li_off runs 0..15
        -- and selects between the current literal line and the lookahead line
        -- via LOOKAHEAD_LOOKUP in vhsnunzip_pipeline), and cmd_gen_2 clamps
        -- the number of literal bytes per command to 15-li_off and pops the
        -- literal line exactly once as li_off crosses 8, so an li_off that
        -- starts out at 9..13 needs no extra machinery downstream.
        --
        -- Two restrictions:
        --  - The header must be inside the valid part of the window.
        --    cdh.wendi is the index of the last valid byte of the whole
        --    16-byte window: 8+nxt.endi when the lookahead half is a real
        --    line, and equal to cdh.endi when this is the last line of the
        --    chunk and the lookahead half holds stale bytes. Without this the
        --    decoder happily reads the pre-decoder's padding as a one-byte
        --    literal header and invents an output byte.
        --  - A multi-byte literal header is only decoded when this element has
        --    no copy in front of it, i.e. when the header sits at off(2..0)
        --    itself. Its length bytes then live at off(2..0)+1..+4, which are
        --    selected from the `off` register and not from the post-copy
        --    offset, so they cost no logic depth on the tag -> offset ->
        --    line-exhausted path. When a copy IS in front of the header, a
        --    multi-byte header is simply not decoded: the element is emitted
        --    with only the copy, `off` is left pointing at the literal header,
        --    and the literal becomes an element of its own on the next cycle
        --    (the pre-i4 behaviour for this case). A multi-byte header means a
        --    literal of 60+ bytes, which occupies many commands anyway, so the
        --    one extra command it costs is negligible, while decoding it here
        --    would put four more offns-indexed byte muxes and a 24-bit
        --    zero-compare in series with the copy tag decode.
        -- `offns <= cdh.wendi`, i.e. `off(2..0) + k1 <= cdh.wendi`, written as
        -- `k1 <= cdh.wendi - off(2..0)` so that the sum the copy tag selects is
        -- not added to anything before it is compared.
        hdr_ok := signed(resize(k1, 6)) <= dwen
              and (elh.cp_val = '0' or lhdr(7 downto 4) /= "1111");

        if not hdr_ok then
          -- No element (for now); beyond end of stream, or starts on the next
          -- line and we cannot reach it.
          elh.li_val := '0';
          k2 := "000";

        elsif lhdr(1 downto 0) /= "00" then
          -- Copy element.
          elh.li_val := '0';
          k2 := "000";

        elsif lhdr(7 downto 4) = "1111" then
          -- Literal with 2- to 5-byte header. Only reachable with cp_val = '0',
          -- i.e. with the header at off(2..0).
          elh.li_val := '1';
          k2 := "010" + unsigned(lhdr(3 downto 2));

        else
          -- Literal with 1-byte header.
          elh.li_val := '1';
          k2 := "001";

        end if;

        -- Total header size, and the post-header offset. The two header sizes
        -- are added to each other rather than one after the other to `off`, so
        -- `off` joins in a single adder that no longer feeds another one. k2 can
        -- only exceed 1 when k1 is 0, because hdr_ok above refuses a multi-byte
        -- literal header that sits behind a copy, so k1 + k2 <= 6 and offns <= 13,
        -- exactly as before: it still fits four bits and never wraps.
        -- When the pair is taken, the element behind the first copy is the
        -- second copy and not a literal, so the literal slot stays empty and
        -- the total header size of this transfer is the first copy's header
        -- plus the second copy's header. That total was already formed per
        -- candidate above, so the pair costs one 4-bit multiplexer *behind* the
        -- k1+k2 adder instead of a multiplexer on k2 in front of it: the
        -- line-exhausted recurrence gains a select and no carry chain. Note
        -- that k2 is "000" here whenever pair is set (lhdr is a copy header, so
        -- the hdr_ok chain above took one of its two li_val = '0' branches),
        -- which is why khdp replaces the whole sum rather than part of it.
        if pair = '1' then
          elh.li_val := '0';
        end if;

        khd  := resize(k1, 4) + resize(k2, 4);
        if pair = '1' then
          khd := khdp;
        end if;
        offns := resize(off(2 downto 0), 4) + khd;

        elh.li_off := offns;

        -- The multi-byte-header branches below can only be *taken with li_val
        -- set* when this element carries no copy, in which case the header sits
        -- at mbi = off(2..0). Their length bytes are therefore indexed by `mbi`,
        -- which comes out of the `off` register: bytes mbi+1 and mbi+2 are
        -- already selected for the copy offset above, so only mbi+3 and mbi+4
        -- add a mux, and none of them sit behind the copy tag decode. When the
        -- branch is taken with li_val clear the length is don't-care, because
        -- `offn`/`offnh` only consume it under li_val (below) and cmd_gen
        -- ignores li_len when li_val is clear.
        if std_match(lhdr, "111100--") then
          -- Literal with 2-byte header, or not a literal.
          elh.li_len := X"000000"
                      & unsigned(cdh.data(mbi + 1));

        elsif std_match(lhdr, "111101--") then
          -- Literal with 3-byte header, or not a literal.
          elh.li_len := X"0000"
                      & unsigned(cdh.data(mbi + 2))
                      & unsigned(cdh.data(mbi + 1));

        elsif std_match(lhdr, "111110--") then
          -- Literal with 4-byte header, or not a literal.
          elh.li_len := X"00"
                      & unsigned(cdh.data(mbi + 3))
                      & unsigned(cdh.data(mbi + 2))
                      & unsigned(cdh.data(mbi + 1));

        elsif std_match(lhdr, "111111--") then
          -- Literal with 5-byte header, or not a literal.
          elh.li_len := unsigned(cdh.data(mbi + 4))
                      & unsigned(cdh.data(mbi + 3))
                      & unsigned(cdh.data(mbi + 2))
                      & unsigned(cdh.data(mbi + 1));

        else
          -- Literal with 1-byte header, or not a literal.
          elh.li_len := X"000000"
                      & "00" & unsigned(lhdr(7 downto 2));

        end if;

        -- Seek past literal data. The literal part of the seek is formed on its
        -- own and then added to `off` and to the header size in one three-input
        -- adder, instead of being chained onto the post-header offset: the sum
        -- is identical (offns = off(2..0) + khd, with off(8..3) zero whenever
        -- this result is kept) but nothing waits for a previous carry chain.
        offnh := (others => '0');
        kli9  := (others => '0');
        kli9m := to_unsigned(504, 9);
        kli3  := (others => '0');
        if elh.li_val = '1' then
          kli9  := resize(elh.li_len(7 downto 0), 9) + 1;
          kli9m := resize(elh.li_len(7 downto 0), 9) - 7;
          kli3  := resize(elh.li_len(2 downto 0), 4) + 1;
          offnh := elh.li_len(31 downto 8);
        end if;
        offn  := resize(off(2 downto 0), 9) + resize(khd, 9) + kli9;
        offnm := resize(off(2 downto 0), 9) + resize(khd, 9) + kli9m;

        -- Same seek, but only over the low three bits of the literal length,
        -- and the flag that says those three bits are not the whole story.
        -- `ktot` is the whole seek *relative to off*, so it is a function of the
        -- window bytes only; see the line-exhausted decision below.
        bigli  := elh.li_val = '1' and elh.li_len(31 downto 3) /= 0;
        ktot   := resize(khd, 5) + resize(kli3, 5);

        ---------------------------------------------------------------------

        -- Invalidate the decoded elements if we were actually decoding
        -- literal data from a previously decoded literal, or if both elements
        -- were beyond the end of the stream. All of the above could actually
        -- go inside this if statement, but by doing it this way, the decoded
        -- header information is independent of the result of the condition
        -- (only the valid bits are).
        if off > cdh.endi or offh /= 0 then
          elh.cp_val := '0';
          elh.li_val := '0';
          elh.cp2_val := '0';

          -- The offset did not move, so the line stays exhausted. Stepping to
          -- the next line is the same subtraction as it always was; only the
          -- other branch gets the folded form.
          exh := true;
          off := off - 8;

        else
          offh := offnh;

          -- The line is exhausted when the new offset offn = offns + li_len + 1
          -- is past cdh.endi, or when the literal length has a non-zero high
          -- part. Written literally like that the decision waits for the
          -- nine-bit seek adder, and it is what gates cdh.valid, which in turn
          -- selects the load multiplexer in front of the entire tag decode: the
          -- whole recurrence hangs off this one comparison, so every level of
          -- logic in front of it is paid for once per cycle.
          --
          -- cdh.endi is at most 7, so as soon as the literal is eight bytes or
          -- longer the answer is "exhausted" no matter what offns is, and the
          -- adder result is irrelevant. That case is exactly `bigli`, a bare OR
          -- reduction over li_len(31..3) which does not pass through the adder
          -- at all (and subsumes the old offnh /= 0 term, since a non-zero high
          -- part means a literal of at least 256 bytes). What is left is the
          -- literals of 0..7 bytes, for which the seek never exceeds 13+7+1 = 21
          -- and five bits are exact.
          --
          -- The remaining five-bit comparison used to be `off + ktot > endi`,
          -- with `ktot` accumulated onto `off` by two or three adders in series
          -- behind the tag decode. It is written here as `ktot > endi - off`
          -- instead: `dend` is a difference of two registers and is ready before
          -- the tag is, and `ktot` is a sum of three small window-derived
          -- constants, so the recurrence now holds one narrow add and one
          -- magnitude compare rather than a chain of them. In this branch
          -- off <= cdh.endi <= 7, so off = off(2..0) and dend >= 0, and the
          -- comparison is exactly the old one.
          exh := bigli or signed(resize(ktot, 6)) > dend;

          -- Both candidate next offsets were formed in parallel above, so the
          -- late `exh` only selects between them.
          if exh then
            off := offnm;
          else
            off := offn;
          end if;

        end if;

        -- If our new offset is beyond the current line, invalidate the line
        -- (the offset was already stepped back by 8 above). Also indicate to
        -- the datapath that it should pop from the literal line stream after
        -- executing this command to stay in sync.
        if exh then
          cdh.valid := '0';
          elh.ld_pop := '1';
          elh.last := cdh.last;
        else
          elh.ld_pop := '0';
          elh.last := '0';
        end if;

      end if;

      -- Load the start offset of a new chunk. This happens in the same cycle in
      -- which the chunk's first line is shifted in, but after the decode block,
      -- which is disabled for that cycle: the value decoding sees for `off` is
      -- therefore always the register output and never this multiplexer. `offh`
      -- is left alone, exactly as before: the previous chunk always ends with
      -- its offset inside its last line, so the high part is already zero here.
      if ld_first = '1' then
        off := resize(cdh.start, 9);
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
