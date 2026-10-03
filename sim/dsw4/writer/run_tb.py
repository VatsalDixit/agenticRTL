"""run_tb.py -- build and run tb_writer (SPEC 12 B2b) over gold traces.

    python run_tb.py build
    python run_tb.py run  [--draws shapes|fuzz|all|NAME,...] [--cases CASE,...] [--jobs 3]

A case = (variant, mode). Variants: base slots1 slots2 slots3 cut norep litp1
(and stall: base with TEST_STALL_PCT=30). Modes: A (full feed, compared with
gold's cmds file inside the testbench), B (random visibility gaps, log
replayed through gold.Writer by check_tb.py), C (B + limited arrival a_r,
invariant check by check_tb.py). Every case runs over every selected draw.
At most --jobs GHDL simulations run at once (WSL memory).
"""
import argparse
import concurrent.futures as cf
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools  # noqa: E402

GOLD = r'C:/Users/Vatsal/dsw4/sim/dsw4/gold'
RTL = r'C:/Users/Vatsal/dsw4/rtl'
WORK = os.path.join(HERE, 'work')
OUT = os.path.join(HERE, 'out')
SHAPES = ['tiny-chunks', 'line-straddle-17', 'line-straddle-33', 'line-straddle-65',
          'sub-line', 'medium', 'single-chunk', 'long-chunk']

# variant -> (cmds file, generics)
VARIANTS = {
    'base':   ('cmds.txt', {}),
    'slots1': ('cmds_slots1.txt', {'SLOTS': 1}),
    'slots2': ('cmds_slots2.txt', {'SLOTS': 2}),
    'slots3': ('cmds_slots3.txt', {'SLOTS': 3}),
    'cut':    ('cmds_cut.txt', {'CUT': 'true'}),
    'norep':  ('cmds_norep.txt', {'NOREP': 'true'}),
    'litp1':  ('cmds_litp1.txt', {'LITP1': 'true'}),
    'stall':  ('cmds.txt', {'STALL_PCT': 30}),
}
MODES = {'A': 0, 'B': 1, 'C': 2}


def wsl(p):
    return tools.shell_path(p)


def build():
    os.makedirs(WORK, exist_ok=True)
    files = ['vhsnunzip_utils_pkg.vhd', 'vhsnunzip_pkg.vhd', 'vhsnunzip_int_pkg.vhd',
             'vhsnunzip_dsw4_pkg.vhd', 'vhsnunzip_elq.vhd', 'vhsnunzip_writer.vhd']
    src = ' '.join(wsl(os.path.join(RTL, f)) for f in files) + ' ' + wsl(os.path.join(HERE, 'tb_writer.vhd'))
    script = ('cd %s && rm -f *.o *.cf tb_writer && ghdl -a --std=08 -frelaxed %s && '
              'ghdl -e --std=08 -frelaxed tb_writer && echo BUILD_OK') % (wsl(WORK), src)
    r = tools.eda_shell(script, timeout=1800)
    print(r.text)
    return 'BUILD_OK' in r.text


def draw_list(sel):
    if sel == 'shapes':
        names = SHAPES
    elif sel == 'fuzz':
        names = ['fuzz/' + d for d in sorted(os.listdir(os.path.join(GOLD, 'fuzz')))
                 if os.path.exists(os.path.join(GOLD, 'fuzz', d, 'elements.txt'))]
    elif sel == 'all':
        return draw_list('shapes') + draw_list('fuzz')
    else:
        names = sel.split(',')
    return names


def run_case(variant, mode, draws, tag, seed, feed=45, arstep=48):
    cmdf, gen = VARIANTS[variant]
    os.makedirs(OUT, exist_ok=True)
    name = '%s_%s%s%s_%s' % (variant, mode, '' if mode == 'A' else str(feed),
                            'r%d' % arstep if mode == 'C' and arstep != 48 else '', tag)
    dl = os.path.join(OUT, name + '.dirs')
    with open(dl, 'w', newline='\n') as f:
        for d in draws:
            f.write(wsl(os.path.join(GOLD, d)) + '\n')
    log = os.path.join(OUT, name + '.log')
    g = dict(gen)
    g['MODE'] = MODES[mode]
    g['SEED'] = seed
    if mode == 'A':
        g['CRED_PCT'] = 20
    else:
        g['CRED_PCT'] = 15
        g['FEED_PCT'] = feed
        g['AR_STEP'] = arstep
    args = ' '.join('-g%s=%s' % (k, v) for k, v in g.items())
    script = ('cd %s && ./tb_writer --ieee-asserts=disable-at-0 -gDIRLIST=%s -gCMDF=%s -gLOGF=%s %s 2>&1 | '
              'grep -v "^.*DRAW .* errors=0$" | tail -40') % (
        wsl(WORK), wsl(dl), cmdf, wsl(log), args)
    t0 = time.time()
    r = tools.eda_shell(script, timeout=10800)
    txt = r.text
    ok = 'TB_PASS' in txt and 'TB_FAIL' not in txt
    res = dict(case=name, variant=variant, mode=mode, ok=ok, secs=time.time() - t0, tail=txt[-3000:])
    res['pass_line'] = next((l[l.index('TB_PASS'):] for l in txt.splitlines() if 'TB_PASS' in l), '')
    if ok and mode in ('B', 'C'):
        import check_tb
        cres = check_tb.check_log(log, variant, replay=(mode == 'B'))
        res['check'] = cres
        res['ok'] = cres['ok']
        if cres['ok']:
            os.remove(log)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('what', choices=['build', 'run'])
    ap.add_argument('--draws', default='shapes')
    ap.add_argument('--cases', default='base:A')
    ap.add_argument('--jobs', type=int, default=3)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    if a.what == 'build':
        sys.exit(0 if build() else 1)
    draws = draw_list(a.draws)
    tag = a.tag or a.draws.replace(',', '+').replace('/', '_')[:40]
    cases = [c.split(':') for c in a.cases.split(',')]
    allok = True
    with cf.ThreadPoolExecutor(max_workers=min(3, a.jobs)) as ex:
        futs = {ex.submit(run_case, c[0], c[1], draws, '%s_s%d' % (tag, a.seed), a.seed,
                          int(c[2]) if len(c) > 2 else 45, int(c[3]) if len(c) > 3 else 48): c for c in cases}
        for fu in cf.as_completed(futs):
            r = fu.result()
            allok &= r['ok']
            line = '%-28s %s  %.0fs' % (r['case'], 'PASS' if r['ok'] else 'FAIL', r['secs'])
            line += '  ' + r.get('pass_line', '')
            if 'check' in r:
                line += '  ' + r['check']['summary']
            print(line, flush=True)
            if not r['ok']:
                print(r['tail'])
                if 'check' in r:
                    print('\n'.join(r['check']['errors'][:15]))
    print('ALL_PASS' if allok else 'SOME_FAIL')
    sys.exit(0 if allok else 1)


if __name__ == '__main__':
    main()
