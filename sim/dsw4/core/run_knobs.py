"""B2c integration runs of the whole design (vhsnunzip_unbuffered) with the
harness throughput testbench, optionally with the SPEC 7 TEST_* knobs.

The kit testbench cannot pass generics to the design, so this script writes
tb_perf_knobs.vhd: the kit's vhsnunzip_perf_tc.sim.08.vhd with the TEST_*
generics added to its entity and forwarded to the uut, nothing else changed.
It is regenerated from the kit file on every run. Stimulus, sink, counters
and out.hex format are therefore the kit's.

    python run_knobs.py VARIANT[,VARIANT...] SET[,SET...] [--jobs N]

VARIANT  base | slots1 | slots2 | slots3 | cut | norep | litp1 | st12 | stall30
SET      shapes (the 8 self-test shapes, gold traces in sim/dsw4/gold)
         fuzz   (the 200 fuzz_tv streams in sim/dsw4/gold/fuzz)
         taxi   (train-taxi, kit agenticRTL_v2_real corpus)

VARIANT also: bp6060 / bp9030 (SRC_PCT/SNK_PCT port backpressure).

Per draw (FAIL on any of these): simulation rc / deadlock / any sim.log
output (an assert), chunk count, the per-chunk byte stream against gold
out.hex (shapes, fuzz) or against the frozen oracle (taxi), and for every
variant other than base, the per-chunk byte stream equal to the base run's.
Also FAIL: out.hex framing different from base in any way other than
cnt=0 last=1 transfers in front of an EOC. Reported as notes (not
failures): transfer framing different from gold, and out.hex differing from
base only by such empty last transfers (the port convention allows a
zero-size last transfer of a non-empty chunk; it appears when a variant
issues the EOC in its own command after a command completed the chunk's
final 32-byte line). Prints
B/cycle from perf.txt. The base run must come first (it is the reference).
"""
import argparse, os, re, shutil, sys, time

sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, 'work')
RTL = r'C:/Users/Vatsal/dsw4/rtl'
GOLD = r'C:/Users/Vatsal/dsw4/sim/dsw4/gold'
KIT_TB = r'C:/Users/Vatsal/dsw4/agentic/tb/vhsnunzip_perf_tc.sim.08.vhd'
SIM_SH = r'C:/Users/Vatsal/dsw4/agentic/syn/sim_draws.sh'
TAXI = r'C:/Users/Vatsal/agenticRTL_v2_real/.agentic/corpus/train-taxi'
SHAPES = ['tiny-chunks', 'line-straddle-17', 'line-straddle-33', 'line-straddle-65',
          'sub-line', 'medium', 'single-chunk', 'long-chunk']
WIDTHS = '-gCO_BYTES=32 -gCO_CNT_BITS=5 -gDE_BYTES=32 -gDE_CNT_BITS=6'

VARIANTS = {
    'base':    '',
    'slots1':  '-gTEST_SLOTS=1',
    'slots2':  '-gTEST_SLOTS=2',
    'slots3':  '-gTEST_SLOTS=3',
    'cut':     '-gTEST_CUT=true',
    'norep':   '-gTEST_NOREP=true',
    'litp1':   '-gTEST_LITP1=true',
    'st12':    '-gTEST_ST_LINES=12',
    'stall30': '-gTEST_STALL_PCT=30',
    # Port backpressure (SPEC B5 style): framing may legitimately differ.
    'bp6060':  '-gSRC_PCT=60 -gSNK_PCT=60',
    'bp9030':  '-gSRC_PCT=90 -gSNK_PCT=30',
}

KNOBS = [('TEST_SLOTS', 'natural', '4'), ('TEST_CUT', 'boolean', 'false'),
         ('TEST_NOREP', 'boolean', 'false'), ('TEST_LITP1', 'boolean', 'false'),
         ('TEST_RETGT', 'boolean', 'false'), ('TEST_ST_LINES', 'natural', '32'),
         ('TEST_STALL_PCT', 'natural', '0')]


