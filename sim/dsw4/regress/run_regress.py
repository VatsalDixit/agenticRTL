"""Round-0 review regression set (DSW-4 B2), integrated design.

    python gen_regress.py                  (once: writes streams/)
    python run_regress.py [--rtl DIR] [--jobs 3] [--only CASE,...]
    python timing_l1.py                    (findings r0-2/3/4, yosys depth)

Each case = (name, TEST_* generics, streams, expectation), run through the
kit perf testbench with the TEST_* generics forwarded (sim/dsw4/core
run_knobs.make_tb) and agentic/syn/sim_draws.sh.

  expect 'pass'     rc 0, no deadlock, sim.log EMPTY (every SPEC 7 assert
                    silent), chunk count and per-chunk bytes == out.hex.
  expect 'assert'   the given assert text appears in sim.log (negative test:
                    TEST_ST_LINES below the SPEC range 12..32 makes LT reads
                    come too soon after the history write; the LT
                    write-visibility assert of vhsnunzip_core must catch it
                    directly, finding r0-0 / r0-5).

Findings covered: r0-0 / r0-5 (LT source line visible >= 2 cycles before
the read: st8 / st9x / st7 cases; copy source < W unless REP: checked on
every copy of every pass case, and the 'mutant' case shows it catches an
over-sized overlapping copy that D >= 0 misses). r0-1..r0-3: timing_l1.py.
"""
import argparse, os, re, shutil, sys, time
sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
sys.path.insert(0, r'C:/Users/Vatsal/dsw4/sim/dsw4/core')
import tools  # noqa: E402
import run_knobs as rk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STREAMS = os.path.join(HERE, 'streams')
LT_ASSERT = 'core: LT read of history line'
CS_ASSERT = 'reads a byte of its own command'
ALLS = ['ltmargin', 'ltmph2', 'ltm678', 'ltstale', 'ovl']

CASES = [
    # name      generics                      streams                    expect
    ('base',  '',                           ALLS,                        ('pass', None)),
    ('st12',  '-gTEST_ST_LINES=12',         ALLS,                        ('pass', None)),
    ('st9',   '-gTEST_ST_LINES=9',          ['ltmargin'],                ('pass', None)),
    # ltmph2 at 9 lines reads a line 1 cycle after its write is presented:
    # the sim RAM model returns the new data (bytes correct), but SPEC 1 only
    # promises it from 2 cycles, and the assert follows the SPEC.
    ('st9x',  '-gTEST_ST_LINES=9',          ['ltmph2'],                  ('assert', LT_ASSERT)),
    ('st8',   '-gTEST_ST_LINES=8',          ['ltmph2', 'ltstale'],       ('assert', LT_ASSERT)),
    ('st7',   '-gTEST_ST_LINES=7',          ['ltm678'],                  ('assert', LT_ASSERT)),
    # Writer mutant (built from --rtl into work/rtl_mut): a slot-k copy is
    # declared complete past its hazard limit (endh uses B, not min(B, off)),
    # so n_k > q_k - S_k while D = q_k - S_k - 1 is still >= 0. Only the
    # copy-source assert of vhsnunzip_agen catches it.
    ('mutant', '',                          ['ovl'],                     ('assert', CS_ASSERT)),
]
MUTANT = ('      if o7 < bi then mo := o7; else mo := bi; end if;',
          '      mo := bi;  -- MUTANT: copy completes past its hazard limit')


