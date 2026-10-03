library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.vhsnunzip_dsw4_pkg.all;

-- DSW-4 writer WR, LOOP L1 (SPEC 3.7).
--
-- Holds the element window E0..E7 (registers with registered derived fields)
-- and the head remainder, and issues at most one command per cycle (<= 4
-- segments, <= 32 bytes, <= 2 literal segments).
--
--   pf / nvis      ELQ prefetch: the 4 entries after the window, and how many
--                  of them are valid.
--   pf_adv         PF entries loaded into the window at this clock edge
--                  (0..4, <= nvis). A window entry is only ever loaded from a
--                  valid PF entry, so it is never stale.
--   rp_adv         elements retired by this cycle's command (c, EOC included;
--                  0 when no command issues). Combinational (late).
--   a_r            arrival counter from vhsnunzip_cbuf (2 cycles old): a
--                  literal byte at GA x may be placed iff ga_sdiff(a_r, x) > 0.
--   de_credit_ok   registered de FIFO credit (free >= DE_CRED).
--   cmd            the command, registered; cmd.valid = issued this cycle.
--
-- Simulation knobs, all off by default. Each must give bit-identical output
-- (gold.py cmds_<variant>.txt): TEST_SLOTS = KMAX (1..4); TEST_CUT (LFSR
-- budget cut, LFSR defined in gold.py); TEST_NOREP; TEST_LITP1 (at most one
-- literal segment); TEST_STALL_PCT (random issue stalls, percent).
--
-- Structure (LOOP L1). Every register below is either a decision input or a
-- next-state value; the decision compares only r (7 b) against registered
-- per-slot constants, and every next-state value is computed from registers
-- in parallel with the decision and only SELECTED by it:
--
--   window   E(0..7): element + l7 = sat7(len), o7 = sat7(off),
--            av = sat6(a_r - lptr) (refreshed every cycle with the current
--            a_r, so it is one cycle old when used: conservative), isL/isC/isE,
--            rep (off in {1,2,4,8,16}).
--   nv       valid entries in the window (a prefix, 0..8).
--   nu       usable entries (a prefix, <= nv): entries that were already in
--            the window registers last cycle, so their per-slot constants and
--            the head state were computed from registers. An entry loaded from
--            PF becomes usable one cycle after it is loaded. nu' = nv - c.
--   head     remainder of E0: R0 (32 b), r7 = sat7(R0), q0, q7 = sat7(q0),
--            lp0, av0 = sat6(a_r - lp0), rep0, cap = the slot-0 cap
--            (LIT min(B, av0); REP copy B; other copy min(B, q7)).
--   bnd(k)   per-slot constants for slots 1..3 (SPEC 3.7 table), computed
--            last cycle for every shift c' in 0..4 and selected by c.
--   B        this command's budget (32; TEST_CUT: LFSR budget).
--
-- Decision (from registers only): slot 0 n0 = min(r, cap), done0 = r <= cap;
-- slot k reached iff slots before it are done (and not EOC), k < nu,
-- k < KMAX and r < bud_k; then LIT: nl_k < LITP and (r <= endb_k ? fav_k :
-- r >= avb_k); CPY: r < haz_k, done iff r <= endh_k. Cut lengths: slot 0
-- n0 = cap (a register), slot k n_k = cutv_k - r (cutv_k = bud_k for LIT,
-- min(bud_k, haz_k) for CPY, a register). So every remainder after a cut is
-- register +/- register:
--   slot 0 cut: R0 - cap, lp0 + cap, q0 := qdbl (doubling, SPEC 5.2, from
--               registers: produced = len0 - R0 before this command)
--   slot k cut: R0 = len_k + r - cutv_k, lp0 = lptr_k + cutv_k - r, q0 = off_k
--   shift c   : head = E_c fresh.
-- The shift one-hot and the outcome one-hot are replicated 8x (keep) to drive
-- the next-state selects (SPEC 3.7).
entity vhsnunzip_writer is
  generic (
    TEST_SLOTS     : natural := 4;
    TEST_CUT       : boolean := false;
    TEST_NOREP     : boolean := false;
    TEST_LITP1     : boolean := false;
    TEST_STALL_PCT : natural := 0
  );
  port (
    clk          : in  std_logic;
    reset        : in  std_logic;

    pf           : in  element_arr(0 to 3);
    nvis         : in  unsigned(6 downto 0);
    pf_adv       : out unsigned(2 downto 0);
    rp_adv       : out unsigned(2 downto 0);

    a_r          : in  ga_t;
    de_credit_ok : in  std_logic;

    cmd          : out element_stream
  );
