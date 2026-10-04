#!/usr/bin/env python3
"""
The one command a candidate-writing agent may run: does my design work?

It compiles the RTL in this worktree with the throughput testbench, simulates
it on stimulus it INVENTS here (chunk sizes that straddle line widths,
overlapping copies, one big chunk, and one 192 KiB chunk that wraps the
64 KiB history the way real pages do), and compares every output byte against
the frozen reference decompressor. It also reports bytes per cycle on those
shapes and where the pipeline is idle.

Nothing here touches the data the design is scored on. Passing this means
"not obviously broken", which is the bar for proposing. It does not predict
the score.

Usage:
    python agentic/check.py            all shapes (about half a minute)
    python agentic/check.py --quick    three shapes
    python agentic/check.py --unit NAME
                                       a build-track step's unit test: compile
                                       rtl/*.vhd plus rtl/unit/NAME.vhd and run
                                       entity NAME in a folder of golden data
                                       made from the invented shapes
"""

import argparse
import os
import re
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import analyse                                       # noqa: E402
import measure                                       # noqa: E402
import stim                                          # noqa: E402
from tools import eda_shell, rmtree, shell_path      # noqa: E402

UNIT_NAME = re.compile(r'^[A-Za-z0-9_]{1,40}$')
UNIT_SCRIPT = os.path.join(KIT, 'syn', 'unit.sh')
UNIT_STOP_TIME = '200ms'
UNIT_TIMEOUT_S = 1200
UNIT_LINE_BYTES = 8


def write_golden(ddir, shapes=None):
    """The golden data a unit test reads, from the invented shapes only (never
    the scoring data): cs.tv, expected.hex and elements.tv, every shape's
    chunks one after another. Returns the number of chunks.

      cs.tv         the compressed chunks, in the testbench's format
      expected.hex  the reference decompressor's output: one line per 8
                    bytes, "<16 hex digits> <count>" (the last line of a chunk
                    is zero-padded and its count says how many bytes are
                    real), then "EOC" after each chunk
      elements.tv   one line per Snappy element: "pos kind hdr len offset",
                    kind 0 = literal, 1 = copy, pos the header's byte offset
                    in its chunk; then "EOC" after each chunk
    """
    import packmodel                 # pure parsing of invented bytes; no cache
    from snappy import decompress_raw
    chunks = []
    for _name, chunk in (shapes or stim.SELFTEST_SHAPES):
        chunks += stim.selftest_chunks(chunk)
    os.makedirs(ddir, exist_ok=True)
    stim.write_cs_tv(chunks, os.path.join(ddir, 'cs.tv'))
    with open(os.path.join(ddir, 'expected.hex'), 'w', encoding='ascii', newline='\n') as fil:
        for chunk in chunks:
            plain = decompress_raw(chunk)
            for pos in range(0, len(plain), UNIT_LINE_BYTES):
                part = plain[pos:pos + UNIT_LINE_BYTES]
                fil.write('%s %d\n' % ((part + b'\x00' * (UNIT_LINE_BYTES - len(part))).hex(),
                                       len(part)))
            fil.write('EOC\n')
    with open(os.path.join(ddir, 'elements.tv'), 'w', encoding='ascii', newline='\n') as fil:
        for chunk in chunks:
            for pos, kind, hdr, length, offset in packmodel.parse(chunk):
                fil.write('%d %d %d %d %d\n' % (pos, 0 if kind == 'L' else 1, hdr, length,
                                                offset))
            fil.write('EOC\n')
    return len(chunks)


def unit_verdict(text):
    """(passed, why) from unit.sh's output."""
    if 'UNIT_COMPILE_FAIL' in text:
        return False, 'the design plus the unit test does not compile'
    if 'UNIT_ELAB_FAIL' in text:
        return False, 'the unit test does not elaborate'
    m = re.search(r'UNIT_RC=(-?\d+) STOPPED=(\d)', text)
    if not m:
        return False, 'the unit test did not run'
    if m.group(2) == '1':
        return False, ('the unit test did not end by itself within %s of simulated '
                       'time; end it with std.env.finish' % UNIT_STOP_TIME)
    if m.group(1) != '0':
        return False, 'the unit test stopped on an assertion (rc %s)' % m.group(1)
    return True, 'the unit test ran to its end without an assertion failure'


