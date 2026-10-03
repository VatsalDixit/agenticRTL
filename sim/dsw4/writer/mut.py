"""mut.py -- mutation test of tb_writer: each mutant of the writer RTL must fail.

Builds each mutant in its own work dir (mutants/<name>/) and runs modes A, B8
and C25 over the shapes plus 20 fuzz streams. Prints KILLED / SURVIVED.
"""
import concurrent.futures as cf
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
sys.path.insert(0, HERE)
import tools  # noqa: E402
import check_tb  # noqa: E402
import run_tb  # noqa: E402

RTL = r'C:/Users/Vatsal/dsw4/rtl'

MUTANTS = {
    'identity':    ("      bubble <= issue and cmd_n.last;", "      bubble <= issue and cmd_n.last;"),
    'haz_le':      ("        if r < bnd(k).haz then", "        if r <= bnd(k).haz then"),
    'endh_lt':     ("        if r <= bnd(k).endh then", "        if r < bnd(k).endh then"),
    'qdbl_64':     ("      if q < 32 and (q(15 downto 0) & '0') <= lim then",
                    "      if q < 64 and (q(15 downto 0) & '0') <= lim then"),
    'qdbl_strict': ("      if q < 32 and (q(15 downto 0) & '0') <= lim then",
                    "      if q < 32 and (q(15 downto 0) & '0') < lim then"),
    'nu_eq_nv':    ("      nu <= to_unsigned(rem0, 4);", "      nu <= to_unsigned(nvn, 4);"),
    'no_fav':      ("          ok  := bnd(k).fav;", "          ok  := '1';"),
    'port_b':      ("          sl.port_b := bnd(k).nl(0);", "          sl.port_b := '0';"),
    'no_bubble':   ("      bubble <= issue and cmd_n.last;", "      bubble <= '0';"),
    'hold_no_av':  ("    h.av0 := satav(a_r, hd.lp0);\n    h.cap := capf(E(0).isL, hd.rep0, h.av0, hd.q7, bnext);\n    hcand(0) <= h;",
                    "    h.cap := capf(E(0).isL, hd.rep0, h.av0, hd.q7, bnext);\n    hcand(0) <= h;"),
    'cutk_lp':     ("      lp := E(k).lptr + resize(bnd(k).cutv, 32) - resize(r, 32);",
                    "      lp := E(k).lptr + resize(bnd(k).cutv, 32) - resize(r, 32) + 1;"),
    'litp':        ("        if bnd(k).nl >= LITP then", "        if bnd(k).nl > LITP then"),
    'repq_32':     ("    if x = 1 or x = 2 or x = 4 or x = 8 or x = 16 then",
                    "    if x = 1 or x = 2 or x = 4 or x = 8 or x = 16 or x = 32 then"),
    'bud_le':      ("         and r < bnd(k).bud then", "         and r <= bnd(k).bud then"),
}

DRAWS = run_tb.SHAPES + ['fuzz/f%03d' % i for i in range(0, 200, 10)]


def one(name):
    o, n = MUTANTS[name]
    d = os.path.join(HERE, 'mutants', name)
    os.makedirs(d, exist_ok=True)
    src = open(os.path.join(RTL, 'vhsnunzip_writer.vhd')).read()
    assert src.count(o) == 1, name
    open(os.path.join(d, 'vhsnunzip_writer.vhd'), 'w', newline='\n').write(src.replace(o, n))
    files = [os.path.join(RTL, f) for f in ('vhsnunzip_utils_pkg.vhd', 'vhsnunzip_pkg.vhd',
                                            'vhsnunzip_int_pkg.vhd', 'vhsnunzip_dsw4_pkg.vhd',
                                            'vhsnunzip_elq.vhd')]
    files += [os.path.join(d, 'vhsnunzip_writer.vhd'), os.path.join(HERE, 'tb_writer.vhd')]
    dl = os.path.join(d, 'dirs')
    with open(dl, 'w', newline='\n') as f:
        for x in DRAWS:
            f.write(run_tb.wsl(os.path.join(run_tb.GOLD, x)) + '\n')
    w = run_tb.wsl(d)
    out = {}
    script = 'cd %s && rm -f *.o *.cf && ghdl -a --std=08 %s && ghdl -e --std=08 tb_writer' % (
        w, ' '.join(run_tb.wsl(f) for f in files))
    for tag, gen in (('A', '-gMODE=0 -gCRED_PCT=20'), ('B', '-gMODE=1 -gCRED_PCT=15 -gFEED_PCT=8'),
                     ('C', '-gMODE=2 -gCRED_PCT=15 -gFEED_PCT=25 -gAR_STEP=12')):
        script += (' ; ./tb_writer --ieee-asserts=disable-at-0 -gDIRLIST=%s/dirs -gLOGF=%s/%s.log %s '
                   '> %s/%s.out 2>&1; grep -E "TB_PASS|TB_FAIL" %s/%s.out | tail -1 | sed "s/^/%s: /"; '
                   'grep -E "MISMATCH|ERROR|failure" %s/%s.out | head -2 | sed "s/^/%s: /"') % (
                       w, w, tag, gen, w, tag, w, tag, tag, w, tag, tag)
    r = tools.eda_shell(script, timeout=7200)
    txt = r.text
    for tag in 'ABC':
        lines = [l for l in txt.splitlines() if l.startswith(tag + ':')]
        passed = any('TB_PASS' in l for l in lines)
        if passed and tag in 'BC':
            c = check_tb.check_log(os.path.join(d, tag + '.log'), 'base', replay=(tag == 'B'))
            passed = c['ok']
            lines.append(tag + ': check ' + c['summary'])
        out[tag] = (passed, lines[:2])
    for tag in 'BC':
        try:
            os.remove(os.path.join(d, tag + '.log'))
        except OSError:
            pass
    return name, out


def main():
    names = sys.argv[1].split(',') if len(sys.argv) > 1 else list(MUTANTS)
    survived = []
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for name, out in ex.map(one, names):
            killed = [t for t in 'ABC' if not out[t][0]]
            print('%-12s %s by %s' % (name, 'KILLED' if killed else 'SURVIVED', ','.join(killed) or '-'),
                  flush=True)
            for t in 'ABC':
                for l in out[t][1]:
                    print('      ' + l[:220])
            if not killed:
                survived.append(name)
    print('SURVIVED: %s' % (survived or 'none'))


if __name__ == '__main__':
    main()
