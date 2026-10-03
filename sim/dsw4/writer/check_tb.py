"""check_tb.py -- check a tb_writer mode B/C log (SPEC 12 B2b).

Log format (written by tb_writer.vhd, one line per decision cycle, i.e. a cycle
with de_credit_ok = 1 that is not the bubble after a last command):
    DRAW <wsl dir> <nel>
    <t> <vis> <a_r hex> 0                       no command
    <t> <vis> <a_r hex> 1 last rep c cut d lw [val kind port_b n s q lptr] x4
    END <cycles>
vis = elements with index < vis were usable by the writer in that cycle.

replay (mode B): gold.Writer (the B0 golden model, same knobs) decides every
    decision cycle with the logged vis; every command must equal the RTL's
    field by field, a stall must be a stall, and the draw must end together.
invariants (modes B and C): the command stream rebuilds the element stream
    exactly (slot 0 continues the head at the right offset and lptr, later
    slots start fresh elements, only the last slot may be partial), literal
    bytes were available (lptr + n <= a_r), copy hazards (slot 0: n <= q
    unless REP, q = off * 2^j <= produced + off; slot k: n <= off - s),
    budget (32, or the TEST_CUT LFSR budget), KMAX, LITP, ports, s offsets,
    d_total, c, cut, last/EOC, rep, lw.
"""
import os
import sys

SPEC = r'C:/Users/Vatsal/diag-i35/diag/work-spec'
if SPEC not in sys.path:
    sys.path.insert(0, SPEC)
import gold  # noqa: E402

KNOBS = {
    'base': {}, 'stall': {},
    'slots1': {'KMAX': 1}, 'slots2': {'KMAX': 2}, 'slots3': {'KMAX': 3},
    'cut': {'CUT': 1}, 'norep': {'REP': 0}, 'litp1': {'LITP': 1},
}
LIT, CPY, EOC = 0, 1, 3


def win_path(p):
    if p.startswith('/mnt/'):
        return p[5].upper() + ':' + p[6:]
    return p


def read_elements(d):
    els = []
    with open(os.path.join(win_path(d), 'elements.txt')) as f:
        for ln in f:
            if not ln.strip() or ln.startswith('#'):
                continue
            k, ln_, off, lptr, pos, hdr, ch = ln.split()
            els.append(gold.El(kind=int(k, 16), pos=int(pos, 16), hdr=int(hdr, 16), len=int(ln_, 16),
                               off=int(off, 16), lptr=int(lptr, 16), chunk=int(ch, 16)))
    return els


def parse_cmd(f):
    """fields after the issued flag -> dict."""
    v = [int(x, 16) for x in f]
    cmd = dict(last=v[0], rep=v[1], c=v[2], cut=v[3], d=v[4], lw=v[5], slots=[])
    for k in range(4):
        val, kind, pb, n, s, q, lptr = v[6 + 7 * k: 13 + 7 * k]
        cmd['slots'].append(dict(val=val, kind=kind, pb=pb, n=n, s=s, q=q, lptr=lptr))
    return cmd


