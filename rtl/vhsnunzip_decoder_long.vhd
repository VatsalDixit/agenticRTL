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
begin
  proc: process (clk) is

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

    -- Same as `off`, but modulo the line width and converted to integer.
    variable ofi    : natural range 0 to 7 := 0;

    -- Index of the literal element header within the 16-byte pre-decoded
    -- window. Unlike `ofi` this is NOT taken modulo the line width, so it can
    -- point into the lookahead half of the window.
    variable lofi   : natural range 0 to 15 := 0;

    -- Whether the literal element header at `lofi` can be decoded this cycle.
    variable hdr_ok : boolean;

    -- Output holding register.
    variable elh    : element_stream := ELEMENT_STREAM_INIT;

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
        -- Handle copy elements
        ---------------------------------------------------------------------
        offns := resize(off(2 downto 0), 4);
        ofi := to_integer(off(2 downto 0));

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
        ofi := to_integer(offns(2 downto 0));

        -- Index of the literal header inside the full 16-byte window. When
        -- the copy element header above ended within the current line this is
        -- the same as ofi; when it ran past byte 7 it points into the
        -- lookahead half that the pre-decoder gives us.
        lofi := to_integer(offns);

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
        --  - Only single-byte literal headers are taken from the lookahead
        --    half. A multi-byte header would need its length bytes indexed at
        --    lofi+1..lofi+4, which can run past the end of the window, and
        --    offns (4 bits) could wrap. Multi-byte headers mean literals of
        --    60+ bytes, which are handled over many cycles anyway, so there
        --    is nothing to win there.
        -- Note that, given offns <= wendi, "offns <= endi" is the same test as
        -- "offns(3) = '0'": when endi < 7 the line is the last one of a chunk
        -- and wendi equals endi, and when endi = 7 the low half is exactly
        -- offns(3) = '0'. So this needs one comparator, not two.
        hdr_ok := offns <= cdh.wendi
              and (offns(3) = '0' or cdh.data(lofi)(7 downto 4) /= "1111");

        if not hdr_ok then
          -- No element (for now); beyond end of stream, or starts on the next
          -- line and we cannot reach it.
          elh.li_val := '0';

        elsif cdh.data(lofi)(1 downto 0) /= "00" then
          -- Copy element.
          elh.li_val := '0';

        elsif cdh.data(lofi)(7 downto 4) = "1111" then
          -- Literal with 2- to 5-byte header. Only reachable for lofi <= 7.
          elh.li_val := '1';
          offns := offns + 2 + unsigned(cdh.data(lofi)(3 downto 2));

        else
          -- Literal with 1-byte header.
          elh.li_val := '1';
          offns := offns + 1;

        end if;

        elh.li_off := offns;

        -- Note that the multi-byte-header branches below can only be *taken
        -- with li_val set* when lofi <= 7, in which case lofi = ofi; keeping
        -- the length bytes indexed by ofi therefore leaves their byte mux
        -- exactly as wide as it was (indices 1..11) while the header byte
        -- itself is selected from the full 16-byte window.
        if std_match(cdh.data(lofi), "111100--") then
          -- Literal with 2-byte header, or not a literal.
          elh.li_len := X"000000"
                      & unsigned(cdh.data(ofi + 1));

        elsif std_match(cdh.data(lofi), "111101--") then
          -- Literal with 3-byte header, or not a literal.
          elh.li_len := X"0000"
                      & unsigned(cdh.data(ofi + 2))
                      & unsigned(cdh.data(ofi + 1));

        elsif std_match(cdh.data(lofi), "111110--") then
          -- Literal with 4-byte header, or not a literal.
          elh.li_len := X"00"
                      & unsigned(cdh.data(ofi + 3))
                      & unsigned(cdh.data(ofi + 2))
                      & unsigned(cdh.data(ofi + 1));

        elsif std_match(cdh.data(lofi), "111111--") then
          -- Literal with 5-byte header, or not a literal.
          elh.li_len := unsigned(cdh.data(ofi + 4))
                      & unsigned(cdh.data(ofi + 3))
                      & unsigned(cdh.data(ofi + 2))
                      & unsigned(cdh.data(ofi + 1));

        else
          -- Literal with 1-byte header, or not a literal.
          elh.li_len := X"000000"
                      & "00" & unsigned(cdh.data(lofi)(7 downto 2));

        end if;

        -- Seek past literal data.
        offn := resize(offns, 9);
        offnh := (others => '0');
        if elh.li_val = '1' then
          offn := offn + elh.li_len(7 downto 0) + 1;
          offnh := elh.li_len(31 downto 8);
        end if;

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
        else
          off := offn;
          offh := offnh;
        end if;

        -- If our new offset is beyond the current line, invalidate the line
        -- and decrease by 8 accordingly to prepare for the next line. Also
        -- indicate to the datapath that it should pop from the literal line
        -- stream after executing this command to stay in sync.
        if off > cdh.endi or offh /= 0 then
          off := off - 8;
          cdh.valid := '0';
          elh.ld_pop := '1';
          elh.last := cdh.last;
        else
          elh.ld_pop := '0';
          elh.last := '0';
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