def make_tb(path):
    s = open(KIT_TB, encoding='utf-8').read()
    gen = ''.join('    %-14s: %-8s := %s;\n' % (n, t, d) for n, t, d in KNOBS)
    old = '    EXPECT_CHUNKS : natural  := 0\n  );'
    assert old in s
    s = s.replace(old, gen + old, 1)
    old = '  uut: entity work.vhsnunzip_unbuffered\n    port map ('
    assert old in s
    gm = ',\n'.join('      %s => %s' % (n, n) for n, _, _ in KNOBS)
    s = s.replace(old, '  uut: entity work.vhsnunzip_unbuffered\n    generic map (\n'
                  + gm + '\n    )\n    port map (', 1)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(s)


def chunks_of(out_hex):
    """Per-chunk byte strings (hex) and the transfer lines of each chunk."""
    streams, lines, cur, cl = [], [], [], []
    with open(out_hex, encoding='ascii', errors='replace') as f:
        for ln in f:
            ln = ln.strip()
            if ln == 'EOC':
                streams.append(''.join(cur)); lines.append(cl); cur, cl = [], []
            else:
                cur.append(ln); cl.append(ln)
    return streams, lines, (cur != [] or cl != [])


def _lines(path):
    with open(path, encoding='ascii', errors='replace') as f:
        return [l.rstrip('\r\n') for l in f]


def strip_empty_last(path):
    """out.hex lines without the empty transfers that directly precede an
    EOC (a cnt=0, last=1 transfer of a non-empty chunk, which the port
    convention allows), and without empty-chunk markers' duplicates."""
    ls, out = _lines(path), []
    for i, l in enumerate(ls):
        if l == '' and i + 1 < len(ls) and ls[i + 1] == 'EOC' and out and out[-1] != 'EOC':
            continue
        out.append(l)
    return out


def count_empty_last(path):
    return len(_lines(path)) - len(strip_empty_last(path))


def draws_for(sets):
    out = []
    for st in sets:
        if st == 'shapes':
            for n in SHAPES:
                gold = os.path.join(GOLD, n, 'out.hex')
                out.append((n, os.path.join(GOLD, n, 'cs.tv'), len(chunks_of(gold)[0]), gold))
        elif st == 'fuzz':
            with open(os.path.join(GOLD, 'fuzz', 'manifest.txt')) as f:
                for ln in f:
                    if ln.startswith('#') or not ln.strip():
                        continue
                    p = ln.split()
                    d = os.path.join(GOLD, 'fuzz', p[0])
                    out.append(('fuzz-' + p[0], os.path.join(d, 'cs.tv'), int(p[1]),
                                os.path.join(d, 'out.hex')))
        elif st == 'taxi':
            import json
            meta = json.load(open(os.path.join(TAXI, 'meta.json')))
            out.append(('train-taxi', os.path.join(TAXI, 'cs.tv'), meta['chunks'], None))
        else:
            raise SystemExit('unknown set ' + st)
    return out


def run_variant(var, draws, jobs, tag):
    vdir = os.path.join(WORK, 'knob_' + var)
    # One build folder per (variant, draw set): sim_draws.sh deletes and
    # rebuilds the executable, so two runs must never share a build folder.
    build = os.path.join(vdir, 'build_' + tag)
    os.makedirs(build, exist_ok=True)
    tb = os.path.join(HERE, 'tb_perf_knobs.vhd')
    make_tb(tb)  # deterministic: rewriting it under a running build is harmless
    specs = []
    for name, cs, nch, _gold in draws:
        d = os.path.join(vdir, 'draws', name)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(cs, os.path.join(d, 'cs.tv'))
        specs.append('"%s:%d"' % (tools.shell_path(d), nch))
    # sim_draws.sh compiles the kit tb name vhsnunzip_perf_tc: the knob tb keeps it.
    gen = WIDTHS + (' ' + VARIANTS[var] if VARIANTS[var] else '')
    cmd = 'SIM_JOBS=%d bash "%s" "%s" "%s" "%s" "%s" 100ms %s' % (
        jobs, tools.shell_path(SIM_SH), tools.shell_path(RTL), tools.shell_path(tb),
        tools.shell_path(build), gen, ' '.join(specs))
    t = time.time()
    r = tools.eda_shell(cmd, timeout=7000)
    txt = r.text
    if 'ELAB_OK' not in txt:
        print(txt[-3000:]); raise SystemExit('build failed')
    status = {}
    for ln in txt.splitlines():
        m = re.match(r'DRAW (\S+) rc=(-?\d+) deadlock=(\d)', ln)
        if m:
            status[m.group(1)] = (int(m.group(2)), m.group(3) == '1')
    return vdir, status, time.time() - t


