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
--                  0 when no command issues). Late (one-hot select).
--   a_r            arrival counter: the CBUF's GB register itself (A0). The
--                  writer registers it twice: a1 = A0 one cycle old, and
--                  A_r = a1 one cycle old (SPEC 3.1, GB two cycles old) is
--                  implied. Every availability field is computed one cycle
--                  ahead from the earlier values and REGISTERED, so it holds
--                  exactly sat7(A_r - lptr) (av) or sat7(A_r' - lptr) with the
--                  next cycle's A_r (avx) in the cycle it is used. A literal
--                  byte at GA x is placed only if it is below the A_r of the
--                  previous cycle (conservative, SPEC 3.7 "with the old A").
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
-- in parallel with the decision and only SELECTED by it (no arithmetic after
-- the decision, SPEC 3.7 / 8):
--
--   window   E(0..7): element (len / off in the packed (u, lo) form it arrives
--            in, see wen_t) + l7 = sat7(len), o7 = sat7(off),
--            av = sat7(A_r - lptr) for THIS cycle's A_r and avx = the same
--            with the NEXT cycle's A_r (registers: computed last cycle from
--            the early arrival values), rhnz = len >= 128,
--            rh2 = len >= 256, isL/isC/isE, rep (off in {1,2,4,8,16}).
--   nv       valid entries in the window (a prefix, 0..8).
--   nu       usable entries (a prefix, <= nv): entries that were already in
--            the window registers last cycle, so their per-slot constants and
--            the head state were computed from registers. An entry loaded from
--            PF becomes usable one cycle after it is loaded. nu' = nv - c.
--   head     remainder of E0, SPEC 3.7 "head remainder":
--              R0 = rl + 128*rh (rl 8 b, rh 25 b, rhnz = rh /= 0) with the
--              pipelined borrow: brw = rhnz and rl(7) = 0 makes this cycle's
--              slot-0 cut use rl with bit 7 set and rh - 1 (off-loop). Copies
--              have rh = 0. r7 = sat7(R0) (127 whenever rhnz).
--              q0 / q7 / rep0: effective offset of the head copy.
--              lim = sat7(produced + off), produced = len0 - R0.
--              dq: qdbl(q0, lim), the q0 a slot-0 cut hands to the next
--              command (SPEC 5.2, DBLLAG = 1). It is computed one cycle ahead
--              for every outcome and registered, so the doubling is never in
--              the hd -> hd recurrence: a slot-0 cut takes q0 := dq (and
--              sat7 / REP of the register dq), and the in-cycle doubling work
--              (7-bit add, 5 PARALLEL compares, AND-OR select) feeds only the
--              dq register.
--              lp0: GA of the head literal's next byte.
--              avn = sat7(A_r - lp0) for this cycle's A_r.
--              cap: slot-0 cap for this cycle (LIT min(B, av0), av0 =
--              sat7(A_r(prev) - lp0); REP copy B; other copy min(B, q7)).
--   bnd(k)   per-slot constants for slots 1..3 (SPEC 3.7 table), computed
--            last cycle for every shift c' in 0..4 and selected by c. The
--            table is one value per slot plus two offsets of it:
--            cutv_k = min(B, o7_k) - P_k, ende_k = cutv_k - l7_k and
--            avb_k = cutv_k - av_k (see bnd_t).
--   B        this command's budget (32; TEST_CUT: LFSR budget).
--
-- Decision (from registers only): slot 0 n0 = min(r, cap), done0 = r <= cap;
-- slot k reached iff slots before it are done (and not EOC), k < nu,
-- k < KMAX and r < cutv_k (budget and copy hazard in one compare); then LIT:
-- nl_k < LITP and (r <= ende_k ? fav_k : r >= avb_k); CPY: always ok, done
-- iff r <= ende_k. Cut lengths: slot 0 n0 = cap (a register), slot k
-- n_k = cutv_k - r (a register minus r). Next-state candidates:
--   slot 0 cut: rl - cap (8 b, borrow from rh precomputed), lp0 + cap,
--               av0' = avn - cap (7 b), q0 := dq (register), dq' from dq,
--               lim + cap (7-bit add, parallel compares).
--   slot k cut: n_k = cutv_k - r once (ncut, shared with the decision's
--               segment length), then rl' = len_k(6:0) [+128] - n_k (8 b),
--               rh' = rh_k - 1, lp0' = lptr_k + n_k (GA add),
--               av0' = av_k - n_k (7 b), q0 = off_k,
--               dq' = qdbl(off_k, n_k + off_k).
--   shift c   : head = E_c fresh.
-- The shift one-hot and the outcome one-hot are built directly from the done
-- prefix (no counting) and replicated 8x (keep) to drive the next-state
-- selects (SPEC 3.7). The ELQ counts (pf_adv, rp_adv) and nv', nu' are
-- precomputed for every shift c' and selected by the same one-hot.
entity vhsnunzip_writer is
  generic (
    TEST_SLOTS     : natural := 4;
    TEST_CUT       : boolean := false;
    TEST_NOREP     : boolean := false;
    TEST_LITP1     : boolean := false;
    TEST_STALL_PCT : natural := 0;
    PROBE          : boolean := false   -- sim only: probe_wr.txt counters
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
  -- The kind code itself is NOT kept: isL / isC / isE are its decode, and the
  -- command's 2-bit code is the one LUT (isE, isC or isE) -- cheaper than
  -- carrying two more bits through the 12-entry candidate array and the
  -- 8 x 5:1 window shift select.
  -- len and off are kept in the element's packed form (dsw4_pkg element_t):
  -- u = len(31:7) for a literal / off for a copy, lo = len(6:0). Nothing in
  -- this module ever needs both halves of a 32-bit length and a 16-bit offset
  -- at once (rl = lo, rh = u, q = u(15:0)), so the window entry is 32 payload
  -- bits instead of 48 -- 16 bits less in the 12-entry candidate array, in the
  -- 8 x 5:1 window shift select and in the 8 window registers.
  type wen_t is record
    u    : unsigned(24 downto 0);
    lo   : unsigned(6 downto 0);
    lptr : gaw_t;
    l7   : unsigned(6 downto 0);
    o7   : unsigned(6 downto 0);
    av   : unsigned(6 downto 0);
    avx  : unsigned(6 downto 0);
    rhnz : std_logic;
    rh2  : std_logic;
    isL  : std_logic;
    isC  : std_logic;
    isE  : std_logic;
    rep  : std_logic;
  end record;
  type wen_arr is array (natural range <>) of wen_t;

  constant WEN_INIT : wen_t := (
    u => (others => '0'), lo => (others => '0'),
    lptr => (others => '0'), l7 => (others => '0'), o7 => (others => '0'),
    av => (others => '0'), avx => (others => '0'), rhnz => '0', rh2 => '0',
    isL => '0', isC => '0', isE => '0', rep => '0');

  -- Head remainder.
  type hd_t is record
    rl   : unsigned(7 downto 0);
    rh   : unsigned(24 downto 0);
    rhnz : std_logic;
    r7   : unsigned(6 downto 0);
    q0   : unsigned(15 downto 0);
    q7   : unsigned(6 downto 0);
    rep0 : std_logic;
    dq   : unsigned(15 downto 0);
    lim  : unsigned(6 downto 0);
    lp0  : gaw_t;
    avn  : unsigned(6 downto 0);
    cap  : unsigned(5 downto 0);
  end record;
  type hd_arr is array (natural range <>) of hd_t;

  constant HD_INIT : hd_t := (
    rl => (others => '0'), rh => (others => '0'), rhnz => '0',
    r7 => (others => '0'), q0 => (others => '0'), q7 => (others => '0'),
    rep0 => '0', dq => (others => '0'),
    lim => (others => '0'), lp0 => (others => '0'), avn => (others => '0'),
    cap => (others => '0'));

  -- Per-slot constants (slots 1..3). The whole SPEC 3.7 slot table is the one
  -- value cutv_k = min(B, o7_k) - P_k (clamped at 0) and two offsets of it:
  --   reached and inside the copy hazard : r <  cutv   (budget B - P_k and
  --       hazard o7_k - P_k are the two halves of the min, taken before the
  --       subtract, so one compare serves both and o7 = 127 needs no special
  --       "no hazard" value: 127 > B >= min discards it anyway)
  --   fits (LIT B-P-l7, CPY min(B,o7)-P-l7)  : r <= ende = cutv - l7
  --   literal availability (the old avb)      : r >= avb  = cutv - av
  -- Every operand compared against cutv is below 34 where it matters
  -- (cutv <= B <= 32 and r >= 0), so l7 / av enter the offsets clamped at 33
  -- and the bundle needs 7 b per offset instead of 8 b clamped to [-64, 127]:
  -- 29 b per slot instead of 55 b, three narrow subtracts instead of five
  -- wide ones with two-sided clamps, and the decision still compares r
  -- against a register only (no arithmetic in front of it).
  type bnd_t is record
    ende : signed(6 downto 0);     -- cutv - sat33(l7_k)  fits iff r <= ende
    avb  : signed(6 downto 0);     -- cutv - sat33(av_k)  LIT cut ok iff r >= avb
    fav  : std_logic;              -- av_k >= l7_k
    nl   : unsigned(1 downto 0);   -- literals before slot k (sat 3)
    p    : unsigned(5 downto 0);   -- P_k (sat 32): S_k = r + P_k
    cutv : unsigned(5 downto 0);   -- min(B, o7_k) - P_k; cut: n_k = cutv - r
  end record;
  type bnd_arr is array (1 to 3) of bnd_t;
  type bnd_arr2 is array (0 to 4) of bnd_arr;

  constant BND_INIT : bnd_t := (
    ende => (others => '0'), avb => (others => '0'), fav => '0',
    nl => (others => '0'), p => (others => '0'), cutv => (others => '0'));

  -----------------------------------------------------------------------------
  -- Helpers.
  -----------------------------------------------------------------------------

  -- sat7(a - p) for a GA difference read as signed: <= 0 gives 0. Both
  -- operands are inside the CBUF credit window (a - p is in [-64, +896], SPEC
  -- 3.1), so GAW = 12 bits of difference is exact and the sign is bit GAW-1.
  function satav(a : gaw_t; p : gaw_t) return unsigned is
    variable d : gaw_t;
  begin
    d := a - p;
    if d(GAW - 1) = '1' then
      return to_unsigned(0, 7);
    elsif d(GAW - 2 downto 7) /= 0 then
      return to_unsigned(127, 7);
    else
      return d(6 downto 0);
    end if;
  end function;

  function is_repq(x : unsigned(15 downto 0)) return std_logic is
  begin
    if x = 1 or x = 2 or x = 4 or x = 8 or x = 16 then
      return '1';
    end if;
    return '0';
  end function;

  -- Window entry from an ELQ element; a1 / a0: the arrival values whose
  -- registered av / avx this is (A_r and A_r of the cycle after the load).
  -- The element's len and off share its (u, lo) payload (dsw4_pkg): u is
  -- len(31:7) for a literal and off for a copy. Unpacking needs no mux and no
  -- gating of the wide fields:
  --   len = u & lo       -- exact for a literal; for a copy len(31:7) = off is
  --                         garbage, but rhnz = 0 and every read of rh (the
  --                         borrow in cand_p) is guarded by rhnz,
  --   off = u(15:0)      -- exact for a copy; for a literal it is len(31:7),
  --                         and every read of off / o7 / q / dq / rep is
  --                         guarded by isC, or by capf's isL branch, which
  --                         ignores q7 and rep0.
  -- Only the two one-bit "long literal" flags have to know the kind, and l7
  -- becomes a 7-bit select instead of a 32-bit saturate.
  function mkent(e : element_t; a1, a0 : gaw_t) return wen_t is
    variable w : wen_t;
  begin
    w.u    := e.u;
    w.lo   := e.lo;
    w.lptr := e.lptr;
    w.av   := satav(a1, w.lptr);
    w.avx  := satav(a0, w.lptr);
    w.isL  := '0';
    w.isC  := '0';
    w.isE  := '0';
    case e.kind is
      when K_LIT  => w.isL := '1';
      when K_CPY  => w.isC := '1';
      when others => w.isE := '1';
    end case;
    w.rhnz := w.isL and b2sl(e.u /= 0);
    w.rh2  := w.isL and b2sl(e.u(24 downto 1) /= 0);
    if w.rhnz = '1' then
      w.l7 := to_unsigned(127, 7);
    else
      w.l7 := e.lo;
    end if;
    w.o7   := sat(e.u(15 downto 0), 7);
    w.rep  := is_repq(e.u(15 downto 0));
    return w;
  end function;

  -- The command slot's 2-bit kind code, rebuilt from the decoded flags
  -- (dsw4_pkg: K_LIT "00", K_CPY "01", K_EOC "11", so kind = isE &
  -- (isC or isE)). One LUT, instead of two more bits in every window entry.
  function kcode(w : wen_t) return std_logic_vector is
  begin
    return w.isE & (w.isC or w.isE);
  end function;

  -- Slot-0 cap: LIT min(B, av0); REP copy B; other copy min(B, q7).
  function capf(isL, rep0 : std_logic; av0 : unsigned(6 downto 0);
                q7 : unsigned(6 downto 0); bb : unsigned(5 downto 0)) return unsigned is
  begin
    if isL = '1' then
      if av0 < resize(bb, 7) then
        return av0(5 downto 0);
      end if;
      return bb;
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


  -- Clamp a 7-bit length / availability at 33. Every value built from it is
  -- compared against cutv - r with cutv <= B <= 32 and r >= 0, so 33 and
  -- anything above it give the same compare result; this keeps the bundle
  -- offsets inside 7-bit signed arithmetic (no two-sided clamp).
  function c33(x : unsigned(6 downto 0)) return unsigned is
  begin
    if x > 33 then
      return to_unsigned(33, 6);
    end if;
    return x(5 downto 0);
  end function;

  -- Per-slot constants for a window y(0..3) (y(0) = head) with budget bb.
  -- All operands are registers (av included). One subtract builds cutv =
  -- min(B, o7_k) - P_k (the min is taken before the subtract, since B - P vs
  -- o7 - P is just B vs o7, and only a copy has a hazard), and two more make
  -- the fit and availability bounds as offsets of it. cutv in [0, 32], the
  -- offsets in [-33, 32]: 7-bit signed throughout, no clamp but the sign.
  function mkbnd(y : wen_arr(0 to 3); bb : unsigned(5 downto 0)) return bnd_arr is
    variable res  : bnd_arr;
    variable pk   : unsigned(5 downto 0);          -- P_k, saturated at 32
    variable nl   : unsigned(1 downto 0);
    variable m    : unsigned(5 downto 0);          -- min(B, o7_k)
    variable cv   : signed(6 downto 0);            -- m - P_k before the clamp
    variable cvu  : unsigned(5 downto 0);          -- cutv
    variable l7c  : unsigned(5 downto 0);          -- sat33(l7_k)
    variable psum : unsigned(6 downto 0);
  begin
    pk := (others => '0');
    if y(0).isL = '1' then nl := "01"; else nl := "00"; end if;
    for k in 1 to 3 loop
      l7c := c33(y(k).l7);
      if y(k).isC = '1' and y(k).o7 < resize(bb, 7) then
        m := y(k).o7(5 downto 0);
      else
        m := bb;
      end if;
      cv := signed('0' & m) - signed('0' & pk);
      if cv < 0 then
        cvu := (others => '0');
      else
        cvu := unsigned(cv(5 downto 0));
      end if;
      res(k).cutv := cvu;
      res(k).ende := signed('0' & cvu) - signed('0' & l7c);
      res(k).avb  := signed('0' & cvu) - signed('0' & c33(y(k).av));
      if y(k).av >= y(k).l7 then res(k).fav := '1'; else res(k).fav := '0'; end if;
      res(k).nl   := nl;
      res(k).p    := pk;
      -- Next slot: P += l7, saturated at 32. P >= B is unreachable (a slot is
      -- reached only while r + P < B <= 32), and once P saturates it stays
      -- saturated, so cutv = 0 hides every slot behind it, as it must.
      psum := resize(pk, 7) + resize(l7c, 7);
      if psum > 32 then
        pk := to_unsigned(32, 6);
      else
        pk := psum(5 downto 0);
      end if;
      if y(k).isL = '1' and nl /= 3 then
        nl := nl + 1;
      end if;
    end loop;
    return res;
  end function;

  -- Offset doubling (SPEC 5.2): q doubles while q < 32 and 2q <= lim, with
  -- lim = produced + off (saturated: only lim < 64 matters). The steps are
  -- monotone (once a step fails, every later one fails), so the 5 conditions
  -- cnd(i) = q*2^i < 32 and q*2^(i+1) <= lim, i = 0..4, are evaluated in
  -- PARALLEL and form a prefix; the result q*2^j (j = length of the prefix)
  -- is an AND-OR one-hot select (no serial compare chain, no counter, no
  -- barrel shifter).
  function qdbl(q : unsigned(15 downto 0); lim : unsigned(7 downto 0)) return unsigned is
    variable qs  : unsigned(10 downto 0);
    variable cnd : std_logic_vector(0 to 5);
    variable res : unsigned(15 downto 0);
  begin
    if q(15 downto 5) /= 0 then
      return q;
    end if;
    for i in 0 to 4 loop
      qs := shift_left(resize(q(4 downto 0), 11), i);
      cnd(i) := b2sl(qs < 32 and shift_left(qs, 1) <= resize(lim, 11));
    end loop;
    cnd(5) := '0';
    res := (others => '0');
    if cnd(0) = '0' then
      res := q;
    end if;
    for i in 0 to 4 loop
      if cnd(i) = '1' and cnd(i + 1) = '0' then
        res := res or shift_left(q, i + 1);
      end if;
    end loop;
    return res;
  end function;

  -- Fresh head from a window entry: av = sat7(A_r - lptr) for the decision
  -- cycle (the entry's registered av), avn = the same with the next A_r.
  function mkhead(w : wen_t; avn : unsigned(6 downto 0); bb : unsigned(5 downto 0)) return hd_t is
    variable h : hd_t;
  begin
    h.rl   := '0' & w.lo;
    h.rh   := w.u;
    h.rhnz := w.rhnz;
    h.r7   := w.l7;
    h.q0   := w.u(15 downto 0);
    h.q7   := w.o7;
    h.rep0 := REPK and w.isC and w.rep;
    -- produced = 0: qdbl(off, off) = off.
    h.dq   := w.u(15 downto 0);
    h.lim  := w.o7;
    h.lp0  := w.lptr;
    h.avn  := avn;
    h.cap  := capf(w.isL, h.rep0, w.av, w.o7, bb);
    return h;
  end function;

  -----------------------------------------------------------------------------
  -- Registers.
  -----------------------------------------------------------------------------
  signal arw     : gaw_t;                      -- a_r, windowed to GAW bits
  signal a1      : gaw_t := (others => '0');   -- a_r registered once
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
  -- n_k = cutv_k - r, the slot-k cut length: the one subtract the decision
  -- (the cut's segment length) and the slot-k cut head candidate share.
  type u7_3 is array (1 to 3) of unsigned(6 downto 0);
  signal ncut    : u7_3;

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
  signal X       : wen_arr(0 to 11);           -- extended window, av for next cycle
  signal npf     : unsigned(2 downto 0);       -- min(4, nvis)
  signal hcand   : hd_arr(0 to 9);             -- head per outcome
  signal bcand   : bnd_arr2;                   -- bundle per shift c'
  signal stall   : std_logic;

  -- Window counts per shift c' (from registers only).
  type u3_5 is array (0 to 4) of unsigned(2 downto 0);
  type u4_5 is array (0 to 4) of unsigned(3 downto 0);
  signal ldc     : u3_5;                       -- PF loads
  signal nvc     : u4_5;                       -- nv'
  signal nuc     : u4_5;                       -- nu'

begin

  -----------------------------------------------------------------------------
  -- Decision (combinational, against registered constants only).
  -----------------------------------------------------------------------------
  stall <= '1' when slfsr(6 downto 0) < STALL_THR else '0';

  -- Only the low GAW bits of the arrival counter are ever needed (every use is
  -- a difference against a pointer inside the credit window).
  arw <= a_r(GAW - 1 downto 0);

  ncut_g: for k in 1 to 3 generate
    ncut(k) <= resize(bnd(k).cutv, 7) - hd.r7;
  end generate;

  dec_p: process (E, nu, hd, bnd, ncut, bubble, de_credit_ok, stall) is
    variable r       : signed(7 downto 0);
    variable iss     : std_logic;
    variable dn, pl  : std_logic_vector(0 to 3);
    variable reach   : std_logic;
    variable fit     : std_logic;
    variable ok      : std_logic;
    variable n       : unsigned(5 downto 0);
    variable d       : unsigned(5 downto 0);
    variable c       : integer range 0 to 4;
    variable cutany  : std_logic;
    variable lst     : std_logic;
    variable cm      : element_stream;
    variable sl      : wslot_t;
  begin
    r := signed('0' & hd.r7);
    cm := ELEMENT_STREAM_INIT;
    dn := (others => '0');
    pl := (others => '0');
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
    sl.kind := kcode(E(0));
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

    -- Slots 1..3. Three compares of r against registered per-slot constants:
    -- r < cutv is both the budget bound and the copy hazard, r <= ende is
    -- both fit bounds, and r >= avb is the literal availability cut bound.
    for k in 1 to 3 loop
      reach := '0';
      if dn(k - 1) = '1' and E(k - 1).isE = '0' and nu > k and k < KMAX
         and r < signed(resize(bnd(k).cutv, 8)) then
        reach := '1';
      end if;
      fit := '0';
      ok  := '0';
      if E(k).isE = '1' then
        ok  := '1';
        fit := '1';
      else
        if r <= resize(bnd(k).ende, 8) then
          fit := '1';
        end if;
        if E(k).isL = '1' then
          if fit = '1' then
            ok := bnd(k).fav;
          elsif r >= resize(bnd(k).avb, 8) then
            ok := '1';
          end if;
          if bnd(k).nl >= LITP then
            ok := '0';
          end if;
        else
          ok := '1';
        end if;
      end if;
      pl(k) := reach and ok;
      dn(k) := reach and ok and fit;
      if pl(k) = '1' then
        sl := WSLOT_INIT;
        sl.val  := '1';
        sl.kind := kcode(E(k));
        sl.s    := unsigned(r(5 downto 0)) + bnd(k).p;
        sl.lptr := E(k).lptr;
        if E(k).isE = '1' then
          n := (others => '0');
          lst := '1';
        elsif fit = '1' then
          n := E(k).l7(5 downto 0);
        else
          n := ncut(k)(5 downto 0);
          cutany := '1';
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
          sl.q := E(k).u(15 downto 0);
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

    -- c (command record only; the next state uses the one-hots below).
    c := 0;
    for k in 0 to 3 loop
      if dn(k) = '1' then
        c := k + 1;
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
  -- shift are replicated 8x for the next-state fan-out). done is a prefix
  -- and plcd(k) implies done(k-1), so every bit is a small AND term:
  --   shift c (c = 1..3) = done(c-1) and not done(c), shift 4 = done(3);
  --   cut at slot k      = plcd(k) and not done(k)   (shift k);
  --   clean shift by k   = done(k-1) and not plcd(k).
  rep_g: for g in 0 to 7 generate
    oc_p: process (issue, done, plcd, nu) is
      variable oc : oc_t;
      variable sh : sh_t;
    begin
      oc := (others => '0');
      sh := (others => '0');
      if issue = '0' then
        if nu = 0 then
          oc(1) := '1';
        else
          oc(0) := '1';
        end if;
      end if;
      oc(2) := issue and not done(0);
      for k in 1 to 3 loop
        oc(2 + k) := issue and done(k - 1) and not plcd(k);
        oc(6 + k) := issue and plcd(k) and not done(k);
        sh(k)     := issue and done(k - 1) and not done(k);
      end loop;
      oc(6) := issue and done(3);
      sh(0) := not issue or not done(0);
      sh(4) := issue and done(3);
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

  -- Extended window X(j) = E(j) for j < nv, PF(j - nv) after; av for the
  -- NEXT cycle: av' = sat7(a1 - lptr) (= this cycle's avx for an entry already
  -- in the window), avx' = sat7(a_r - lptr).
  x_p: process (E, nv, pf, a1, arw) is
    variable w : wen_t;
  begin
    for j in 0 to 11 loop
      w := WEN_INIT;
      if j < 8 and j < to_integer(nv) then
        w := E(j);
        w.av  := E(j).avx;
        w.avx := satav(arw, E(j).lptr);
      else
        -- PF entry kk lands at position nv + kk, so position j takes pf(kk)
        -- exactly when nv = j - kk. Both bounds on kk are static per j:
        -- kk <= j (nv >= 0) and j - kk <= 8 (nv <= 8, the window depth), so
        -- positions 0..2 and 9..11 never build all four inputs of this mux
        -- (12 of the 48 PF inputs are unreachable).
        for kk in 0 to 3 loop
          if kk <= j and j - kk <= 8 and to_integer(nv) = j - kk then
            w := mkent(pf(kk), a1, arw);
          end if;
        end loop;
      end if;
      X(j) <= w;
    end loop;
  end process;

  -- Head candidates and per-shift bundles. Uses only E (registers) for the
  -- bundles and the shifted heads: a slot whose entry would come from PF is
  -- not usable next cycle (nu' = nv - c), so its values do not matter.
  cand_p: process (E, hd, bnd, ncut, bnext, a1) is
    variable y    : wen_arr(0 to 3);
    variable h    : hd_t;
    variable brw  : std_logic;
    variable rle  : unsigned(7 downto 0);
    variable av   : unsigned(6 downto 0);
    variable l8   : unsigned(7 downto 0);
    variable n7   : unsigned(6 downto 0);
  begin
    -- A head is only ever built from E(c) with c < nv (a head from PF is
    -- replaced by INIT the cycle after, as nu' = 0), so E(c).avx (a register)
    -- is its avn.

    -- 0 HOLD: same head; av0 for the next cycle = avn, avn refreshed.
    h := hd;
    h.avn := satav(a1, hd.lp0);
    h.cap := capf(E(0).isL, hd.rep0, hd.avn, hd.q7, bnext);
    hcand(0) <= h;

    -- 1 INIT: fresh head from E0.
    hcand(1) <= mkhead(E(0), E(0).avx, bnext);

    -- 2 slot 0 cut: n0 = cap. R0 - cap with the pipelined borrow, lp0 + cap,
    -- av0' = avn - cap, q0 := dq (registered), dq' = qdbl(dq, lim + cap).
    h := hd;
    brw := hd.rhnz and not hd.rl(7);
    rle := hd.rl;
    if brw = '1' then
      rle(7) := '1';
      h.rh   := hd.rh - 1;
      h.rhnz := b2sl(hd.rh /= 1);
    end if;
    h.rl := rle - resize(hd.cap, 8);
    if h.rhnz = '1' or h.rl(7) = '1' or h.rl(6 downto 0) = 127 then
      h.r7 := to_unsigned(127, 7);
    else
      h.r7 := h.rl(6 downto 0);
    end if;
    av := hd.avn - resize(hd.cap, 7);
    if E(0).isL = '1' then
      h.lp0 := hd.lp0 + resize(hd.cap, GAW);
      h.avn := satav(a1, hd.lp0 + resize(hd.cap, GAW));
    end if;
    if E(0).isC = '1' then
      h.q0   := hd.dq;
      h.q7   := sat(hd.dq, 7);
      h.rep0 := REPK and is_repq(hd.dq);
      l8     := resize(hd.lim, 8) + resize(hd.cap, 8);
      h.lim  := sat(l8, 7);
      h.dq   := qdbl(hd.dq, l8);
    end if;
    h.cap := capf(E(0).isL, h.rep0, av, h.q7, bnext);
    hcand(2) <= h;

    -- 3..6 shift by 1..4: fresh head from E(c).
    for c in 1 to 4 loop
      hcand(2 + c) <= mkhead(E(c), E(c).avx, bnext);
    end loop;

    -- 7..9 slot k cut: n_k = cutv_k - r, subtracted ONCE. This candidate is
    -- only ever selected when slot k was reached and cut, so r < cutv_k and
    -- n7 is the exact cut length; every next-state value below is one of
    -- "x -/+ n_k", so the one subtract replaces the four separate
    -- "- cutv_k + r" pairs this loop used to build (two carry chains each).
    for k in 1 to 3 loop
      h  := mkhead(E(k), E(k).avx, bnext);
      n7 := ncut(k);
      -- R0 = len_k - n_k = len_k(6:0) [+ 128, borrowing from rh] - n_k.
      rle := '0' & E(k).lo;
      if E(k).rhnz = '1' then
        rle(7) := '1';
        h.rh   := E(k).u - 1;
        h.rhnz := E(k).rh2;
      end if;
      h.rl := rle - resize(n7, 8);
      if h.rhnz = '1' or h.rl(7) = '1' or h.rl(6 downto 0) = 127 then
        h.r7 := to_unsigned(127, 7);
      else
        h.r7 := h.rl(6 downto 0);
      end if;
      -- lp0 = lptr_k + n_k, one GAW-bit add.
      h.lp0 := E(k).lptr + resize(n7, GAW);
      -- av0' = av_k - n_k, avn' = (av_k with the next A_r) - n_k (7 b).
      av    := E(k).av - n7;
      h.avn := E(k).avx - n7;
      -- Copy: produced = n_k, lim = n_k + off_k, dq = qdbl(off_k, lim).
      l8    := resize(n7, 8) + resize(E(k).o7, 8);
      h.lim := sat(l8, 7);
      h.dq  := qdbl(E(k).u(15 downto 0), l8);
      h.cap := capf(E(k).isL, h.rep0, av, h.q7, bnext);
      hcand(6 + k) <= h;
    end loop;

    -- Bundles for every shift c' (slots 1..3 of the shifted window).
    for c in 0 to 4 loop
      for i in 0 to 3 loop
        y(i) := E(c + i);
      end loop;
      bcand(c) <= mkbnd(y, bnext);
    end loop;
  end process;

  -- Window counts for every shift c' (registers only): rem = nv - c',
  -- ld = min(npf, 8 - rem), nv' = rem + ld, nu' = rem.
  cnt_c: process (nv, npf) is
    variable rm : unsigned(3 downto 0);
    variable fr : unsigned(3 downto 0);
  begin
    for c in 0 to 4 loop
      rm := nv - to_unsigned(c, 4);
      fr := to_unsigned(8, 4) - rm;
      if resize(npf, 4) < fr then
        ldc(c) <= npf;
        nvc(c) <= rm + resize(npf, 4);
      else
        ldc(c) <= fr(2 downto 0);
        nvc(c) <= to_unsigned(8, 4);
      end if;
      nuc(c) <= rm;
    end loop;
  end process;

  -----------------------------------------------------------------------------
  -- Select and register.
  -----------------------------------------------------------------------------
  -- Late counts for the ELQ: AND-OR selects by the replicated shift one-hot.
  cnt_p: process (sh_rep, ldc) is
    variable ra, pa : unsigned(2 downto 0);
  begin
    ra := (others => '0');
    pa := (others => '0');
    for c in 0 to 4 loop
      if sh_rep(7)(c) = '1' then
        ra := ra or to_unsigned(c, 3);
        pa := pa or ldc(c);
      end if;
    end loop;
    rp_adv <= ra;
    pf_adv <= pa;
  end process;

  reg_p: process (clk) is
    variable nvn, nun : unsigned(3 downto 0);
  begin
    if rising_edge(clk) then
      a1 <= arw;

      -- Window: E(i)' = X(i + c), late 5:1 by the replicated shift.
      for i in 0 to 7 loop
        for s in 0 to 4 loop
          if sh_rep(i)(s) = '1' then
            E(i) <= X(i + s);
          end if;
        end loop;
      end loop;

      nvn := (others => '0');
      nun := (others => '0');
      for s in 0 to 4 loop
        if sh_rep(6)(s) = '1' then
          nvn := nvn or nvc(s);
          nun := nun or nuc(s);
        end if;
      end loop;
      nv <= nvn;
      nu <= nun;

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
        a1     <= (others => '0');
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
    variable nsh, noc : natural;
  begin
    if rising_edge(clk) and reset = '0' then
      assert nu <= nv and nv <= 8 report "writer: window count out of range" severity failure;
      if cmd_n.valid = '1' then
        assert cmd_n.d_total <= bb report "writer: command over budget" severity failure;
      end if;
      nsh := 0;
      noc := 0;
      for i in 0 to 4 loop
        if sh_rep(0)(i) = '1' then nsh := nsh + 1; end if;
      end loop;
      for i in 0 to 9 loop
        if oc_rep(0)(i) = '1' then noc := noc + 1; end if;
      end loop;
      assert nsh = 1 and noc = 1
        report "writer: shift / outcome select is not one-hot" severity failure;
      -- R0 split: the borrow invariant (rh /= 0 => rl >= 96 after a cut) and
      -- rhnz => rh /= 0. The converse no longer holds: a copy head's rh is the
      -- element payload's offset half (see mkent) and is never read, because
      -- every read of rh is guarded by rhnz.
      assert hd.rhnz = '0' or hd.rh /= 0
        report "writer: rhnz does not match rh" severity failure;
    end if;
  end process;

  -- Probe (PROBE true): per-cycle issue / stop-reason counters, rewritten to
  -- probe_wr.txt in the simulation's working directory at every EOC command
  -- and every 16384 cycles. Stop reason = first slot k that is not done.
  probe_g: if PROBE generate
    probe_p: process (clk) is
      use std.textio.all;
      type cnt_arr is array (natural range <>) of natural;
      variable cyc, iss, nu0, decr, bub, litw, byt : natural := 0;
      variable seg : cnt_arr(0 to 4) := (others => 0);
      -- 0 cut0, 1 eoc, 2 starved, 3 kmax, 4 budget, 5 hazard, 6 litavail,
      -- 7 litp, 8 cut, 9 all4
      variable stp : cnt_arr(0 to 9) := (others => 0);
      constant SN : string := "cut0    eoc     starved kmax    budget  hazard  litavl  litp    cut     all4    ";
      variable r  : signed(7 downto 0);
      variable k, why, ns : natural;
      file f      : text;
      variable l  : line;
    begin
      if rising_edge(clk) and reset = '0' then
        cyc := cyc + 1;
        r := signed('0' & hd.r7);
        if issue = '1' then
          iss := iss + 1;
          byt := byt + to_integer(cmd_n.d_total);
          ns := 0;
          for j in 0 to 3 loop
            if done(j) = '1' then ns := j + 1; end if;
          end loop;
          seg(ns) := seg(ns) + 1;
          if done(0) = '0' then
            why := 0;
          elsif ns = 4 then
            why := 9;
          else
            k := ns;
            if E(k - 1).isE = '1' then why := 1;
            elsif nu <= k then why := 2;
            elsif k >= KMAX then why := 3;
            -- budget and hazard are one bound now, both counted as "budget".
            elsif not (r < signed(resize(bnd(k).cutv, 8))) then why := 4;
            elsif plcd(k) = '1' then why := 8;
            elsif E(k).isL = '1' and bnd(k).nl >= LITP then why := 7;
            elsif E(k).isL = '1' then why := 6;
            else why := 5;
            end if;
          end if;
          stp(why) := stp(why) + 1;
        elsif nu = 0 then
          nu0 := nu0 + 1;
        elsif de_credit_ok = '0' then
          decr := decr + 1;
        elsif bubble = '1' or stall = '1' then
          bub := bub + 1;
        else
          litw := litw + 1;
        end if;
        if cyc mod 16384 = 0 or (issue = '1' and cmd_n.last = '1') then
          file_open(f, "probe_wr.txt", write_mode);
          write(l, string'("cycles ") & integer'image(cyc) & " issue " & integer'image(iss)
                & " bytes " & integer'image(byt)); writeline(f, l);
          write(l, string'("noissue nu0 ") & integer'image(nu0) & " decredit " & integer'image(decr)
                & " bubble " & integer'image(bub) & " litwait " & integer'image(litw)); writeline(f, l);
          write(l, string'("segs"));
          for j in 0 to 4 loop write(l, " " & integer'image(seg(j))); end loop;
          writeline(f, l);
          for j in 0 to 9 loop
            write(l, string'("stop ") & SN(8 * j + 1 to 8 * j + 8) & integer'image(stp(j)));
            writeline(f, l);
          end loop;
          file_close(f);
        end if;
      end if;
    end process;
  end generate;
  -- pragma translate_on

end behavior;
