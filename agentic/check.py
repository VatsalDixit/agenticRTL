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
"""

import argparse
import os
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import analyse                                       # noqa: E402
import measure                                       # noqa: E402
import stim                                          # noqa: E402
from tools import rmtree                             # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--quick', action='store_true')
    args = ap.parse_args()

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
