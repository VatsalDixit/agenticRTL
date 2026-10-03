"""run.py -- build and run tb_dpath in WSL GHDL.

    python run.py build
    python run.py run DRAW [--mode 0|1] [--ag agcmd.txt] [--cmds cmds.txt]
                      [--st 32] [--issue 100] [--de 100] [--seed 1]
    python run.py suite [--jobs 3] [--quick]    the B2a gate (see SUITE)

Sources are analysed one by one from C:/Users/Vatsal/dsw4/rtl (only the files
this unit needs) into work/build; the testbench is tb_dpath.vhd here.
"""
import argparse
import concurrent.futures
import os
import sys
import time

sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prep import gold_dir  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RTL = r'C:/Users/Vatsal/dsw4/rtl'
GOLD = r'C:/Users/Vatsal/dsw4/sim/dsw4/gold'
BUILD = os.path.join(HERE, 'work', 'build')
SRCS = ['vhsnunzip_utils_pkg.vhd', 'vhsnunzip_int_pkg.vhd', 'vhsnunzip_dsw4_pkg.vhd',
        'vhsnunzip_srl.vhd', 'vhsnunzip_fifo.vhd', 'vhsnunzip_ram.sim.vhd',
        'vhsnunzip_agen.vhd', 'vhsnunzip_dpath.vhd', 'vhsnunzip_defifo.vhd']
SHAPES = ['tiny-chunks', 'line-straddle-17', 'line-straddle-33', 'line-straddle-65',
          'sub-line', 'medium', 'single-chunk', 'long-chunk']
SP = tools.shell_path


def build(rtl=RTL, bdir=BUILD):
    os.makedirs(bdir, exist_ok=True)
    files = [SP(os.path.join(rtl, f)) for f in SRCS] + [SP(os.path.join(HERE, 'tb_dpath.vhd'))]
    script = 'rm -f *.o *.cf tb_dpath; ' + ' && '.join(
        'ghdl -a --std=08 -frelaxed %s' % f for f in files) + \
        ' && ghdl -e --std=08 -frelaxed tb_dpath && echo BUILD_OK'
    r = tools.eda_shell(script, timeout=1800, cwd=bdir)
    return r.text


def run(draw, mode=0, ag='agcmd.txt', cmds='cmds.txt', st=32, issue=100, de=100, seed=1,
        maxcyc=20000000, bdir=BUILD, strict=None):
    gdir = SP(gold_dir(draw)) + '/'
    mem = SP(os.path.join(HERE, 'work', draw, 'mem.hex'))
    gens = ['-gDIR=%s' % gdir, '-gMEMF=%s' % mem, '-gAGF=%s' % ag, '-gCMDF=%s' % cmds,
            '-gMODE=%d' % mode, '-gST_LINES_G=%d' % st, '-gISSUE_PCT=%d' % issue,
            '-gDE_PCT=%d' % de, '-gSEED=%d' % seed, '-gMAXCYC=%d' % maxcyc]
    if strict is None:
        # out.hex is gold's push framing of the base command stream
        strict = ag in ('agcmd.txt', 'agcmd_st12.txt')
    gens.append('-gSTRICT=%s' % ('true' if strict else 'false'))
    script = './tb_dpath %s --ieee-asserts=disable-at-0 2>&1 | grep -v "^$" | tail -40' % ' '.join(gens)
    t0 = time.time()
    r = tools.eda_shell(script, timeout=3600, cwd=bdir)
    txt = r.text
    ok = 'TB_PASS' in txt and 'TB_FAIL' not in txt
    return ok, txt, time.time() - t0


# (draw, mode, agcmd file, cmds file, TEST_ST_LINES, issue %, de %, seed)
EXTRA = ['train-taxi', 'fuzz/f095', 'fuzz/f129', 'fuzz/f011', 'line-straddle-33']


def suite_cases(quick=False, big=False):
    cases = []
    for d in SHAPES:
        cases.append((d, 0, 'agcmd.txt', 'cmds.txt', 32, 100, 100, 1))
        cases.append((d, 0, 'agcmd_st12.txt', 'cmds.txt', 32, 100, 100, 1))
        cases.append((d, 1, 'agcmd.txt', 'cmds.txt', 32, 100, 100, 1))
        cases.append((d, 1, 'agcmd_st12.txt', 'cmds.txt', 12, 100, 100, 1))
    if not quick:
        for d in SHAPES:
            # backpressure: random issue gaps and de_ready
            cases.append((d, 1, 'agcmd.txt', 'cmds.txt', 32, 70, 40, 3))
            # writer knob variants through agen
            for v in ('cut', 'norep', 'slots1', 'litp1'):
                cases.append((d, 1, 'agcmd_%s.txt' % v, 'cmds_%s.txt' % v, 32, 100, 100, 1))
        # LT-heavy real data / fuzz (prep.py these first; train-taxi from work/goldx)
        for d in EXTRA:
            if d != 'line-straddle-33':
                cases.append((d, 0, 'agcmd.txt', 'cmds.txt', 32, 100, 100, 1))
                cases.append((d, 1, 'agcmd_st12.txt', 'cmds.txt', 12, 100, 100, 1))
            cases.append((d, 1, 'agcmd.txt', 'cmds.txt', 32, 60, 30, 5))
    if big:
        cases.append(('held-supplier', 0, 'agcmd.txt', 'cmds.txt', 32, 100, 100, 1))
        cases.append(('held-supplier', 1, 'agcmd_st12.txt', 'cmds.txt', 12, 100, 100, 1))
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('what')
    ap.add_argument('draw', nargs='?')
    ap.add_argument('--mode', type=int, default=0)
    ap.add_argument('--ag', default='agcmd.txt')
    ap.add_argument('--cmds', default='cmds.txt')
    ap.add_argument('--st', type=int, default=32)
    ap.add_argument('--issue', type=int, default=100)
    ap.add_argument('--de', type=int, default=100)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--jobs', type=int, default=3)
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--big', action='store_true')
    a = ap.parse_args()
    if a.what == 'build':
        print(build())
    elif a.what == 'run':
        ok, txt, sec = run(a.draw, a.mode, a.ag, a.cmds, a.st, a.issue, a.de, a.seed)
        print(txt)
        print('PASS' if ok else 'FAIL', '%.0fs' % sec)
    elif a.what == 'suite':
        cases = suite_cases(a.quick, a.big)
        fails = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(a.jobs, 3)) as ex:
            futs = {ex.submit(run, *c): c for c in cases}
            for f in concurrent.futures.as_completed(futs):
                c = futs[f]
                ok, txt, sec = f.result()
                summ = [ln for ln in txt.splitlines() if 'tb_dpath: cmds' in ln]
                print('%-4s %-17s mode=%d %-15s st=%d issue=%d de=%d  %4.0fs  %s' % (
                    'ok' if ok else 'FAIL', c[0], c[1], c[2], c[4], c[5], c[6], sec,
                    summ[0].split('tb_dpath: ')[1] if summ else ''), flush=True)
                if not ok:
                    fails += 1
                    print(txt)
        print('SUITE %s: %d/%d passed' % ('PASS' if not fails else 'FAIL',
                                         len(cases) - fails, len(cases)))
    else:
        ap.error('unknown command')


if __name__ == '__main__':
    main()
