"""Build and run tb_parser (DSW-4 B3b table parser) on gold draws.

    python run_parser.py build
    python run_parser.py run [--draws shapes|fuzz|all|NAME,...] [--cfg NAME,...] [-j 3]

Configurations (generics of tb_parser):
    fast   SRC 100, drain 100% x 0..4 random, LWLAT 10
    slow   SRC 60,  drain 50%  x 0..1, LWLAT 10
    mix    SRC 85,  drain 70%  x 0..2, LWLAT 14
    tight  SRC 100, drain 15%  x 0..1, LWLAT 30
    free   SRC 100, drain unthrottled (4 per cycle, the ELQ maximum), LWLAT 10
    retgt  as fast, TEST_RETGT (group act codes differ: only base/e/wcnt/p/el compared)
Every run writes the walker group trace and compares it with groups.txt.
"""
import argparse
import concurrent.futures as cf
import os
import re
import sys
import time

sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__)).replace(chr(92), '/')
RTL = 'C:/Users/Vatsal/dsw4/rtl'
GOLD = 'C:/Users/Vatsal/dsw4/sim/dsw4/gold'
SHAPES = ['tiny-chunks', 'line-straddle-17', 'line-straddle-33', 'line-straddle-65',
          'sub-line', 'medium', 'single-chunk', 'long-chunk']
FILES = ['vhsnunzip_utils_pkg.vhd', 'vhsnunzip_int_pkg.vhd', 'vhsnunzip_dsw4_pkg.vhd',
         'vhsnunzip_srl.vhd', 'vhsnunzip_fifo.vhd', 'vhsnunzip_cofifo.vhd',
         'vhsnunzip_cbuf.vhd', 'vhsnunzip_blkrd.vhd', 'vhsnunzip_pt.vhd',
         'vhsnunzip_walker.vhd', 'vhsnunzip_parser.vhd']
CFGS = {
    'fast': dict(SEED=1, SRC_PCT=100, DRAIN_PCT=100, DRAIN_MAX=4, LWLAT=10),
    'slow': dict(SEED=2, SRC_PCT=60, DRAIN_PCT=50, DRAIN_MAX=1, LWLAT=10),
    'mix':  dict(SEED=3, SRC_PCT=85, DRAIN_PCT=70, DRAIN_MAX=2, LWLAT=14),
    'tight': dict(SEED=4, SRC_PCT=100, DRAIN_PCT=15, DRAIN_MAX=1, LWLAT=30),
    'free': dict(SEED=5, SRC_PCT=100, DRAIN_PCT=100, DRAIN_MAX=4, LWLAT=10, DRAIN_FULL='true'),
    'free4': dict(SEED=5, SRC_PCT=100, DRAIN_PCT=100, DRAIN_MAX=4, LWLAT=10, DRAIN_FULL='true', WFD=4),
    'free6': dict(SEED=5, SRC_PCT=100, DRAIN_PCT=100, DRAIN_MAX=4, LWLAT=10, DRAIN_FULL='true', WFD=6),
    'retgt': dict(SEED=6, SRC_PCT=100, DRAIN_PCT=100, DRAIN_MAX=4, LWLAT=10, RETGT='true'),
}


def sp(p):
    return tools.shell_path(p)


def build():
    srcs = ' '.join(sp(RTL + '/' + f) for f in FILES) + ' ' + sp(HERE + '/tb_parser.vhd')
    cmd = ('cd %s && rm -rf work tb_parser && mkdir -p work out && ghdl -i --std=08 --workdir=work %s && '
           'ghdl -m --std=08 --workdir=work -o tb_parser tb_parser' % (sp(HERE), srcs))
    r = tools.eda_shell(cmd, timeout=1800)
    print(r.text[-4000:])
    return r.rc == 0


def data_lines(path):
    with open(path) as f:
        return [l.split() for l in f if l.strip() and not l.startswith('#')]


def cmp_groups(gold, got, retgt):
    g, r = data_lines(gold), data_lines(got)
    if retgt:   # act/arg differ under TEST_RETGT
        g = [l[:8] + l[10:] for l in g]
        r = [l[:8] + l[10:] for l in r]
    for i, (a, b) in enumerate(zip(g, r)):
        if a != b:
            return 'group %d differs: gold %s got %s' % (i, ' '.join(a), ' '.join(b))
    if len(g) != len(r):
        return 'group count gold %d got %d' % (len(g), len(r))
    return None


def run_one(draw_dir, cfg):
    name = draw_dir.replace(GOLD + '/', '').replace('/', '_')
    grp = HERE + '/out/%s_%s.grp' % (name, cfg)
    gen = ' '.join('-g%s=%s' % kv for kv in CFGS[cfg].items())
    cmd = ('cd %s && ./tb_parser -gDIR=%s -gGRP=%s %s --ieee-asserts=disable 2>&1 | tail -30'
           % (sp(HERE), sp(draw_dir), sp(grp), gen))
    t0 = time.time()
    r = tools.eda_shell(cmd, timeout=7200)
    txt = r.text
    ok = 'PARSER_PASS' in txt and 'PARSER_FAIL' not in txt and 'failure' not in txt.lower()
    m = re.search(r'PARSER_PASS elements (\d+) cycles (\d+)', txt)
    info = ('%s elements %s cycles' % m.groups()) if m else ''
    if ok:
        err = cmp_groups(draw_dir + '/groups.txt', grp, cfg == 'retgt')
        if err:
            ok = False
            info += ' GROUPS ' + err
        else:
            info += ' groups ok (%d)' % len(data_lines(grp))
    else:
        info = '\n'.join(l for l in txt.splitlines() if 'FAIL' in l or 'error' in l.lower()
                         or 'failure' in l.lower())[:2000] or txt[-2000:]
    return draw_dir, cfg, ok, info, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('what', choices=['build', 'run'])
    ap.add_argument('--draws', default='all')
    ap.add_argument('--cfg', default='fast,free')
    ap.add_argument('-j', type=int, default=3)
    a = ap.parse_args()
    if a.what == 'build':
        sys.exit(0 if build() else 1)
    fz = sorted(GOLD + '/fuzz/' + d for d in os.listdir(GOLD + '/fuzz') if d.startswith('f'))
    if a.draws == 'shapes':
        draws = [GOLD + '/' + s for s in SHAPES]
    elif a.draws == 'fuzz':
        draws = fz
    elif a.draws == 'all':
        draws = [GOLD + '/' + s for s in SHAPES] + fz
    else:
        draws = [d if '/' in d else (GOLD + '/' + d if d in SHAPES or os.path.isdir(GOLD + '/' + d)
                                     else GOLD + '/fuzz/' + d)
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