def run_unit(name, root=ROOT):
    """Compile and run rtl/unit/<name>.vhd against golden data. Prints the
    verdict and the log tail; returns the exit code (0 pass, 1 fail, 2 bad
    argument)."""
    if not UNIT_NAME.match(name or ''):
        print('check --unit: NAME must be 1-40 letters, digits or underscores')
        return 2
    unit = os.path.join(root, 'rtl', 'unit', name + '.vhd')
    if not os.path.isfile(unit):
        print('check --unit: rtl/unit/%s.vhd does not exist' % name)
        return 2
    base = os.path.join(root, '.agentic', 'selftest', 'unit')
    rmtree(base)
    run_dir = os.path.join(base, 'run')
    count = write_golden(run_dir)
    print('golden data from the invented shapes: %d chunk(s) in %s' % (count, run_dir))
    script = 'bash "%s" "%s" "%s" "%s" "%s" "%s" "%s"' % (
        shell_path(UNIT_SCRIPT), shell_path(os.path.join(root, 'rtl')), shell_path(unit),
        shell_path(os.path.join(base, 'build')), shell_path(run_dir), name, UNIT_STOP_TIME)
    res = eda_shell(script, timeout=UNIT_TIMEOUT_S, kill_token='unit-' + name)
    if res.timed_out:
        print('UNIT FAIL %s: did not finish within %d s' % (name, UNIT_TIMEOUT_S))
        return 1
    ok, why = unit_verdict(res.text)
    tail = [ln for ln in res.text.splitlines() if ln.strip()][-30:]
    print('\n'.join(tail))
    print('UNIT %s %s: %s' % ('PASS' if ok else 'FAIL', name, why))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--unit', metavar='NAME', default=None)
    args = ap.parse_args()
    if args.unit is not None:
        return run_unit(args.unit)

    shapes = stim.SELFTEST_SHAPES[:3] if args.quick else stim.SELFTEST_SHAPES
    base = os.path.join(ROOT, '.agentic', 'selftest')
    rmtree(base)
    draws = []
    for name, chunk in shapes:
        chunks = stim.selftest_chunks(chunk)
        ddir = os.path.join(base, name)
        os.makedirs(ddir, exist_ok=True)
        stim.write_cs_tv(chunks, os.path.join(ddir, 'cs.tv'))
        draws.append((stim.Draw('synthetic', name, chunk=chunk), ddir, len(chunks)))

    rtl_dir = os.path.join(ROOT, 'rtl')
    widths = analyse.rtl_widths(rtl_dir)
    print('ports read from rtl/: input %d bytes (cnt %d bits), output %d bytes '
          '(cnt %d bits), cores %d, core line %d bytes'
          % (widths['in_bytes'], widths['in_cnt_bits'], widths['out_bytes'],
             widths['out_cnt_bits'], widths['cores'], widths['core_line_bytes']))
    try:
        results = measure.simulate(rtl_dir, os.path.join(base, 'build'), draws,
                                   widths, timeout=900)
    except measure.MeasureError as exc:
        print('CHECK FAILED: %s' % exc)
        return 1

    failures = 0
    for rec in results:
        if rec['oracle_pass']:
            print('%-18s ok    %7.3f bytes/cycle   output idle %5.1f%%   %d chunk(s)'
                  % (rec['name'], rec['bytes_per_cycle'], rec['output_idle_pct'],
                     rec['chunks']))
        else:
            failures += 1
            print('%-18s FAIL  %s' % (rec['name'], (rec['problem'] or '')[:300]))

    for rec in results:
        if rec['oracle_pass'] and rec['name'] == 'medium' and rec.get('analysis'):
            print()
            print('stage profile on the "medium" shape (invented data, not the score):')
            print(analyse.describe(rec['analysis']))
            break

    print()
    if failures:
        print('%d of %d shapes FAILED. The design is broken; fix it before '
              'writing PROPOSAL.json.' % (failures, len(results)))
        return 1
    print('All %d shapes pass: every output byte matches the reference '
          'decompressor.' % len(results))
    print('This is invented stimulus. It says the design is not obviously '
          'broken; it does not predict the score.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
