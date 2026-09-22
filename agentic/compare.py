#!/usr/bin/env python3
"""
Put two runs side by side and say whether the second one was better.

The loop's changes are only worth what they buy, and most of that cannot be
seen from inside one run. This reads the records two runs already wrote and
prints the numbers that decide it:

  cost per percent gained  the bottom line: dollars for throughput
  adopted per session      how often a session produced something usable
  before first edit        how long a session worked the design out before it
                           changed anything (what the design map and the
                           handed-over notes are meant to shrink)
  cut off by the cap       sessions killed mid-change, which buy nothing
  wasted on the window     money spent on sessions a usage limit threw away

Older runs did not record everything; missing numbers print as n/a rather
than being guessed.

    python agentic/compare.py campaign2 my-new-run
    python agentic/compare.py --list
"""

import argparse
import json
import os
import statistics
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

import report                                  # noqa: E402


def load(name):
    path = os.path.join(report.run_dir(name), 'state.json')
    if not os.path.exists(path):
        raise SystemExit('no run called %s (looked for %s)' % (name, path))
    with open(path, encoding='utf-8') as fil:
        return json.load(fil)


def sessions(state):
    for it in state.get('iterations', []):
        for cand in it.get('candidates', []):
            if (cand.get('session') or {}).get('status') != 'retry':
                yield it, cand


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def summarise(state):
    """Every number worth comparing, out of one run's records."""
    out = {'run': state.get('run'), 'model': state.get('model'),
           'iterations': len(state.get('iterations', []))}
    cands = [c for _it, c in sessions(state)]
    out['sessions'] = len(cands)
    out['adopted'] = sum(1 for c in cands if c.get('adopted'))
    out['usable'] = sum(1 for c in cands
                        if c.get('outcome') in ('adopted', 'candidate'))
    out['no_proposal'] = sum(1 for c in cands if c.get('outcome') == 'no_proposal')
    out['capped'] = sum(1 for c in cands
                        if (c.get('session') or {}).get('status') in ('budget', 'timeout'))
    out['cost'] = round(state.get('cost_usd') or 0, 2)
    out['discarded'] = state.get('discarded_usd')
    out['gain_pct'] = (state.get('progress') or {}).get('throughput_gain_pct')
    out['per_session_usd'] = median([(c.get('session') or {}).get('cost_usd')
                                     for c in cands]) or None
    out['minutes'] = median([((c.get('session') or {}).get('seconds') or 0) / 60.0
                             for c in cands]) or None
    out['turns'] = median([(c.get('session') or {}).get('turns') for c in cands])
    out['first_edit_min'] = median([
        ((c.get('session') or {}).get('first_edit_s') or 0) / 60.0 or None
        for c in cands])
    out['output_tokens'] = median([
        ((c.get('session') or {}).get('usage') or {}).get('output_tokens')
        for c in cands])
    out['models'] = (sorted({c.get('model') for c in cands if c.get('model')})
                     or ([state['model']] if state.get('model') else []))
    if out['gain_pct']:
        spent = out['cost'] + (out['discarded'] or 0)
        out['usd_per_pct'] = round(spent / out['gain_pct'], 2)
    else:
        out['usd_per_pct'] = None
    return out


ROWS = [
    ('iterations', 'iterations', '%s'),
    ('sessions', 'sessions', '%s'),
    ('models', 'models used', '%s'),
    ('gain_pct', 'throughput gained', '%+.2f%%'),
    ('cost', 'recorded cost', '$%.2f'),
    ('discarded', 'spent on discarded sessions', '$%.2f'),
    ('usd_per_pct', 'DOLLARS PER PERCENT GAINED', '$%.2f'),
    ('adopted', 'sessions adopted', '%s'),
    ('usable', 'sessions that produced a usable candidate', '%s'),
    ('no_proposal', 'sessions that proposed nothing', '%s'),
    ('capped', 'sessions cut off by a cap', '%s'),
    ('per_session_usd', 'median session cost', '$%.2f'),
    ('minutes', 'median session minutes', '%.1f'),
    ('first_edit_min', 'median minutes before first edit', '%.1f'),
    ('turns', 'median turns', '%s'),
    ('output_tokens', 'median tokens written per session', '%s'),
]


def cell(value, fmt):
    if value is None:
        return 'n/a'
    if isinstance(value, list):
        return ', '.join(value) or 'n/a'
    try:
        return fmt % value
    except TypeError:
        return str(value)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('runs', nargs='*', help='two run names, older first')
    ap.add_argument('--list', action='store_true', help='what runs exist')
    args = ap.parse_args()

    base = report.runs_dir()
    if args.list or not args.runs:
        if not os.path.isdir(base):
            print('no runs under %s' % base)
            return 1
        for name in sorted(os.listdir(base)):
            if os.path.exists(os.path.join(base, name, 'state.json')):
                sumry = summarise(load(name))
                print('%-24s %2d iteration(s), %-18s %s'
                      % (name, sumry['iterations'],
                         ', '.join(sumry['models']) or 'n/a',
                         cell(sumry['gain_pct'], '%+.2f%% throughput')))
        return 0

    cols = [summarise(load(name)) for name in args.runs[:2]]
    width = max(44, max(len(c['run'] or '') for c in cols) + 2)
    print()
    print('%-44s %s' % ('', ''.join('%-*s' % (width, c['run']) for c in cols)))
    print('-' * (44 + width * len(cols)))
    for key, label, fmt in ROWS:
        print('%-44s %s' % (label, ''.join('%-*s' % (width, cell(c.get(key), fmt))
                                           for c in cols)))
    print()
    if len(cols) == 2 and cols[0]['usd_per_pct'] and cols[1]['usd_per_pct']:
        old, new = cols[0]['usd_per_pct'], cols[1]['usd_per_pct']
        change = 100.0 * (new - old) / old
        print('%s cost %+.0f%% per percent of throughput compared with %s.'
              % (cols[1]['run'], change, cols[0]['run']))
    if len(cols) == 2 and len({tuple(c['models']) for c in cols}) > 1:
        print('WARNING: these runs used different models, so most of any '
              'difference above is the model, not the loop. Compare runs on '
              'the same model and the same starting design.')
    missing = [c['run'] for c in cols if c['first_edit_min'] is None]
    if missing:
        print('No "before first edit" figure for %s: it ran before that was '
              'measured. The loop.log of an older run shows it roughly, as '
              'the gap between a session starting and its first "Now ..." '
              'line.'
              % ' or '.join(missing))
    return 0


if __name__ == '__main__':
    sys.exit(main())