end vhsnunzip_writer;

architecture behavior of vhsnunzip_writer is

  function b2sl(b : boolean) return std_logic is
  begin
    if b then return '1'; end if;
    return '0';
  end function;

  constant KMAX : natural := TEST_SLOTS;
  constant LITP : natural := 2 - boolean'pos(TEST_LITP1);
  constant REPK : std_logic := b2sl(not TEST_NOREP);
  -- TEST_STALL_PCT: stall when the stall LFSR's low 7 bits are below this.
  constant STALL_THR : natural := (TEST_STALL_PCT * 128) / 100;

  -----------------------------------------------------------------------------
  -- Window entry with registered derived fields.
  -----------------------------------------------------------------------------
  type wen_t is record
    kind : std_logic_vector(1 downto 0);
    len  : unsigned(31 downto 0);
    off  : unsigned(15 downto 0);
    lptr : ga_t;
    l7   : unsigned(6 downto 0);
    o7   : unsigned(6 downto 0);
    av   : unsigned(5 downto 0);
    isL  : std_logic;
    isC  : std_logic;
    isE  : std_logic;
    rep  : std_logic;
  end record;
  type wen_arr is array (natural range <>) of wen_t;

  constant WEN_INIT : wen_t := (
    kind => K_LIT, len => (others => '0'), off => (others => '0'),
    lptr => (others => '0'), l7 => (others => '0'), o7 => (others => '0'),
    av => (others => '0'), isL => '0', isC => '0', isE => '0', rep => '0');

  -- Head remainder.
  type hd_t is record
    R0   : unsigned(31 downto 0);
    r7   : unsigned(6 downto 0);
    q0   : unsigned(15 downto 0);
    q7   : unsigned(6 downto 0);
    lp0  : ga_t;
    av0  : unsigned(5 downto 0);
    rep0 : std_logic;
    cap  : unsigned(5 downto 0);
  end record;
  type hd_arr is array (natural range <>) of hd_t;

  constant HD_INIT : hd_t := (
    R0 => (others => '0'), r7 => (others => '0'), q0 => (others => '0'),
    q7 => (others => '0'), lp0 => (others => '0'), av0 => (others => '0'),
    rep0 => '0', cap => (others => '0'));

  -- Per-slot constants (slots 1..3). Signed 8 b, clamped to [-64, 127]: r is
  -- 0..127, and every clamped value keeps its compare result against r.
  type bnd_t is record
    bud  : signed(7 downto 0);     -- B - P_k             reached iff r < bud
    endb : signed(7 downto 0);     -- B - P_k - l7_k      LIT fits iff r <= endb
    avb  : signed(7 downto 0);     -- B - P_k - av_k      LIT cut ok iff r >= avb
    haz  : signed(7 downto 0);     -- o7_k - P_k (127: inf) CPY ok iff r < haz
    endh : signed(7 downto 0);     -- min(B,o7_k)-P_k-l7_k CPY done iff r <= endh
    fav  : std_logic;              -- av_k >= l7_k
    nl   : unsigned(1 downto 0);   -- literals before slot k (sat 3)
    p    : unsigned(5 downto 0);   -- P_k (sat 63): S_k = r + P_k
    cutv : unsigned(5 downto 0);   -- cut: n_k = cutv - r (LIT bud, CPY min(bud,haz))
  end record;
  type bnd_arr is array (1 to 3) of bnd_t;
  type bnd_arr2 is array (0 to 4) of bnd_arr;

  constant BND_INIT : bnd_t := (
    bud => (others => '0'), endb => (others => '0'), avb => (others => '0'),
    haz => (others => '0'), endh => (others => '0'), fav => '0',
    nl => (others => '0'), p => (others => '0'), cutv => (others => '0'));

  -----------------------------------------------------------------------------
  -- Helpers.
  -----------------------------------------------------------------------------

  -- sat6(a - p) for a GA difference read as signed: <= 0 gives 0.
  function satav(a, p : ga_t) return unsigned is
    variable d : unsigned(31 downto 0);
  begin
    d := a - p;
    if d(31) = '1' then
      return to_unsigned(0, 6);
    elsif d(30 downto 6) /= 0 then
      return to_unsigned(63, 6);
    else
      return d(5 downto 0);
    end if;
  end function;

  function is_repq(x : unsigned(15 downto 0)) return std_logic is
  begin
    if x = 1 or x = 2 or x = 4 or x = 8 or x = 16 then
      return '1';
    end if;
    return '0';
  end function;

  function mkent(e : element_t; a : ga_t) return wen_t is
    variable w : wen_t;
  begin
    w.kind := e.kind;
    w.len  := e.len;
    w.off  := e.off;
    w.lptr := e.lptr;
    w.l7   := sat(e.len, 7);
    w.o7   := sat(e.off, 7);
    w.av   := satav(a, e.lptr);
    w.isL  := '0';
    w.isC  := '0';
    w.isE  := '0';
    case e.kind is
      when K_LIT  => w.isL := '1';
      when K_CPY  => w.isC := '1';
      when others => w.isE := '1';
    end case;
    w.rep := is_repq(e.off);
    return w;
  end function;

  function min6(a, b : unsigned(5 downto 0)) return unsigned is
  begin
    if a < b then return a; end if;
    return b;
  end function;

  -- Slot-0 cap: LIT min(B, av0); REP copy B; other copy min(B, q7).
  function capf(isL, rep0 : std_logic; av0 : unsigned(5 downto 0);
                q7 : unsigned(6 downto 0); bb : unsigned(5 downto 0)) return unsigned is
  begin
    if isL = '1' then
      return min6(bb, av0);
    elsif rep0 = '1' then
      return bb;
    elsif q7 < resize(bb, 7) then
      return q7(5 downto 0);
    else
      return bb;
    end if;
  end function;

  function lfsr_step(s : unsigned(15 downto 0)) return unsigned is
    variable b : std_logic;
  begin
    b := s(0) xor s(2) xor s(3) xor s(5);
    return b & s(15 downto 1);
  end function;

  function lfsr_budget(s : unsigned(15 downto 0)) return unsigned is
  begin
    if s(1 downto 0) = "00" then
      return resize(s(8 downto 4), 6) + 1;
    end if;
    return to_unsigned(32, 6);
  end function;

  function clampi(x, lo, hi : integer) return integer is
  begin
    if x < lo then return lo; end if;
    if x > hi then return hi; end if;
    return x;
  end function;

  function s8(x : integer) return signed is
  begin
    return to_signed(clampi(x, -64, 127), 8);
  end function;

  -- Per-slot constants for a window y(0..3) (y(0) = head) with budget bb.
  function mkbnd(y : wen_arr(0 to 3); bb : unsigned(5 downto 0)) return bnd_arr is
    variable res  : bnd_arr;
    variable pk   : integer range 0 to 511;
    variable nl   : integer range 0 to 3;
    variable bi, l7, o7, av, bud, haz, mo : integer;
  begin
    bi := to_integer(bb);
    pk := 0;
    if y(0).isL = '1' then nl := 1; else nl := 0; end if;
    for k in 1 to 3 loop
      l7  := to_integer(y(k).l7);
      o7  := to_integer(y(k).o7);
      av  := to_integer(y(k).av);
      bud := bi - pk;
      res(k).bud  := s8(bud);
      res(k).endb := s8(bud - l7);
      res(k).avb  := s8(bud - av);
      if o7 = 127 then
        haz := 127;
      else
        haz := o7 - pk;
      end if;
      res(k).haz  := s8(haz);
      if o7 < bi then mo := o7; else mo := bi; end if;
      res(k).endh := s8(mo - pk - l7);
      if av >= l7 then res(k).fav := '1'; else res(k).fav := '0'; end if;
      res(k).nl   := to_unsigned(nl, 2);
      res(k).p    := to_unsigned(clampi(pk, 0, 63), 6);
      if y(k).isL = '1' or haz > bud then
        res(k).cutv := to_unsigned(clampi(bud, 0, 63), 6);
      else
        res(k).cutv := to_unsigned(clampi(haz, 0, 63), 6);
      end if;
      -- Next slot: P += l7 (saturated: P >= 32 is unreachable anyway).
      pk := clampi(pk + l7, 0, 63);
      if y(k).isL = '1' and nl < 3 then
        nl := nl + 1;
      end if;
    end loop;
    return res;
  end function;

  -- Fresh head from a window entry (w.av already refreshed).
  function mkhead(w : wen_t; bb : unsigned(5 downto 0)) return hd_t is
    variable h : hd_t;
  begin
    h.R0   := w.len;
    h.r7   := w.l7;
    h.q0   := w.off;
    h.q7   := w.o7;
    h.lp0  := w.lptr;
    h.av0  := w.av;
    h.rep0 := REPK and w.isC and w.rep;
    h.cap  := capf(w.isL, h.rep0, h.av0, h.q7, bb);
    return h;
  end function;

  -- Offset doubling (SPEC 5.2): q doubles while q < 32 and
  -- 2q <= produced + off, produced = len0 - R0 before this command.
  function qdbl(q0, off0 : unsigned(15 downto 0); len0, r0 : unsigned(31 downto 0))
    return unsigned is
    variable q    : unsigned(16 downto 0);
    variable prod : unsigned(16 downto 0);
    variable lim  : unsigned(16 downto 0);
  begin
    q    := resize(q0, 17);
    -- copies are at most 64 bytes long, so produced fits 7 bits
    prod := resize(len0(7 downto 0) - r0(7 downto 0), 17);
    lim  := prod + resize(off0, 17);
    for i in 1 to 5 loop
      if q < 32 and (q(15 downto 0) & '0') <= lim then
        q := q(15 downto 0) & '0';
      end if;
    end loop;
    return q(15 downto 0);
  end function;

  -----------------------------------------------------------------------------
  -- Registers.
  -----------------------------------------------------------------------------
  signal E       : wen_arr(0 to 7) := (others => WEN_INIT);
  signal nv, nu  : unsigned(3 downto 0) := (others => '0');
  signal hd      : hd_t := HD_INIT;
  signal bnd     : bnd_arr := (others => BND_INIT);
  signal bb      : unsigned(5 downto 0) := to_unsigned(32, 6);
  signal lfsr    : unsigned(15 downto 0) := X"ACE1";
  signal slfsr   : unsigned(15 downto 0) := X"1D35";
  signal bubble  : std_logic := '0';
  signal cmd_r   : element_stream := ELEMENT_STREAM_INIT;

  -----------------------------------------------------------------------------
  -- Decision outputs.
  -----------------------------------------------------------------------------
  signal issue   : std_logic;
  signal done    : std_logic_vector(0 to 3);   -- slot k done (EOC included)
  signal plcd    : std_logic_vector(0 to 3);   -- slot k placed (used)
  signal cmd_n   : element_stream;

  -- Outcome one-hot, replicated: 0 HOLD, 1 INIT, 2 C0 (slot 0 cut),
  -- 3..6 shift by 1..4, 7..9 slot 1..3 cut.
  subtype oc_t is std_logic_vector(0 to 9);
  type oc_rep_t is array (0 to 7) of oc_t;
  -- Shift one-hot (c = 0..4, 0 also when no command issues), replicated.
  subtype sh_t is std_logic_vector(0 to 4);
  type sh_rep_t is array (0 to 7) of sh_t;
  signal oc_rep  : oc_rep_t;
  signal sh_rep  : sh_rep_t;
  attribute keep : string;
  attribute keep of oc_rep : signal is "true";
  attribute keep of sh_rep : signal is "true";

  -- Next-state candidates (from registers only).
  signal bnext   : unsigned(5 downto 0);       -- budget of the next command
  signal lfsr_n  : unsigned(15 downto 0);
  signal X       : wen_arr(0 to 11);           -- extended window, av refreshed
  signal npf     : unsigned(2 downto 0);       -- min(4, nvis)
  signal hcand   : hd_arr(0 to 9);             -- head per outcome
  signal bcand   : bnd_arr2;                   -- bundle per shift c'
  signal stall   : std_logic;

