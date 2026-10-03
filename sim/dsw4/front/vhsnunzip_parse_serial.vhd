library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 temporary serial parser (SPEC 11 and 12, step B2; deleted in B3).
--
-- Reads the header bytes at position p from CBUF port C, emits at most one
-- element per cycle, p += hdr + literal length; skips the chunk's varint;
-- emits an EOC element (lptr = chunk-end GA) at the chunk end, then pops CSF
-- and CEF. Its element and credit ports are the same as vhsnunzip_walker's,
-- so the ELQ does not change in B3.
--
--   gb             GA of the next CBUF beat: a byte at GA x has arrived iff
--                  ga_sdiff(gb, x) > 0. Bytes at or past the chunk end are
--                  never interpreted.
--   csf_* / cef_*  chunk start / end FIFO heads from vhsnunzip_cbuf.
--   rowC / lanesC  CBUF port C: per-lane rows (registered here), async data.
--   el / wcnt      elements written to the ELQ this cycle: el(0 to wcnt-1),
--                  wcnt 0..4 (at most 1 for this parser).
--   cred_ret       credits returned by the ELQ (elements the writer retired).
--   cred           current credit count (reset ELQ_CRED). An element may be
--                  emitted only while a credit is held (debug / probe output).
entity vhsnunzip_parse_serial is
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    gb          : in  ga_t;
    csf_valid   : in  std_logic;
    csf_ga      : in  ga_t;
    csf_pop     : out std_logic;
    cef_valid   : in  std_logic;
    cef_ga      : in  ga_t;
    cef_pop     : out std_logic;

    rowC        : out u5_arr(0 to 31);
    lanesC      : in  byte_array(0 to 31);

    el          : out element_arr(0 to 3);
    wcnt        : out unsigned(2 downto 0);
    cred_ret    : in  unsigned(2 downto 0);
    cred        : out unsigned(6 downto 0)
  );
end vhsnunzip_parse_serial;

-- Implementation. One decision per cycle on the registered position p_r;
-- port C rows (rowC_r) are registered from p_next, so lanesC holds the 32
-- bytes p_r .. p_r+31 (lane (p_r + i) mod 32 = byte p_r + i).
--   START : wait for the CSF head; p := chunk start.            (1 cycle)
--   VAR   : bytes at p arrived -> p += vlen (varint length 1..5). (1 cycle)
--   RUN   : if the CEF head is valid and p >= chunk end: emit EOC (lptr =
--           chunk end), pop CSF and CEF, go to START. Otherwise, when the 5
--           bytes p..p+4 have arrived (or the whole chunk has: CEF valid),
--           decode the header at p, emit the element, p += hdr (+ len for a
--           literal). Every emission needs one credit.
-- Outputs el(0) / wcnt are registered (presented the cycle after the
-- decision). This block is not timing-optimised (it is replaced in B3): the
-- decode, the 32-bit p += hdr + len and the row computation are one path.
architecture behavior of vhsnunzip_parse_serial is

  type mode_t is (S_START, S_VAR, S_RUN);
  signal mode     : mode_t := S_START;
  signal p_r      : ga_t := (others => '0');
  signal rowC_r   : u5_arr(0 to 31) := (others => (others => '0'));
  signal cred_r   : unsigned(6 downto 0) := to_unsigned(ELQ_CRED, 7);
  signal el_r     : element_t := ELEMENT_INIT;
  signal wcnt_r   : unsigned(2 downto 0) := "000";
  signal csf_pop_s, cef_pop_s : std_logic;
  signal p_next_s   : ga_t;
  signal mode_next  : mode_t;
  signal el_next    : element_t;
  signal emit_s     : std_logic;

  -- Port C rows that present bytes q .. q+31.
  function rows_for(q : ga_t) return u5_arr is
    variable r : u5_arr(0 to 31);
  begin
    for j in 0 to 31 loop
      if to_unsigned(j, 5) >= q(4 downto 0) then
        r(j) := q(9 downto 5);
      else
        r(j) := q(9 downto 5) + 1;
      end if;
    end loop;
    return r;
  end function;

