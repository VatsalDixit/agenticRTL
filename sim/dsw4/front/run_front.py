"""Build and run tb_front (DSW-4 B2 input side) on gold draws.

    python run_front.py build
    python run_front.py run [--draws shapes|fuzz|all|NAME,...] [--cfg NAME,...] [-j 3]

Configurations (generics of tb_front):
    fast   SRC 100, drain 100% x 0..4, LWLAT 10
    slow   SRC 60,  drain 50%  x 0..1, LWLAT 10
    mix    SRC 85,  drain 70%  x 0..2, LWLAT 14
    tight  SRC 100, drain 15%  x 0..1, LWLAT 30 (CBUF credit / CSF / ELQ credit pressure)
Each draw dir must contain cs.tv and elements.txt (gold.py).
"""
import argparse
import concurrent.futures as cf
import os
import re
import sys
import time

sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')
RTL = 'C:/Users/Vatsal/dsw4/rtl'
GOLD = 'C:/Users/Vatsal/dsw4/sim/dsw4/gold'
SHAPES = ['tiny-chunks', 'line-straddle-17', 'line-straddle-33', 'line-straddle-65',
          'sub-line', 'medium', 'single-chunk', 'long-chunk']
FILES = ['vhsnunzip_utils_pkg.vhd', 'vhsnunzip_int_pkg.vhd', 'vhsnunzip_dsw4_pkg.vhd',
         'vhsnunzip_srl.vhd', 'vhsnunzip_fifo.vhd', 'vhsnunzip_cofifo.vhd',
         'vhsnunzip_cbuf.vhd', 'vhsnunzip_parse_serial.vhd']
CFGS = {
    'fast': dict(SEED=1, SRC_PCT=100, DRAIN_PCT=100, DRAIN_MAX=4, LWLAT=10),
    'slow': dict(SEED=2, SRC_PCT=60, DRAIN_PCT=50, DRAIN_MAX=1, LWLAT=10),
    'mix':  dict(SEED=3, SRC_PCT=85, DRAIN_PCT=70, DRAIN_MAX=2, LWLAT=14),
    'tight': dict(SEED=4, SRC_PCT=100, DRAIN_PCT=15, DRAIN_MAX=1, LWLAT=30),
}


def sp(p):
    return tools.shell_path(p)


def build():
    srcs = ' '.join(sp(RTL + '/' + f) for f in FILES) + ' ' + sp(HERE + '/tb_front.vhd')
    cmd = ('cd %s && mkdir -p work && ghdl -i --std=08 --workdir=work %s && '
           'ghdl -m --std=08 --workdir=work -o tb_front tb_front' % (sp(HERE), srcs))
    r = tools.eda_shell(cmd, timeout=1800)
    print(r.text)
    return r.rc == 0


def run_one(draw_dir, cfg):
    gen = ' '.join('-g%s=%s' % kv for kv in CFGS[cfg].items())
    cmd = 'cd %s && ./tb_front -gDIR=%s %s --ieee-asserts=disable' % (sp(HERE), sp(draw_dir), gen)
    t0 = time.time()
    r = tools.eda_shell(cmd, timeout=3600)
    txt = r.text
    ok = 'FRONT_PASS' in txt and 'FRONT_FAIL' not in txt and r.rc == 0
    m = re.search(r'FRONT_PASS elements (\d+) cycles (\d+)', txt)
    info = ('%s elements %s cycles' % m.groups()) if m else ''
    mc = re.search(r'FRONT_COV (.*)', txt)
    if mc:
        info += ' | ' + mc.group(1).strip()
    if not ok:
        info = '\n'.join(l for l in txt.splitlines() if 'FAIL' in l or 'error' in l.lower())[:2000] or txt[-2000:]
    return draw_dir, cfg, ok, info, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('what', choices=['build', 'run'])
    ap.add_argument('--draws', default='all')
    ap.add_argument('--cfg', default='fast,slow,mix')
    ap.add_argument('-j', type=int, default=3)
    a = ap.parse_args()
    if a.what == 'build':
        sys.exit(0 if build() else 1)
    if a.draws == 'shapes':
        draws = [GOLD + '/' + s for s in SHAPES]
    elif a.draws == 'fuzz':
        draws = sorted(GOLD + '/fuzz/' + d for d in os.listdir(GOLD + '/fuzz') if d.startswith('f'))
    elif a.draws == 'all':
        draws = [GOLD + '/' + s for s in SHAPES] + sorted(
            GOLD + '/fuzz/' + d for d in os.listdir(GOLD + '/fuzz') if d.startswith('f'))
    else:
        draws = [d if '/' in d else (GOLD + '/' + d if d in SHAPES else GOLD + '/fuzz/' + d)
                 for d in a.draws.split(',')]
    jobs = [(d, c) for d in draws for c in a.cfg.split(',')]
    nfail = 0
    with cf.ThreadPoolExecutor(max_workers=min(3, a.j)) as ex:
        for d, c, ok, info, sec in ex.map(lambda j: run_one(*j), jobs):
            name = d.replace(GOLD + '/', '')
            print('%-6s %-22s %-5s %6.1fs %s' % ('ok' if ok else 'FAIL', name, c, sec, info), flush=True)
            nfail += not ok
    print('TOTAL %d runs, %d failed' % (len(jobs), nfail))
    sys.exit(1 if nfail else 0)


if __name__ == '__main__':
    main()