def make_mutant(rtl):
    dst = os.path.join(HERE, 'work', 'rtl_mut')
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    for f in os.listdir(rtl):
        if f.endswith('.vhd'):
            shutil.copyfile(os.path.join(rtl, f), os.path.join(dst, f))
    p = os.path.join(dst, 'vhsnunzip_writer.vhd')
    s = open(p, newline='').read()
    assert s.count(MUTANT[0]) == 1, 'mutation site not found'
    open(p, 'w', newline='').write(s.replace(MUTANT[0], MUTANT[1]))
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rtl', default=rk.RTL)
    ap.add_argument('--jobs', type=int, default=3)
    ap.add_argument('--only', default='')
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    man = {}
    for ln in open(os.path.join(STREAMS, 'manifest.txt')):
        p = ln.split()
        if p:
            man[p[0]] = int(p[1])
    tb = os.path.join(HERE, 'work', 'tb_perf_knobs.vhd')
    os.makedirs(os.path.dirname(tb), exist_ok=True)
    rk.make_tb(tb)
    bad = 0
    for name, gen, names, (exp, txt) in CASES:
        if a.only and name not in a.only.split(','):
            continue
        vdir = os.path.join(HERE, 'work', name + a.tag)
        build = os.path.join(vdir, 'build')
        os.makedirs(build, exist_ok=True)
        specs = []
        for n in names:
            d = os.path.join(vdir, n)
            if os.path.exists(d):
                shutil.rmtree(d)
            os.makedirs(d)
            shutil.copyfile(os.path.join(STREAMS, n, 'cs.tv'), os.path.join(d, 'cs.tv'))
            specs.append('"%s:%d"' % (tools.shell_path(d), man[n]))
        g = rk.WIDTHS + (' ' + gen if gen else '')
        rtl = make_mutant(a.rtl) if name == 'mutant' else a.rtl
        cmd = 'SIM_JOBS=%d bash "%s" "%s" "%s" "%s" "%s" 100ms %s' % (
            a.jobs, tools.shell_path(rk.SIM_SH), tools.shell_path(rtl), tools.shell_path(tb),
            tools.shell_path(build), g, ' '.join(specs))
        t = time.time()
        r = tools.eda_shell(cmd, timeout=7000)
        if 'ELAB_OK' not in r.text:
            print(r.text[-3000:]); sys.exit(2)
        st = {}
        for ln in r.text.splitlines():
            m = re.match(r'DRAW (\S+) rc=(-?\d+) deadlock=(\d)', ln)
            if m:
                st[m.group(1)] = (int(m.group(2)), m.group(3) == '1')
        for n in names:
            d = os.path.join(vdir, n)
            rc, dl = st.get(tools.shell_path(d), (99, False))
            lp = os.path.join(d, 'sim.log')
            log = open(lp, errors='replace').read() if os.path.exists(lp) else ''
            msg = []
            bpc = '-'
            if exp == 'pass':
                if rc or dl:
                    msg.append('rc=%d deadlock=%d %s' % (rc, dl, log[-600:].replace('\n', ' | ')))
                elif log.strip():
                    msg.append('sim.log: ' + log[:600].replace('\n', ' | '))
                out = os.path.join(d, 'out.hex')
                if os.path.exists(out):
                    got = rk.chunks_of(out)[0]
                    want = rk.chunks_of(os.path.join(STREAMS, n, 'out.hex'))[0]
                    if len(got) != len(want):
                        msg.append('chunks %d != %d' % (len(got), len(want)))
                    for i, (x, y) in enumerate(zip(got, want)):
                        if x != y:
                            msg.append('chunk %d differs' % i)
                            break
                    pf = os.path.join(d, 'perf.txt')
                    if os.path.exists(pf):
                        perf = dict(l.strip().split('=') for l in open(pf) if '=' in l)
                        bpc = '%.4f' % (int(perf['bytes_out']) / max(1, int(perf['cycles'])))
                else:
                    msg.append('no out.hex')
                note = ''
            else:
                if txt not in log:
                    msg.append('expected assert "%s" not raised (rc=%d, sim.log: %s)'
                               % (txt, rc, log[:300].replace('\n', ' | ')))
                first = next((l for l in log.splitlines() if 'assert' in l), '')
                note = 'first: ' + first[first.find('):') + 2:][:150] if first else ''
            bad += bool(msg)
            print('%-6s %-9s %-7s %s %s %s' % (name, n, exp, 'FAIL' if msg else 'ok  ', bpc,
                                              '; '.join(msg) or note), flush=True)
        print('case %s done in %.0f s' % (name, time.time() - t), flush=True)
    print('REGRESS_PASS' if not bad else 'REGRESS_FAIL %d' % bad)
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