begin

  comb_proc: process (mode, p_r, cred_r, gb, csf_valid, csf_ga, cef_valid, cef_ga,
                      lanesC) is
    variable b       : byte_array(0 to 4);
    variable arrived : boolean;
    variable at_end  : boolean;
    variable pr      : prec_t;
    variable flen    : unsigned(31 downto 0);
    variable len     : unsigned(31 downto 0);
    variable hdr     : unsigned(2 downto 0);
    variable vlen    : unsigned(2 downto 0);
    variable p_next  : ga_t;
    variable mode_n  : mode_t;
    variable el_v    : element_t;
    variable emit    : std_logic;
    variable pop_v   : std_logic;
  begin
    for i in 0 to 4 loop
      b(i) := lanesC(to_integer(p_r(4 downto 0) + i));
    end loop;
    -- The header (or varint) bytes are readable when p+5 <= GB, or when the
    -- chunk's last beat is in (CEF head valid: the head is this chunk's).
    arrived := cef_valid = '1' or ga_sdiff(gb, p_r + 5) >= 0;
    at_end  := cef_valid = '1' and ga_sdiff(p_r, cef_ga) >= 0;

    p_next := p_r;
    mode_n := mode;
    el_v   := ELEMENT_INIT;
    emit   := '0';
    pop_v  := '0';

    case mode is
      when S_START =>
        if csf_valid = '1' then
          p_next := csf_ga;
          mode_n := S_VAR;
        end if;

      when S_VAR =>
        if arrived then
          if b(0)(7) = '0' then    vlen := "001";
          elsif b(1)(7) = '0' then vlen := "010";
          elsif b(2)(7) = '0' then vlen := "011";
          elsif b(3)(7) = '0' then vlen := "100";
          else                     vlen := "101";
          end if;
          p_next := p_r + vlen;
          mode_n := S_RUN;
        end if;

      when S_RUN =>
        if cred_r /= 0 then
          if at_end then
            el_v.valid := '1';
            el_v.kind  := K_EOC;
            el_v.lptr  := cef_ga;
            emit   := '1';
            pop_v  := '1';
            mode_n := S_START;
          elsif arrived then
            pr := hdr_decode(b(0 to 2));
            hdr := pr.hdr;
            -- Far literal length: 1 + little-endian b(1 .. hdr-1).
            flen := (others => '0');
            case pr.hdr is
              when "010"  => flen(7 downto 0)  := unsigned(b(1));
              when "011"  => flen(15 downto 0) := unsigned(b(2)) & unsigned(b(1));
              when "100"  => flen(23 downto 0) := unsigned(b(3)) & unsigned(b(2)) & unsigned(b(1));
              when others => flen := unsigned(b(4)) & unsigned(b(3)) & unsigned(b(2)) & unsigned(b(1));
            end case;
            if pr.far = '1' then
              len := flen + 1;
            else
              len := resize(pr.len, 32);
            end if;
            el_v.valid := '1';
            el_v.len   := len;
            if pr.kind = K_LIT then
              el_v.kind := K_LIT;
              el_v.lptr := p_r + hdr;
              p_next    := p_r + hdr + len;
            else
              el_v.kind := K_CPY;
              el_v.off  := pr.off;
              el_v.lptr := p_r;
              p_next    := p_r + hdr;
            end if;
            emit := '1';
          end if;
        end if;
    end case;

    -- Registered next state, via signals driven from here.
    p_next_s   <= p_next;
    mode_next  <= mode_n;
    el_next    <= el_v;
    emit_s     <= emit;
    csf_pop_s  <= pop_v;
    cef_pop_s  <= pop_v;
  end process;

  reg_proc: process (clk) is
  begin
    if rising_edge(clk) then
      p_r    <= p_next_s;
      rowC_r <= rows_for(p_next_s);
      mode   <= mode_next;
      el_r   <= el_next;
      if emit_s = '1' then
        wcnt_r <= "001";
        cred_r <= cred_r - 1 + cred_ret;
      else
        wcnt_r <= "000";
        el_r.valid <= '0';
        cred_r <= cred_r + cred_ret;
      end if;
      if reset = '1' then
        mode   <= S_START;
        p_r    <= (others => '0');
        rowC_r <= (others => (others => '0'));
        cred_r <= to_unsigned(ELQ_CRED, 7);
        wcnt_r <= "000";
        el_r   <= ELEMENT_INIT;
      end if;
    end if;
  end process;

  csf_pop <= csf_pop_s;
  cef_pop <= cef_pop_s;
  rowC    <= rowC_r;
  el(0)   <= el_r;
  el(1 to 3) <= (others => ELEMENT_INIT);
  wcnt    <= wcnt_r;
  cred    <= cred_r;

  -- pragma translate_off
  chk_proc: process (clk) is
  begin
    if rising_edge(clk) then
      if reset = '0' then
        assert cred_r <= ELQ_CRED
          report "parse_serial: credit count " & integer'image(to_integer(cred_r))
                 & " exceeds ELQ_CRED" severity failure;
      end if;
    end if;
  end process;
  -- pragma translate_on

end behavior;