def check(var, vdir, status, draws):
    sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
    import oracle
    bad = nnote = 0
    rows = []
    for name, cs, nch, gold in draws:
        d = os.path.join(vdir, 'draws', name)
        rc, dl = status.get(tools.shell_path(d), (99, False))
        out = os.path.join(d, 'out.hex')
        msg, note = [], []
        bpc = None
        if rc != 0 or dl or not os.path.exists(out):
            log = open(os.path.join(d, 'sim.log'), errors='replace').read()[-600:] \
                if os.path.exists(os.path.join(d, 'sim.log')) else ''
            msg.append('rc=%d deadlock=%d %s' % (rc, dl, log.replace('\n', ' | ')))
        else:
            log = open(os.path.join(d, 'sim.log'), errors='replace').read()
            if log.strip():
                msg.append('sim.log not empty: ' + log[:300].replace('\n', ' | '))
            perf = dict(l.strip().split('=') for l in open(os.path.join(d, 'perf.txt')) if '=' in l)
            bpc = int(perf['bytes_out']) / max(1, int(perf['cycles']))
            got, glines, tail = chunks_of(out)
            if tail:
                msg.append('output after the last EOC')
            if len(got) != nch:
                msg.append('chunks %d != %d' % (len(got), nch))
            if gold:
                want, wlines, _ = chunks_of(gold)
                for i, (a, b) in enumerate(zip(got, want)):
                    if a != b:
                        msg.append('chunk %d bytes differ from gold' % i); break
                else:
                    if glines != wlines:
                        note.append('framing differs from gold (bytes equal)')
            else:
                probs = oracle.check(os.path.join(d, 'cs.tv'), out)
                if probs:
                    msg.append('oracle: ' + probs[0])
            if var != 'base':
                bout = os.path.join(WORK, 'knob_base', 'draws', name, 'out.hex')
                if not os.path.exists(bout):
                    msg.append('no base run to compare')
                elif open(bout, 'rb').read() != open(out, 'rb').read():
                    if chunks_of(bout)[0] != got:
                        msg.append('output bytes differ from base')
                    elif strip_empty_last(bout) != strip_empty_last(out):
                        msg.append('framing of non-empty transfers differs from base')
                    else:
                        note.append('out.hex differs from base only by %d cnt=0 last=1 '
                                    'transfers (bytes identical)'
                                    % abs(count_empty_last(bout) - count_empty_last(out)))
                else:
                    note.append('out.hex identical to base')
        ok = not msg
        nnote += any('differs' in n for n in note)
        bad += not ok
        rows.append('%-8s %-22s %s %s %s' % (var, name, 'ok  ' if ok else 'FAIL',
                                            '%.4f' % bpc if bpc is not None else '-',
                                            '; '.join(msg + note)))
    return bad, nnote, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('variants'); ap.add_argument('sets')
    ap.add_argument('--jobs', type=int, default=3)
    ap.add_argument('--quiet', action='store_true', help='print only failing draws')
    ap.add_argument('--check-only', action='store_true',
                    help='re-check existing outputs without simulating (rc taken as 0 '
                         'where perf.txt exists)')
    a = ap.parse_args()
    draws = draws_for(a.sets.split(','))
    total_bad = 0
    for var in a.variants.split(','):
        if a.check_only:
            vdir, secs = os.path.join(WORK, 'knob_' + var), 0.0
            status = {}
            for name, _cs, _n, _g in draws:
                d = os.path.join(vdir, 'draws', name)
                status[tools.shell_path(d)] = (0 if os.path.exists(os.path.join(d, 'perf.txt'))
                                               else 99, False)
        else:
            vdir, status, secs = run_variant(var, draws, a.jobs, a.sets.replace(',', '_'))
        bad, nnote, rows = check(var, vdir, status, draws)
        for r in rows:
            if not a.quiet or ' FAIL ' in r or 'differs' in r:
                print(r)
        print('VARIANT %s: %d/%d ok, %d with framing notes (%.0f s)'
              % (var, len(draws) - bad, len(draws), nnote, secs), flush=True)
        total_bad += bad
    print('ALL_PASS' if total_bad == 0 else 'FAILURES %d' % total_bad)
    sys.exit(1 if total_bad else 0)


if __name__ == '__main__':
    main()