class Inv(object):
    """Invariant checker for one draw."""

    def __init__(self, els, knobs, errs, name):
        self.els, self.k, self.errs, self.name = els, knobs, errs, name
        self.i = 0          # head element
        self.taken = 0      # bytes of the head already placed
        self.lfsr = 0xACE1
        self.n = 0

    def err(self, msg):
        if len(self.errs) < 200:
            self.errs.append('%s cmd %d: %s' % (self.name, self.n, msg))

    def cmd(self, c, a_r):
        kn = self.k
        B = gold.lfsr_budget(self.lfsr) if kn['CUT'] else 32
        used = [s for s in c['slots'] if s['val']]
        if [s['val'] for s in c['slots']] != [1] * len(used) + [0] * (4 - len(used)):
            self.err('slot val not a prefix')
        if not 1 <= len(used) <= kn['KMAX']:
            self.err('%d slots, KMAX %d' % (len(used), kn['KMAX']))
        for s in c['slots'][len(used):]:
            if any(s[f] for f in ('kind', 'pb', 'n', 's', 'q', 'lptr')):
                self.err('unused slot not zero')
        d = 0
        nl = 0
        done = 0
        last = 0
        cut = 0
        i, taken = self.i, self.taken
        for k, s in enumerate(used):
            if i >= len(self.els):
                self.err('slot %d beyond the last element' % k)
                return
            E = self.els[i]
            if k > 0 and taken != 0:
                self.err('slot %d does not start a fresh element' % k)
            if s['kind'] != E.kind:
                self.err('slot %d kind %d, element %d kind %d' % (k, s['kind'], i, E.kind))
                return
            if s['s'] != d:
                self.err('slot %d s %d, expected %d' % (k, s['s'], d))
            n = s['n']
            if E.kind == EOC:
                if n != 0 or s['lptr'] != E.lptr or s['q'] or s['pb']:
                    self.err('EOC slot fields')
                last = 1
                done += 1
                i += 1
                taken = 0
                if k != len(used) - 1:
                    self.err('EOC is not the last slot')
                break
            rem = E.len - taken
            if not 1 <= n <= rem:
                self.err('slot %d n %d, remainder %d' % (k, n, rem))
            if E.kind == LIT:
                if s['lptr'] != (E.lptr + taken) % gold.GMOD:
                    self.err('slot %d LIT lptr %08X expected %08X' % (k, s['lptr'], E.lptr + taken))
                if s['pb'] != nl:
                    self.err('slot %d port_b %d, literal number %d' % (k, s['pb'], nl))
                nl += 1
                if nl > kn['LITP']:
                    self.err('more than LITP literals')
                if ((a_r - (s['lptr'] + n)) % gold.GMOD) >= (1 << 31):
                    self.err('literal bytes up to %08X not available (a_r %08X)'
                             % (s['lptr'] + n, a_r))
                if s['q']:
                    self.err('LIT q not 0')
            else:
                if s['lptr'] != E.lptr:
                    self.err('slot %d CPY lptr' % k)
                q = s['q']
                if k == 0:
                    j = 0
                    while E.off << j < q:
                        j += 1
                    if E.off << j != q or q > taken + E.off:
                        self.err('slot 0 q %d invalid (off %d, produced %d)' % (q, E.off, taken))
                    rep = 1 if (kn['REP'] and q in gold.REPQ) else 0
                    if c['rep'] != rep:
                        self.err('rep %d expected %d' % (c['rep'], rep))
                    if not rep and n > q:
                        self.err('slot 0 n %d > q %d' % (n, q))
                else:
                    if q != E.off:
                        self.err('slot %d q %d off %d' % (k, q, E.off))
                    if n > E.off - d:
                        self.err('slot %d hazard: n %d, off %d, d %d' % (k, n, E.off, d))
            d += n
            if n == rem:
                done += 1
                i += 1
                taken = 0
            else:
                cut = 1
                taken += n
                if k != len(used) - 1:
                    self.err('partial slot %d is not the last' % k)
        if used and used[0]['kind'] != CPY and c['rep']:
            self.err('rep on a non-copy head')
        if d > B:
            self.err('d_total %d > budget %d' % (d, B))
        if c['d'] != d:
            self.err('d_total %d, slots sum %d' % (c['d'], d))
        if c['c'] != done or c['cut'] != cut or c['last'] != last:
            self.err('c/cut/last %d/%d/%d expected %d/%d/%d'
                     % (c['c'], c['cut'], c['last'], done, cut, last))
        if used and c['lw'] != used[0]['lptr']:
            self.err('lw %08X, slot 0 lptr %08X' % (c['lw'], used[0]['lptr']))
        self.i, self.taken = i, taken
        if kn['CUT']:
            self.lfsr = gold.lfsr_step(self.lfsr)
        self.n += 1


def check_log(path, variant, replay=True):
    knobs = dict(gold.BASE_KNOBS)
    knobs.update(KNOBS[variant])
    errs = []
    ndraw = ncmd = ndec = 0
    w = inv = None
    name = None
    els = None

    def finish():
        if w is not None and replay and not w.done():
            errs.append('%s: gold has elements left at rp %d of %d' % (name, w.rp, len(els)))
        if inv is not None and inv.i != len(els):
            errs.append('%s: commands covered %d of %d elements' % (name, inv.i, len(els)))

    with open(path) as f:
        for ln in f:
            p = ln.split()
            if not p:
                continue
            if p[0] == 'DRAW':
                finish()
                name = p[1].split('/gold/')[-1]
                els = read_elements(p[1])
                if int(p[2]) != len(els):
                    errs.append('%s: element count' % name)
                w = gold.Writer(els, knobs) if replay else None
                inv = Inv(els, knobs, errs, name)
                ndraw += 1
                continue
            if p[0] == 'END':
                continue
            t, vis, a_r, iss = int(p[0]), int(p[1]), int(p[2], 16), int(p[3])
            ndec += 1
            rc = parse_cmd(p[4:]) if iss else None
            if replay:
                g = w.decide(vis=vis)
                if (g is None) != (rc is None):
                    if len(errs) < 200:
                        errs.append('%s t=%d vis=%d: gold %s, RTL %s' % (
                            name, t, vis, 'stall' if g is None else 'issues', 'stall' if rc is None else 'issues'))
                elif g is not None:
                    exp = gold.fmt_cmd(0, g).split()[1:-1]
                    got = p[4:]
                    if exp != got and len(errs) < 200:
                        errs.append('%s t=%d vis=%d cmd %d:\n   gold %s\n   rtl  %s' % (
                            name, t, vis, ncmd, ' '.join(exp), ' '.join(got)))
            if rc is not None:
                inv.cmd(rc, a_r)
                ncmd += 1
        finish()
    summary = 'draws=%d commands=%d decision_cycles=%d %s errors=%d' % (
        ndraw, ncmd, ndec, 'replay+inv' if replay else 'inv', len(errs))
    return dict(ok=not errs and ndraw > 0, summary=summary, errors=errs)


if __name__ == '__main__':
    r = check_log(sys.argv[1], sys.argv[2], replay=(len(sys.argv) < 4 or sys.argv[3] != 'inv'))
    print(r['summary'])
    print('\n'.join(r['errors'][:30]))
    sys.exit(0 if r['ok'] else 1)