begin

  -----------------------------------------------------------------------------
  -- Decision (combinational, against registered constants only).
  -----------------------------------------------------------------------------
  stall <= '1' when slfsr(6 downto 0) < STALL_THR else '0';

  dec_p: process (E, nu, hd, bnd, bb, bubble, de_credit_ok, stall) is
    variable r       : signed(7 downto 0);
    variable iss     : std_logic;
    variable dn, pl  : std_logic_vector(0 to 3);
    variable reach   : std_logic;
    variable fit     : std_logic;
    variable ok      : std_logic;
    variable n       : unsigned(5 downto 0);
    variable d       : unsigned(5 downto 0);
    variable c       : integer range 0 to 4;
    variable cutk    : integer range 0 to 3;
    variable cutany  : std_logic;
    variable lst     : std_logic;
    variable cm      : element_stream;
    variable sl      : wslot_t;
  begin
    r := signed('0' & hd.r7);
    cm := ELEMENT_STREAM_INIT;
    dn := (others => '0');
    pl := (others => '0');
    cutk := 0;
    cutany := '0';
    lst := '0';

    -- issue: a usable head, data for a literal head, credit, no bubble.
    iss := '0';
    if nu /= 0 and de_credit_ok = '1' and bubble = '0' and stall = '0' then
      if E(0).isL = '0' or hd.cap /= 0 then
        iss := '1';
      end if;
    end if;

    -- Slot 0.
    sl := WSLOT_INIT;
    sl.val  := '1';
    sl.kind := E(0).kind;
    pl(0) := '1';
    if E(0).isE = '1' then
      dn(0) := '1';
      lst := '1';
      sl.lptr := E(0).lptr;
      n := (others => '0');
    else
      if hd.r7 <= resize(hd.cap, 7) then
        dn(0) := '1';
        n := hd.r7(5 downto 0);
      else
        n := hd.cap;
        cutany := '1';
      end if;
      if E(0).isL = '1' then
        sl.lptr := hd.lp0;
      else
        sl.lptr := E(0).lptr;
        sl.q    := hd.q0;
      end if;
    end if;
    sl.n := n;
    cm.slot(0) := sl;
    d := n;
    if E(0).isC = '1' then
      cm.cp0_val := '1';
      cm.rep     := hd.rep0;
    elsif E(0).isL = '1' then
      cm.li0_val := '1';
    end if;
    if E(0).isL = '1' then
      cm.lw := hd.lp0;
    else
      cm.lw := E(0).lptr;
    end if;

    -- Slots 1..3.
    for k in 1 to 3 loop
      reach := '0';
      if dn(k - 1) = '1' and E(k - 1).isE = '0' and nu > k and k < KMAX
         and r < bnd(k).bud then
        reach := '1';
      end if;
      fit := '0';
      ok  := '0';
      if E(k).isE = '1' then
        ok  := '1';
        fit := '1';
      elsif E(k).isL = '1' then
        if r <= bnd(k).endb then
          fit := '1';
          ok  := bnd(k).fav;
        elsif r >= bnd(k).avb then
          ok  := '1';
        end if;
        if bnd(k).nl >= LITP then
          ok := '0';
        end if;
      else
        if r < bnd(k).haz then
          ok := '1';
        end if;
        if r <= bnd(k).endh then
          fit := '1';
        end if;
      end if;
      pl(k) := reach and ok;
      dn(k) := reach and ok and fit;
      if pl(k) = '1' then
        sl := WSLOT_INIT;
        sl.val  := '1';
        sl.kind := E(k).kind;
        sl.s    := unsigned(r(5 downto 0)) + bnd(k).p;
        sl.lptr := E(k).lptr;
        if E(k).isE = '1' then
          n := (others => '0');
          lst := '1';
        elsif fit = '1' then
          n := E(k).l7(5 downto 0);
        else
          n := bnd(k).cutv - unsigned(r(5 downto 0));
          cutany := '1';
          cutk := k;
        end if;
        sl.n := n;
        if E(k).isL = '1' then
          sl.port_b := bnd(k).nl(0);
          if bnd(k).nl(0) = '1' then
            cm.li1_val := '1';
          else
            cm.li0_val := '1';
          end if;
        elsif E(k).isC = '1' then
          sl.q := E(k).off;
          case k is
            when 1 => cm.cp1_val := '1';
            when 2 => cm.cp2_val := '1';
            when others => cm.cp3_val := '1';
          end case;
        end if;
        cm.slot(k) := sl;
        d := d + n;
      end if;
    end loop;

    c := 0;
    for k in 0 to 3 loop
      if dn(k) = '1' then
        c := c + 1;
      end if;
    end loop;

    cm.valid   := iss;
    cm.last    := lst;
    cm.d_total := d;
    cm.c       := to_unsigned(c, 3);
    cm.cut     := cutany;
    if iss = '0' then
      cm := ELEMENT_STREAM_INIT;
    end if;

    issue <= iss;
    done  <= dn;
    plcd  <= pl;
    cmd_n <= cm;
  end process;

  -- Replicated outcome / shift one-hots (SPEC 3.7: c, cut and the one-hot
  -- shift are replicated 8x for the next-state fan-out).
  rep_g: for g in 0 to 7 generate
    oc_p: process (issue, done, plcd, nu) is
      variable oc : oc_t;
      variable sh : sh_t;
      variable c  : integer range 0 to 4;
    begin
      oc := (others => '0');
      sh := (others => '0');
      c := 0;
      for k in 0 to 3 loop
        if done(k) = '1' then
          c := c + 1;
        end if;
      end loop;
      if issue = '0' then
        if nu = 0 then
          oc(1) := '1';
        else
          oc(0) := '1';
        end if;
        sh(0) := '1';
      elsif done(0) = '0' then
        oc(2) := '1';
        sh(0) := '1';
      elsif plcd(1) = '1' and done(1) = '0' then
        oc(7) := '1';
        sh(1) := '1';
      elsif plcd(2) = '1' and done(2) = '0' then
        oc(8) := '1';
        sh(2) := '1';
      elsif plcd(3) = '1' and done(3) = '0' then
        oc(9) := '1';
        sh(3) := '1';
      else
        oc(2 + c) := '1';
        sh(c) := '1';
      end if;
      oc_rep(g) <= oc;
      sh_rep(g) <= sh;
    end process;
  end generate;

  -----------------------------------------------------------------------------
  -- Next-state candidates (registers only; no decision input).
  -----------------------------------------------------------------------------
  lfsr_p: process (lfsr, issue) is
    variable s : unsigned(15 downto 0);
  begin
    s := lfsr;
    if TEST_CUT and issue = '1' then
      s := lfsr_step(lfsr);
    end if;
    lfsr_n <= s;
    if TEST_CUT then
      bnext <= lfsr_budget(s);
    else
      bnext <= to_unsigned(LINE_B, 6);
    end if;
  end process;

  npf <= "100" when nvis >= 4 else nvis(2 downto 0);

  -- Extended window X(j) = E(j) for j < nv, PF(j - nv) after; av refreshed.
  x_p: process (E, nv, pf, a_r) is
    variable w : wen_t;
    variable k : integer;
  begin
    for j in 0 to 11 loop
      if j < 8 and j < to_integer(nv) then
        w := E(j);
        w.av := satav(a_r, E(j).lptr);
      else
        k := j - to_integer(nv);
        if k >= 0 and k <= 3 then
          w := mkent(pf(k), a_r);
        else
          w := WEN_INIT;
        end if;
      end if;
      X(j) <= w;
    end loop;
  end process;

  -- Head candidates and per-shift bundles. Uses only E (registers) for the
  -- bundles and the shifted heads: a slot whose entry would come from PF is
  -- not usable next cycle (nu' = nv - c), so its values do not matter.
  cand_p: process (E, hd, bnd, bnext, a_r) is
    variable Ea   : wen_arr(0 to 7);
    variable y    : wen_arr(0 to 3);
    variable h    : hd_t;
    variable r    : unsigned(5 downto 0);
    variable lp   : ga_t;
    variable nr   : unsigned(31 downto 0);
  begin
    for j in 0 to 7 loop
      Ea(j) := E(j);
      Ea(j).av := satav(a_r, E(j).lptr);
    end loop;
    r := hd.r7(5 downto 0);

    -- 0 HOLD: same head, av0 refreshed.
    h := hd;
    h.av0 := satav(a_r, hd.lp0);
    h.cap := capf(E(0).isL, hd.rep0, h.av0, hd.q7, bnext);
    hcand(0) <= h;

    -- 1 INIT: fresh head from E0.
    hcand(1) <= mkhead(Ea(0), bnext);

    -- 2 slot 0 cut: n0 = cap.
    h := hd;
    nr := hd.R0 - resize(hd.cap, 32);
    h.R0 := nr;
    h.r7 := sat(nr, 7);
    if E(0).isL = '1' then
      h.lp0 := hd.lp0 + resize(hd.cap, 32);
    end if;
    h.av0 := satav(a_r, h.lp0);
    if E(0).isC = '1' then
      h.q0 := qdbl(hd.q0, E(0).off, E(0).len, hd.R0);
    end if;
    h.q7 := sat(h.q0, 7);
    h.rep0 := REPK and E(0).isC and is_repq(h.q0);
    h.cap := capf(E(0).isL, h.rep0, h.av0, h.q7, bnext);
    hcand(2) <= h;

    -- 3..6 shift by 1..4: fresh head from E(c).
    for c in 1 to 4 loop
      hcand(2 + c) <= mkhead(Ea(c), bnext);
    end loop;

    -- 7..9 slot k cut: n_k = cutv_k - r.
    for k in 1 to 3 loop
      h := mkhead(Ea(k), bnext);
      nr := E(k).len + resize(r, 32) - resize(bnd(k).cutv, 32);
      h.R0 := nr;
      h.r7 := sat(nr, 7);
      lp := E(k).lptr + resize(bnd(k).cutv, 32) - resize(r, 32);
      h.lp0 := lp;
      h.av0 := satav(a_r, lp);
      h.cap := capf(E(k).isL, h.rep0, h.av0, h.q7, bnext);
      hcand(6 + k) <= h;
    end loop;

    -- Bundles for every shift c' (slots 1..3 of the shifted window).
    for c in 0 to 4 loop
      for i in 0 to 3 loop
        y(i) := Ea(c + i);
      end loop;
      bcand(c) <= mkbnd(y, bnext);
    end loop;
  end process;

  -----------------------------------------------------------------------------
  -- Select and register.
  -----------------------------------------------------------------------------
  -- Late counts for the ELQ: c and the PF loads.
  cnt_p: process (sh_rep, nv, npf) is
    variable c    : integer;
    variable rem0 : integer;
    variable ld   : integer;
  begin
    c := 0;
    for i in 0 to 4 loop
      if sh_rep(7)(i) = '1' then
        c := i;
      end if;
    end loop;
    rem0 := clampi(to_integer(nv) - c, 0, 8);
    ld := clampi(to_integer(npf), 0, 4);
    if ld > 8 - rem0 then
      ld := 8 - rem0;
    end if;
    rp_adv <= to_unsigned(c, 3);
    pf_adv <= to_unsigned(ld, 3);
  end process;

  reg_p: process (clk) is
    variable c    : integer range 0 to 4;
    variable rem0 : integer range 0 to 8;
    variable nvn  : integer range 0 to 12;
  begin
    if rising_edge(clk) then
      -- Window: E(i)' = X(i + c), late 5:1 by the replicated shift.
      for i in 0 to 7 loop
        for s in 0 to 4 loop
          if sh_rep(i)(s) = '1' then
            E(i) <= X(i + s);
          end if;
        end loop;
      end loop;

      c := 0;
      for s in 0 to 4 loop
        if sh_rep(6)(s) = '1' then
          c := s;
        end if;
      end loop;
      rem0 := to_integer(nv) - c;
      nvn := rem0 + to_integer(npf);
      if nvn > 8 then
        nvn := 8;
      end if;
      nv <= to_unsigned(nvn, 4);
      nu <= to_unsigned(rem0, 4);

      -- Head: 10:1 by the replicated outcome.
      for o in 0 to 9 loop
        if oc_rep(4)(o) = '1' then
          hd <= hcand(o);
        end if;
      end loop;

      -- Bundle: 5:1 by the replicated shift.
      for s in 0 to 4 loop
        if sh_rep(5)(s) = '1' then
          bnd <= bcand(s);
        end if;
      end loop;

      bb     <= bnext;
      lfsr   <= lfsr_n;
      slfsr  <= lfsr_step(slfsr);
      bubble <= issue and cmd_n.last;
      cmd_r  <= cmd_n;

      if reset = '1' then
        nv     <= (others => '0');
        nu     <= (others => '0');
        bb     <= to_unsigned(LINE_B, 6);
        lfsr   <= X"ACE1";
        slfsr  <= X"1D35";
        bubble <= '0';
        cmd_r  <= ELEMENT_STREAM_INIT;
      end if;
    end if;
  end process;

  cmd <= cmd_r;

  -- pragma translate_off
  chk_p: process (clk) is
  begin
    if rising_edge(clk) and reset = '0' then
      assert nu <= nv and nv <= 8 report "writer: window count out of range" severity failure;
      if cmd_n.valid = '1' then
        assert cmd_n.d_total <= bb report "writer: command over budget" severity failure;
      end if;
    end if;
  end process;
  -- pragma translate_on

end behavior;
