"""mutate.py -- show that tb_dpath fails on wrong datapaths.

Each mutation copies the rtl files to work/mut/<name>/rtl, patches one file,
builds into work/mut/<name>/build and runs one tb case; the mutation is
"caught" when the case FAILS. Never touches C:/Users/Vatsal/dsw4/rtl.
"""
import concurrent.futures
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as R  # noqa: E402

# name, file, old, new, case kwargs
MUTS = [
    ('hsb', 'vhsnunzip_dpath.vhd', 'x.hsb(k)(p) := not lane(4);', 'x.hsb(k)(p) := lane(4);',
     dict(draw='medium')),
    ('sta', 'vhsnunzip_dpath.vhd', 'x.sta(k, j) := d2.dhi(k) - 1;', 'x.sta(k, j) := d2.dhi(k);',
     dict(draw='medium')),
    ('d2', 'vhsnunzip_dpath.vhd', 'd2 <= d1;', 'd2 <= ag;', dict(draw='long-chunk')),
    ('adev', 'vhsnunzip_agen.vhd', 'adev := l0(12 downto 1) + 1;', 'adev := l0(12 downto 1);',
     dict(draw='long-chunk', mode=1)),
    ('hwpar', 'vhsnunzip_dpath.vhd', 'to_integer(hw_ptr(0 downto 0)) = par',
     'to_integer(hw_ptr(0 downto 0)) /= par', dict(draw='long-chunk')),
    ('ltpar', 'vhsnunzip_dpath.vhd', 'par := not par;', 'par := par;', dict(draw='train-taxi')),
    ('spill', 'vhsnunzip_dpath.vhd', 'pv.data  := ol;', 'pv.data  := s3_new;',
     dict(draw='line-straddle-33')),
    ('repmask', 'vhsnunzip_dpath.vhd', 'lane := d2.rep_base + (rel and msk);',
     'lane := d2.rep_base + rel;', dict(draw='long-chunk')),
    ('olpush', 'vhsnunzip_dpath.vhd', 'pv.data(j) := ol(j);', 'pv.data(j) := s3_new(j);',
     dict(draw='medium')),
    ('olpush_ns', 'vhsnunzip_dpath.vhd', 'pv.data(j) := ol(j);', 'pv.data(j) := s3_new(j);',
     dict(draw='medium', mode=1, ag='agcmd_cut.txt', cmds='cmds_cut.txt')),
    ('rowA', 'vhsnunzip_dpath.vhd', 'x.rowA(j) := d2.crowA + 1;', 'x.rowA(j) := d2.crowA;',
     dict(draw='medium')),
    ('credit', 'vhsnunzip_defifo.vhd', '>= DE_CRED then', '>= 1 then',
     dict(draw='medium', issue=100, de=10)),
    ('lwx', 'vhsnunzip_dpath.vhd', 'lwx_r <= s1.lw;', 'lwx_r <= s1.lw + 200;',
     dict(draw='train-taxi')),
    ('st_lines', 'vhsnunzip_agen.vhd', 'constant ST_MAX : natural := 32 * TEST_ST_LINES - 1;',
     'constant ST_MAX : natural := 32 * TEST_ST_LINES - 2;',
     dict(draw='train-taxi', mode=1, ag='agcmd_st12.txt', st=12)),
]


def one(m):
    name, fil, old, new, kw = m
    root = os.path.join(HERE, 'work', 'mut', name)
    rtl = os.path.join(root, 'rtl')
    bdir = os.path.join(root, 'build')
    os.makedirs(rtl, exist_ok=True)
    for f in R.SRCS:
        shutil.copy(os.path.join(R.RTL, f), os.path.join(rtl, f))
    p = os.path.join(rtl, fil)
    s = open(p).read()
    if s.count(old) != 1:
        return name, 'BAD-PATTERN (%d matches)' % s.count(old), ''
    open(p, 'w', newline='\n').write(s.replace(old, new))
    b = R.build(rtl, bdir)
    if 'BUILD_OK' not in b:
        return name, 'BUILD-FAIL', b
    ok, txt, sec = R.run(bdir=bdir, **kw)
    why = [ln for ln in txt.splitlines() if 'error' in ln or 'failure' in ln][:2]
    return name, 'caught' if not ok else 'MISSED', '\n'.join(why)


if __name__ == '__main__':
    sel = sys.argv[1:]
    muts = [m for m in MUTS if not sel or m[0] in sel]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        for name, verdict, why in ex.map(one, muts):
            print('%-9s %s' % (name, verdict))
            for ln in why.splitlines():
                print('          ' + ln[-170:])
