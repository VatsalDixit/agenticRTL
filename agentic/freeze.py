#!/usr/bin/env python3
"""
The measuring instrument must not change while it is measuring.

These files decide whether a candidate is correct and how fast it is. They
are hashed once at setup (agentic/frozen.json) and checked every time the
loop starts and before every scoring pass. A candidate is never scored with a
copy of these files from its own worktree, so it cannot edit them into
passing; this check is against someone editing them by hand mid-run.

Usage:
    python agentic/freeze.py            check
    python agentic/freeze.py --record   re-record after a deliberate change
"""

import argparse
import json
import os
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

from tools import sha256_file   # noqa: E402

FROZEN = [
    'ref/snappy.py',
    'ref/parquet_pages.py',
    'oracle.py',
    'stim.py',
    # Every scored simulation runs on the copy of rtl/ that probe.py
    # instruments, so it is part of the instrument too.
    'probe.py',
    'tb/vhsnunzip_perf_tc.sim.08.vhd',
    'syn/sim_draws.sh',
    'syn/synth.sh',
    'syn/ram_stub.vhd',
    'syn/vivado.tcl',
    'syn/ram_xilinx.vhd',
]
HASHES = os.path.join(KIT, 'frozen.json')


def current():
    out = {}
    for rel in FROZEN:
        path = os.path.join(KIT, rel)
        out[rel] = sha256_file(path) if os.path.exists(path) else 'MISSING'
    return out


def record():
    data = current()
    with open(HASHES, 'w', encoding='utf-8') as fil:
        json.dump(data, fil, indent=1, sort_keys=True)
    return data


def check():
    """List of problems; empty means every frozen file is as recorded."""
    if not os.path.exists(HASHES):
        return ['no agentic/frozen.json -- run: python agentic/freeze.py --record']
    with open(HASHES, encoding='utf-8') as fil:
        want = json.load(fil)
    now = current()
    problems = []
    for rel, digest in sorted(want.items()):
        got = now.get(rel)
        if got != digest:
            problems.append('%s has changed since it was frozen' % rel)
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--record', action='store_true')
    args = ap.parse_args()
    if args.record:
        data = record()
        for rel, digest in sorted(data.items()):
            print('  %-40s %s' % (rel, digest[:16]))
        return 0
    problems = check()
    if problems:
        print('FROZEN FILES CHANGED:')
        for p in problems:
            print('  - %s' % p)
        return 1
    print('All %d frozen files intact.' % len(FROZEN))
    return 0


if __name__ == '__main__':
    sys.exit(main())
