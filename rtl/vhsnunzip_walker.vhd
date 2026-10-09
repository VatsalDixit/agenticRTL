library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- pragma translate_off
use std.textio.all;
-- pragma translate_on

-- DSW-4 walker WK, LOOP L3, and emission PE (SPEC 3.4, 3.5; build step B3).
-- Same element / credit ports as vhsnunzip_parse_serial.
--
-- Walker (one decision per cycle on registered e, mode, epoch_w, cred and the
-- WFIFO head H; H usable = valid and H.epoch = epoch_w, stale windows are
-- popped and dropped in every mode):
--   DRAIN    (reset state; after an EOC) pop windows until a usable H with
--            first = 1, then e := H.vlen and RUN (the per-chunk dead cycle).
--   RUN      needs usable H and cred >= 4; reserves 4 credits (PE1 returns
--            4 - wcnt). e >= H.endrel: EOC group, pop H, DRAIN. Otherwise the
--            group at e goes to PE1 and x = H.nxt4[e]:
--              x = 127   WAITFAR (H kept)
--              x < 16    e := x
--              x < 32    pop, e := x - 16
--              x <= 92   pop, STEP x/16 - 1 more windows, e := x mod 16
--   STEP     pop one usable window per cycle until done, then RUN.
--   WAITFAR  wait for far_v from PE (far target T, 32-bit GA, decoded from
--            the raw header bytes at the exit position). dist = T - H.base
--            (modular): dist < 80: step dist/16 windows, e := dist mod 16;
--            else retarget: tgt = T and not 15, epoch_w += 1, e := T mod 16,
--            RUN (which drops the stale windows until the new epoch arrives).
--   TEST_RETGT: every step (x >= 16 and every far exit) is a retarget to the
--            same 16-aligned window instead; must give identical elements.
--
-- Emission, feed-forward, 4 registered stages:
--   PE1   p1 = e, p2 = nxt[e], p3 = nxt2[e], p4 = nxt3[e] and a copy of H
--         (base, endrel, rec, x). Validity v1 = 1, v_k = v_{k-1} and p_k < 32
--         and p_k < endrel (a FAR p_{k-1} gives p_k = 127, so a far literal is
--         always the group's last element); wcnt and p_wcnt are decided with
--         the group and registered here. Returns 4 - wcnt credits.
--   PE2   r_k = rec[p_k]; raw bytes x[pl+1 .. pl+4] of the last valid pl.
--   PE3a  pos_k = base + p_k; len_k (far: 1 + little-endian raw bytes, 32 b);
--         hl = far length + hdr (the far element's exit distance).
--   PE3b  (= the PE emission, split in two as the timing review asked)
--         lptr_k = pos_k + hdr_k (LIT) or pos_k (CPY, EOC); far target
--         T = pos_last + hl with far_v, and dist = T - H.base = p_last + hl
--         pre-decoded (dist < 80, dist/16, dist mod 16) so WAITFAR has no
--         wide arithmetic. Registered el / wcnt to the ELQ.
--
--   wf_head / wf_pop    WFIFO head window (wf_head.valid) and its pop (comb.).
--   el / wcnt           el(0 to wcnt-1), wcnt 0..4, registered, to the ELQ.
--   retgt_v/retgt_tgt   retarget to the block reader (registered pulse).
--   eoc_p               an EOC group was decided (registered pulse).
--   cred_ret            credits returned by the ELQ (elements retired).
--   cred                credit count (reset ELQ_CRED; debug / probe).
--
-- GRP_FILE (sim only): when not "-", one line per group in gold's groups.txt
-- format is written to this file.
entity vhsnunzip_walker is
  generic (
    TEST_RETGT  : boolean := false;
    GRP_FILE    : string  := "-";
    PROBE       : boolean := false   -- sim only: probe_wk.txt counters
  );
  port (
    clk         : in  std_logic;
    reset       : in  std_logic;

    wf_head     : in  win_t;
    wf_pop      : out std_logic;

    el          : out element_arr(0 to 3);
    wcnt        : out unsigned(2 downto 0);

    retgt_v     : out std_logic;
    retgt_tgt   : out ga_t;
    eoc_p       : out std_logic;

    cred_ret    : in  unsigned(2 downto 0);
    cred        : out unsigned(6 downto 0)
  );
end vhsnunzip_walker;

architecture behavior of vhsnunzip_walker is

  type mode_t is (M_DRAIN, M_RUN, M_STEP, M_WAITFAR);
  signal mode     : mode_t := M_DRAIN;
  signal e_r      : unsigned(3 downto 0) := (others => '0');
  signal stp      : unsigned(2 downto 0) := (others => '0');
  signal epoch_w  : unsigned(1 downto 0) := "00";
  signal cred_r   : unsigned(6 downto 0) := to_unsigned(ELQ_CRED, 7);
  signal retgt_r  : std_logic := '0';
  signal tgt_r    : ga_t := (others => '0');
  signal eoc_r    : std_logic := '0';

  -- Walker decision (combinational).
  signal pop_s    : std_logic;
  signal emit_s   : std_logic;
  signal eoc_s    : std_logic;
  signal rtg_s    : std_logic;
  signal tgt_s    : ga_t;
  signal mode_n   : mode_t;
  signal e_n      : unsigned(3 downto 0);
  signal stp_n    : unsigned(2 downto 0);

  -- PE1
  signal p1v      : std_logic := '0';
  signal p1eoc    : std_logic := '0';
  signal p1p      : u7_arr(1 to 4) := (others => (others => '0'));
  signal p1base   : ga_t := (others => '0');
  signal p1endrel : unsigned(5 downto 0) := (others => '0');
  signal p1x      : byte_array(0 to 35) := (others => X"00");
  signal p1cnt    : unsigned(2 downto 0) := "001";        -- wcnt (registered)
  signal p1last   : unsigned(4 downto 0) := (others => '0'); -- p_wcnt
  signal ret_s    : unsigned(2 downto 0);

  -- PE2
  signal p2v      : std_logic := '0';
  signal p2eoc    : std_logic := '0';
  signal p2cnt    : unsigned(2 downto 0) := "001";
  signal p2p      : u5_arr(0 to 3) := (others => (others => '0'));
  signal p2r      : prec_arr(0 to 3) := (others => PREC_INIT);
  signal p2base   : ga_t := (others => '0');
  signal p2raw    : byte_array(0 to 3) := (others => X"00");
  signal p2far    : std_logic := '0';
  signal p2hdrl   : unsigned(2 downto 0) := (others => '0');
  signal p2last   : unsigned(4 downto 0) := (others => '0');

  -- PE3a
  type ga4_t is array (0 to 3) of ga_t;
  signal p3v      : std_logic := '0';
  signal p3eoc    : std_logic := '0';
  signal p3cnt    : unsigned(2 downto 0) := (others => '0');
  signal p3pos    : ga4_t := (others => (others => '0'));
  signal p3r      : prec_arr(0 to 3) := (others => PREC_INIT);
  signal p3len    : ga4_t := (others => (others => '0'));
  signal p3far    : std_logic := '0';
  signal p3hl     : ga_t := (others => '0');
  signal p3posl   : ga_t := (others => '0');
  signal p3dist   : ga_t := (others => '0');

  -- PE3b (outputs)
  signal el_r     : element_arr(0 to 3) := (others => ELEMENT_INIT);
  signal wcnt_r   : unsigned(2 downto 0) := (others => '0');
  signal far_v    : std_logic := '0';
  signal far_tgt  : ga_t := (others => '0');
  signal far_near : std_logic := '0';                       -- T - H.base < 80
  signal far_n    : unsigned(2 downto 0) := (others => '0'); -- (T - H.base) / 16
  signal far_e    : unsigned(3 downto 0) := (others => '0'); -- T mod 16

begin

  ---------------------------------------------------------------------------
  -- Walker decision (LOOP L3).
  ---------------------------------------------------------------------------
  dec_proc: process (mode, e_r, stp, epoch_w, cred_r, wf_head, far_v, far_tgt, far_near,
                     far_n, far_e) is
    variable hv, usable, stale : boolean;
    variable x     : unsigned(6 downto 0);
    variable n     : unsigned(2 downto 0);
  begin
    hv     := wf_head.valid = '1';
    usable := hv and wf_head.epoch = epoch_w;
    stale  := hv and not usable;
    x      := wf_head.nxt4(to_integer(e_r));

    pop_s  <= '0';
    emit_s <= '0';
    eoc_s  <= '0';
    rtg_s  <= '0';
    tgt_s  <= (others => '0');
    mode_n <= mode;
    e_n    <= e_r;
    stp_n  <= stp;

    if stale then
      pop_s <= '1';
    else
      case mode is
        when M_DRAIN =>
          if usable then
            if wf_head.first = '1' then
              e_n    <= '0' & wf_head.vlen;
              mode_n <= M_RUN;
            else
              pop_s <= '1';
            end if;
          end if;

        when M_RUN =>
          if usable and cred_r >= 4 then
            emit_s <= '1';
            if resize(e_r, 6) >= wf_head.endrel then
              eoc_s  <= '1';
              pop_s  <= '1';
              mode_n <= M_DRAIN;
            elsif x = FAR_NXT then
              mode_n <= M_WAITFAR;
            elsif x(6 downto 4) = "000" then
              e_n <= x(3 downto 0);
            elsif TEST_RETGT then
              rtg_s <= '1';
              tgt_s <= wf_head.base + (x(6 downto 4) & "0000");
              e_n   <= x(3 downto 0);
            elsif x(6 downto 4) = "001" then
              pop_s <= '1';
              e_n   <= x(3 downto 0);
            else
              pop_s  <= '1';
              e_n    <= x(3 downto 0);
              stp_n  <= x(6 downto 4) - 1;
              mode_n <= M_STEP;
            end if;
          end if;

        when M_STEP =>
          if usable then
            pop_s <= '1';
            stp_n <= stp - 1;
            if stp = 1 then
              mode_n <= M_RUN;
            end if;
          end if;

        when M_WAITFAR =>
          if far_v = '1' then
            n := far_n;
            if not TEST_RETGT and far_near = '1' then
              e_n <= far_e;
              if n = 0 then
                mode_n <= M_RUN;
              elsif n = 1 then
                pop_s  <= '1';
                mode_n <= M_RUN;
              else
                pop_s  <= '1';
                stp_n  <= n - 1;
                mode_n <= M_STEP;
              end if;
            else
              rtg_s  <= '1';
              tgt_s  <= far_tgt(31 downto 4) & "0000";
              e_n    <= far_e;
              mode_n <= M_RUN;
            end if;
          end if;
      end case;
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- PE1 validity and credit return (from PE1 registers).
  ---------------------------------------------------------------------------
  pe1_proc: process (p1v, p1cnt) is
  begin
    if p1v = '1' then
      ret_s <= 4 - p1cnt;
    else
      ret_s <= "000";
    end if;
  end process;

  ---------------------------------------------------------------------------
  -- Registers: walker state, PE1, PE2, PE3a, PE3b.
  ---------------------------------------------------------------------------
  seq_proc: process (clk) is
    variable pl    : natural range 0 to 31;
    variable pk    : natural range 0 to 31;
    variable hb    : byte_array(0 to 2);
    variable raw   : byte_array(0 to 3);
    variable rk    : prec_t;
    variable flen  : unsigned(31 downto 0);
    variable el_v  : element_t;
    variable c     : unsigned(2 downto 0);
    variable ok    : boolean;
    variable q     : unsigned(6 downto 0);
    variable last  : unsigned(4 downto 0);
  begin
    if rising_edge(clk) then
      -- Walker state.
      mode    <= mode_n;
      e_r     <= e_n;
      stp     <= stp_n;
      retgt_r <= rtg_s;
      tgt_r   <= tgt_s;
      eoc_r   <= eoc_s;
      if rtg_s = '1' then
        epoch_w <= epoch_w + 1;
      end if;
      if emit_s = '1' then
        cred_r <= cred_r - 4 + ret_s + cred_ret;
      else
        cred_r <= cred_r + ret_s + cred_ret;
      end if;

      -- PE1: chain positions and a copy of H.
      p1v   <= emit_s;
      p1eoc <= eoc_s;
      p1p(1) <= resize(e_r, 7);
      p1p(2) <= wf_head.nxt(to_integer(e_r));
      p1p(3) <= wf_head.nxt2(to_integer(e_r));
      p1p(4) <= wf_head.nxt3(to_integer(e_r));
      p1base   <= wf_head.base;
      p1endrel <= wf_head.endrel;
      p1x      <= wf_head.x;
      -- Group size and last position, decided with the group (off the loop:
      -- they only feed PE1 registers). v_k = v_{k-1} and p_k < 32 and
      -- p_k < endrel (a FAR p_{k-1} gives p_k = 127).
      c    := "001";
      last := '0' & e_r;
      ok   := eoc_s = '0';
      for k in 2 to 4 loop
        case k is
          when 2      => q := wf_head.nxt(to_integer(e_r));
          when 3      => q := wf_head.nxt2(to_integer(e_r));
          when others => q := wf_head.nxt3(to_integer(e_r));
        end case;
        ok := ok and q(6 downto 5) = "00" and resize(q, 6) < wf_head.endrel;
        if ok then
          c    := c + 1;
          last := q(4 downto 0);
        end if;
      end loop;
      p1cnt  <= c;
      p1last <= last;

      -- PE2: records of the chain, raw length bytes of the last element. The
      -- window carries no per-position record array: the header of each of the
      -- (at most 4) chain positions is decoded here from the three raw bytes at
      -- that position, which is the same hdr_decode PT1 would have run, because
      -- x is zeroed at and above endrel and every valid slot has p < endrel.
      p2v   <= p1v;
      p2eoc <= p1eoc;
      p2cnt <= p1cnt;
      p2base <= p1base;
      for k in 0 to 3 loop
        pk := to_integer(p1p(k + 1)(4 downto 0));
        p2p(k) <= p1p(k + 1)(4 downto 0);
        hb(0) := p1x(pk);
        hb(1) := p1x(pk + 1);
        hb(2) := p1x(pk + 2);
        p2r(k) <= hdr_decode(hb);
      end loop;
      pl := to_integer(p1last);
      p2last <= p1last;
      for j in 0 to 3 loop
        raw(j)   := p1x(pl + 1 + j);
        p2raw(j) <= raw(j);
      end loop;
      hb(0) := p1x(pl);
      hb(1) := raw(0);
      hb(2) := raw(1);
      rk := hdr_decode(hb);
      p2far  <= rk.far and not p1eoc;
      p2hdrl <= rk.hdr;

      -- PE3a: positions, lengths, far exit distance.
      p3v   <= p2v;
      p3eoc <= p2eoc;
      p3cnt <= p2cnt;
      p3r   <= p2r;
      p3far <= p2far and p2v;
      flen := (others => '0');
      case p2hdrl is
        when "010"  => flen(7 downto 0)  := unsigned(p2raw(0));
        when "011"  => flen(15 downto 0) := unsigned(p2raw(1)) & unsigned(p2raw(0));
        when "100"  => flen(23 downto 0) := unsigned(p2raw(2)) & unsigned(p2raw(1)) & unsigned(p2raw(0));
        when others => flen := unsigned(p2raw(3)) & unsigned(p2raw(2)) & unsigned(p2raw(1)) & unsigned(p2raw(0));
      end case;
      for k in 0 to 3 loop
        p3pos(k) <= p2base + p2p(k);
        if p2r(k).far = '1' then
          p3len(k) <= flen + 1;
        else
          p3len(k) <= resize(p2r(k).len, 32);
        end if;
      end loop;
      p3posl <= p2base + p2last;
      p3hl   <= flen + (p2hdrl + 1);
      -- T - H.base = p_last + hdr + 1 + raw (H.base is 16-aligned).
      p3dist <= flen + (resize(p2hdrl, 6) + 1 + p2last);

      -- PE3b: element records, far target.
      wcnt_r <= (others => '0');
      if p3v = '1' then
        wcnt_r <= p3cnt;
      end if;
      for k in 0 to 3 loop
        el_v := ELEMENT_INIT;
        if p3v = '1' and k < to_integer(p3cnt) then
          el_v.valid := '1';
        end if;
        if p3eoc = '1' then
          el_v.kind := K_EOC;
          el_v.lptr := p3pos(k);
        elsif p3r(k).kind = K_LIT then
          el_v.kind := K_LIT;
          el_v.len  := p3len(k);
          el_v.lptr := p3pos(k) + p3r(k).hdr;
        else
          el_v.kind := K_CPY;
          el_v.len  := p3len(k);
          el_v.off  := p3r(k).off;
          el_v.lptr := p3pos(k);
        end if;
        el_r(k) <= el_v;
      end loop;
      far_v   <= p3far and p3v;
      far_tgt <= p3posl + p3hl;
      if p3dist(31 downto 7) = 0 and p3dist(6 downto 0) < 80 then
        far_near <= '1';
      else
        far_near <= '0';
      end if;
      far_n <= p3dist(6 downto 4);
      far_e <= p3dist(3 downto 0);

      if reset = '1' then
        mode    <= M_DRAIN;
        e_r     <= (others => '0');
        stp     <= (others => '0');
        epoch_w <= "00";
        cred_r  <= to_unsigned(ELQ_CRED, 7);
        retgt_r <= '0';
        eoc_r   <= '0';
        p1v     <= '0';
        p2v     <= '0';
        p3v     <= '0';
        p3far   <= '0';
        wcnt_r  <= (others => '0');
        far_v   <= '0';
        for k in 0 to 3 loop
          el_r(k).valid <= '0';
        end loop;
      end if;
    end if;
  end process;

  wf_pop    <= pop_s;
  el        <= el_r;
  wcnt      <= wcnt_r;
  retgt_v   <= retgt_r;
  retgt_tgt <= tgt_r;
  eoc_p     <= eoc_r;
  cred      <= cred_r;

  -- pragma translate_off
  chk_proc: process (clk) is
    variable d : ga_t;
  begin
    if rising_edge(clk) then
      if reset = '0' then
        assert cred_r <= ELQ_CRED
          report "walker: credit count " & integer'image(to_integer(cred_r))
                 & " exceeds ELQ_CRED" severity failure;
        assert not (pop_s = '1' and wf_head.valid = '0')
          report "walker: pop of an empty WFIFO" severity failure;
        if mode = M_RUN and emit_s = '1' and eoc_s = '0' then
          assert wf_head.nxt4(to_integer(e_r)) = FAR_NXT
                 or wf_head.nxt4(to_integer(e_r)) <= 92
            report "walker: nxt4 out of range" severity failure;
        end if;
        assert not (far_v = '1' and mode /= M_WAITFAR)
          report "walker: far target outside WAITFAR" severity failure;
        if far_v = '1' and mode = M_WAITFAR then
          d := far_tgt - wf_head.base;
          assert (d < 80) = (far_near = '1')
                 and (far_near = '0' or d(6 downto 0) = far_n & far_e)
            report "walker: far distance mismatch" severity failure;
        end if;
      end if;
    end if;
  end process;

  -- Probe (PROBE true): walker cycle classes, rewritten to probe_wk.txt in
  -- the simulation's working directory every 16384 cycles and at every EOC.
  probe_g: if PROBE generate
    probe_p: process (clk) is
      use std.textio.all;
      type cnt_arr is array (natural range <>) of natural;
      -- 0 run-emit, 1 run-nowin, 2 run-nocred, 3 stale, 4 step, 5 step-nowin,
      -- 6 waitfar, 7 drain-pop, 8 drain-nowin, 9 drain-start
      variable cl  : cnt_arr(0 to 9) := (others => 0);
      constant CN  : string := "run-emit  run-nowin run-nocredstale     step      step-nowinwaitfar   drain-pop drain-nowidrain-strt";
      variable cyc, els, wfe : natural := 0;
      variable c   : natural;
      file f       : text;
      variable l   : line;
    begin
      if rising_edge(clk) and reset = '0' then
        cyc := cyc + 1;
        els := els + to_integer(wcnt_r);
        if wf_head.valid = '0' then wfe := wfe + 1; end if;
        if wf_head.valid = '1' and wf_head.epoch /= epoch_w then
          c := 3;
        else
          case mode is
            when M_RUN =>
              if wf_head.valid = '0' then c := 1;
              elsif cred_r < 4 then c := 2;
              else c := 0; end if;
            when M_STEP =>
              if wf_head.valid = '0' then c := 5; else c := 4; end if;
            when M_WAITFAR => c := 6;
            when M_DRAIN =>
              if wf_head.valid = '0' then c := 8;
              elsif wf_head.first = '1' then c := 9;
              else c := 7; end if;
          end case;
        end if;
        cl(c) := cl(c) + 1;
        if cyc mod 16384 = 0 or eoc_s = '1' then
          file_open(f, "probe_wk.txt", write_mode);
          write(l, string'("cycles ") & integer'image(cyc) & " elements " & integer'image(els)
                & " wfifo_empty " & integer'image(wfe));
          writeline(f, l);
          for j in 0 to 9 loop
            write(l, CN(10 * j + 1 to 10 * j + 10) & " " & integer'image(cl(j)));
            writeline(f, l);
          end loop;
          file_close(f);
        end if;
      end if;
    end process;
  end generate;

  -- Group trace in gold's groups.txt format:
  --   chunk(4) base(8) e(2) wcnt(1) p1(2) p2(2) p3(2) p4(2) act(1) arg(2) el(6)
  trace_gen: if GRP_FILE /= "-" generate
    trace_proc: process (clk) is
      file fo        : text;
      variable opened : boolean := false;
      variable ln    : line;
      variable ci    : natural := 0;
      variable eln   : natural := 0;
      variable pend  : boolean := false;
      variable pel   : natural := 0;
      variable pline : line;
      variable c     : natural;
      variable ok    : boolean;
      variable x     : unsigned(6 downto 0);
      variable ps    : u7_arr(1 to 4);
      variable dist  : ga_t;
      variable act, arg : natural;

      function hx(v : unsigned; nd : positive) return string is
        constant HD : string(1 to 16) := "0123456789ABCDEF";
        variable vv : unsigned(4 * nd - 1 downto 0) := resize(v, 4 * nd);
        variable s  : string(1 to nd);
      begin
        for i in 0 to nd - 1 loop
          s(nd - i) := HD(to_integer(vv(4 * i + 3 downto 4 * i)) + 1);
        end loop;
        return s;
      end function;
      function hn(v : natural; nd : positive) return string is
      begin
        return hx(to_unsigned(v, 4 * nd), nd);
      end function;
    begin
      if rising_edge(clk) then
        if not opened then
          file_open(fo, GRP_FILE, write_mode);
          opened := true;
        end if;
        if reset = '0' then
          -- A pending far group resolves in WAITFAR.
          if pend and mode = M_WAITFAR and far_v = '1' then
            dist := far_tgt - wf_head.base;
            if rtg_s = '1' then
              act := 3; arg := 0;
            else
              act := 2; arg := to_integer(dist(6 downto 4));
            end if;
            write(pline, " " & hn(act, 1) & " " & hn(arg, 2) & " " & hn(pel, 6));
            writeline(fo, pline);
            pend := false;
          end if;
          if emit_s = '1' then
            x := wf_head.nxt4(to_integer(e_r));
            ps(1) := resize(e_r, 7);
            ps(2) := wf_head.nxt(to_integer(e_r));
            ps(3) := wf_head.nxt2(to_integer(e_r));
            ps(4) := wf_head.nxt3(to_integer(e_r));
            if eoc_s = '1' then
              write(ln, hn(ci, 4) & " " & hx(wf_head.base, 8) & " " & hx(e_r, 2) & " 1 "
                        & hx(e_r, 2) & " 7F 7F 7F E 00 " & hn(eln, 6));
              writeline(fo, ln);
              eln := eln + 1;
              ci := ci + 1;
            else
              c := 1;
              ok := true;
              for k in 2 to 4 loop
                ok := ok and ps(k) < 32 and resize(ps(k), 6) < wf_head.endrel;
                if ok then c := c + 1; end if;
              end loop;
              write(pline, hn(ci, 4) & " " & hx(wf_head.base, 8) & " " & hx(e_r, 2) & " "
                           & hn(c, 1) & " " & hx(ps(1), 2) & " " & hx(ps(2), 2) & " "
                           & hx(ps(3), 2) & " " & hx(ps(4), 2));
              if x = FAR_NXT then
                pend := true;             -- act/arg/el written when WAITFAR resolves
                pel  := eln;
              else
                if x < 16 then
                  act := 0; arg := 0;
                elsif rtg_s = '1' then
                  act := 3; arg := 0;
                else
                  act := 1; arg := to_integer(x(6 downto 4));
                end if;
                write(pline, " " & hn(act, 1) & " " & hn(arg, 2) & " " & hn(eln, 6));
                writeline(fo, pline);
              end if;
              eln := eln + c;
            end if;
          end if;
        end if;
      end if;
    end process;
  end generate;
  -- pragma translate_on

end behavior;
