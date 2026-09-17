#!/usr/bin/env python3
"""
The skill library: what has worked, what has not, and why.

Each skill is a pattern (a bottleneck shape you can recognise in the profile
or the RTL) paired with a strategy (what to change), a confidence level and
simple counters. The loop reads the library into every candidate's brief and
updates it after every iteration from the group of candidates it just
measured (see learn.py). The file is plain JSON so a person can read and edit
it too.

    kind         mechanism  something to try; it has a confidence and counters
                 rule       an invariant or a stop rule; always shown, never
                            rated, never counted. A rule is not evidence for or
                            against itself when a candidate that cited it lost.

    confidence   high    worked repeatedly, try it first        (mechanisms only)
                 medium  worked sometimes, depends on the design state
                 low     unproven or risky
                 avoid   measured useless, absorbed by synthesis, or broke
                         correctness; do not spend a candidate on it

A skill's pattern and strategy are written once and never rewritten by the
learning step: an earlier campaign had one failed attempt turn "widen the
line" into "be very cautious about widening the line", and every session read
that for the next ten iterations while line widening was the change that
eventually won. The learner adds short caveats under the strategy instead.

Usage:
    python agentic/skills.py            print the library
    python agentic/skills.py --distil <run-dir> [--out FILE]
                                        a library for the next campaign
"""

import argparse
import json
import os
import re
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(KIT, 'skills.json')
ORDER = ('high', 'medium', 'low', 'avoid')


def load(path=None):
    path = path or SEED
    with open(path, encoding='utf-8') as fil:
        data = json.load(fil)
    data.setdefault('skills', [])
    kept = []
    for sk in data['skills']:
        if not isinstance(sk, dict) or not sk.get('id'):
            continue                      # a hand edit gone wrong; skip it
        sk.setdefault('pattern', '')
        sk.setdefault('strategy', '')
        sk.setdefault('kind', 'mechanism')
        if sk['kind'] not in ('mechanism', 'rule'):
            sk['kind'] = 'mechanism'
        sk.setdefault('caveats', [])
        sk.setdefault('confidence', 'low')
        if str(sk['confidence']).lower() not in ORDER:
            sk['confidence'] = 'low'
        sk['confidence'] = str(sk['confidence']).lower()
        sk.setdefault('tried', 0)
        sk.setdefault('passed', 0)
        sk.setdefault('adopted', 0)
        sk.setdefault('advantages', [])
        sk.setdefault('notes', [])
        kept.append(sk)
    data['skills'] = kept
    return data


def save(data, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fil:
        json.dump(data, fil, indent=1, sort_keys=True)
    os.replace(tmp, path)


def by_id(data, sid):
    for sk in data['skills']:
        if sk['id'] == sid:
            return sk
    return None


def record_outcome(data, skill_ids, passed, adopted, advantage,
                   count_advantage=True):
    """Bump the counters of the one mechanism a candidate was steered by.

    Rules are skipped: a session citing "score on real data, not synthetic"
    and then losing says nothing about that rule, and an earlier campaign
    demoted three invariants exactly that way.
    """
    for sid in skill_ids or []:
        sk = by_id(data, sid)
        if not sk or sk.get('kind') == 'rule':
            continue
        sk['tried'] += 1
        if passed:
            sk['passed'] += 1
        if adopted:
            sk['adopted'] += 1
        # With two candidates the group advantage is always +1 or -1, which is
        # a coin flip, so it is only kept when the group was big enough to mean
        # something.
        if advantage is not None and count_advantage:
            sk['advantages'] = (sk['advantages'] + [round(advantage, 3)])[-20:]


def step_confidence(sk, wanted):
    """Move a skill's confidence at most one level per iteration, and only
    with evidence.

    One failed attempt is not a reason to mark a skill 'avoid': the biggest
    win of an earlier campaign was a line widening that failed on its first
    try. So: down one level at a time, 'avoid' only after two measured tries
    with no adoption, 'high' only after an adoption.
    """
    cur = sk['confidence']
    if wanted == cur or wanted not in ORDER or cur not in ORDER:
        return cur
    ci, wi = ORDER.index(cur), ORDER.index(wanted)
    if wi > ci:                                   # demotion, one step
        nxt = ORDER[ci + 1]
        if nxt == 'avoid' and not (sk.get('tried', 0) >= 2 and sk.get('adopted', 0) == 0):
            return cur
        return nxt
    nxt = ORDER[ci - 1]                           # promotion, one step
    if nxt == 'high' and sk.get('adopted', 0) < 1:
        return cur
    return nxt


def apply_updates(data, updates, iteration):
    """Merge skill updates proposed by the learning step.

    Each update: {id, pattern, strategy, confidence, note}. An unknown id
    adds a new skill; a known id may change its confidence and add a note.
    """
    changed = []
    if isinstance(updates, dict):
        updates = [updates]
    for up in updates or []:
        if not isinstance(up, dict):
            continue
        sid = str(up.get('id') or '').strip()
        sid = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in sid.lower())[:60]
        if not sid:
            continue
        # A missing or unknown confidence means "no change". It used to mean
        # "low", so the note-only update the learner is asked to prefer
        # silently demoted the skill it was writing about.
        conf = str(up.get('confidence') or '').lower().strip()
        pattern = str(up.get('pattern') or '').strip()
        strategy = str(up.get('strategy') or '').strip()
        note = clean_note(up.get('note'))
        caveat = str(up.get('caveat') or '').strip()
        sk = by_id(data, sid)
        if sk is None:
            if not pattern or not strategy:
                continue
            sk = {'id': sid, 'pattern': pattern[:600], 'strategy': strategy[:900],
                  'kind': 'rule' if str(up.get('kind')) == 'rule' else 'mechanism',
                  'confidence': conf if conf in ORDER else 'low',
                  'tried': 0, 'passed': 0, 'adopted': 0,
                  'advantages': [], 'notes': [], 'caveats': [],
                  'source': 'learned at iteration %d' % iteration}
            data['skills'].append(sk)
            changed.append('new: ' + sid)
        else:
            if conf in ORDER and sk.get('kind') != 'rule':
                new_conf = step_confidence(sk, conf)
                if new_conf != sk['confidence']:
                    changed.append('%s: %s -> %s' % (sid, sk['confidence'], new_conf))
                    sk['confidence'] = new_conf
            # A rewritten strategy is kept as a caveat under the original,
            # unless the learner explicitly asks to replace it AND the skill
            # has an adoption behind it.
            if strategy and strategy != sk['strategy']:
                if up.get('replace_strategy') and sk.get('adopted', 0) >= 1:
                    sk.setdefault('strategy_history', []).append(sk['strategy'])
                    sk['strategy'] = strategy[:900]
                    changed.append('%s: strategy replaced' % sid)
                elif not caveat:
                    caveat = strategy
            if caveat:
                sk['caveats'] = (sk.get('caveats', []) + [caveat[:200]])[-2:]
        if note:
            sk['notes'] = (sk['notes'] + ['i%d: %s' % (iteration, note)])[-6:]
    return changed


def clean_note(text, cap=400):
    """One learner note: no repeated iteration prefix, cut at a sentence."""
    text = ' '.join(str(text or '').split())
    text = re.sub(r'^(?:i\d+(?:\s+c\d+)?\s*[:.-]\s*)+', '', text)
    if len(text) <= cap:
        return text
    cut = text[:cap]
    stop = max(cut.rfind('. '), cut.rfind('; '))
    return (cut[:stop + 1] if stop > cap // 2 else cut.rstrip()) + ''


def mean_advantage(sk):
    adv = sk.get('advantages') or []
    return sum(adv) / len(adv) if adv else None


def format_for_prompt(data, max_entries=40, notes_per_skill=2):
    """The library as text for a brief: rules first, then mechanisms."""
    lines = []
    rules = [s for s in data['skills'] if s.get('kind') == 'rule']
    if rules:
        lines.append('RULES (these always apply; they are not ranked and do not expire)')
        for sk in rules:
            lines.append('- %s' % sk['id'])
            lines.append('    pattern:  %s' % sk['pattern'])
            lines.append('    strategy: %s' % sk['strategy'])
            for cav in (sk.get('caveats') or [])[-1:]:
                lines.append('    caveat:   %s' % cav)
        lines.append('')
    mechanisms = [s for s in data['skills'] if s.get('kind') != 'rule']
    # A cap per group, so a growing library never pushes the AVOID entries
    # (the ones that stop a session repeating a known failure) off the end.
    per_group = max(4, max_entries // 3)
    for conf in ORDER:
        group = [s for s in mechanisms if s['confidence'] == conf]
        # Sorted by what was actually adopted. The old sort was by mean group
        # advantage, which with two candidates is always +1 or -1.
        group.sort(key=lambda s: (-s['adopted'], -s['tried']))
        if not group:
            continue
        title = {'high': 'HIGH confidence (try these first)',
                 'medium': 'MEDIUM confidence (depends on the design state)',
                 'low': 'LOW confidence (unproven or risky)',
                 'avoid': 'AVOID (measured useless, absorbed by synthesis, or broke correctness)'}[conf]
        lines.append(title)
        limit = len(group) if conf == 'avoid' else per_group
        for sk in group[:limit]:
            stats = ''
            if sk['tried']:
                stats = ' [tried %d, passed the check %d, adopted %d]' % (
                    sk['tried'], sk['passed'], sk['adopted'])
            lines.append('- %s%s' % (sk['id'], stats))
            lines.append('    pattern:  %s' % sk['pattern'])
            lines.append('    strategy: %s' % sk['strategy'])
            for cav in (sk.get('caveats') or [])[-2:]:
                lines.append('    caveat: %s' % cav)
            for note in sk['notes'][-notes_per_skill:] if notes_per_skill else []:
                lines.append('    note: %s' % note)
        lines.append('')
    return '\n'.join(lines).strip()


def distil(run_dir, seed_path=None):
    """A library for a new campaign, out of a finished run.

    What carries over is what stays true: the facts sessions verified about
    the design, and the mechanisms that were measured not worth trying again.
    What does not carry over is every confidence and counter, because those
    describe a design that no longer exists -- an earlier campaign\'s learned
    library held the winning mechanism at "medium" with its advice rewritten
    into a warning, and starting a new campaign from it would have steered
    away from that campaign\'s first and largest win.

    Returns (data, carried_ids, facts).
    """
    data = load(seed_path or SEED)
    prev_path = os.path.join(run_dir, 'skills.json')
    prev = load(prev_path) if os.path.exists(prev_path) else {'skills': []}
    state = {}
    try:
        with open(os.path.join(run_dir, 'state.json'), encoding='utf-8') as fil:
            state = json.load(fil)
    except (OSError, ValueError):
        pass
    widths = ((state.get('best') or {}).get('metrics') or {}).get('widths') or {}
    where = ('measured on a %d-byte core line with %d core(s)'
             % (int(widths.get('core_line_bytes') or 0), widths.get('cores') or 1))
    run_name = os.path.basename(os.path.normpath(run_dir))

    carried = []
    for sk in prev['skills']:
        if sk.get('confidence') != 'avoid':
            continue
        mine = by_id(data, sk['id'])
        # A library written before rules existed rates everything, including
        # the invariants. The seed decides what is a rule, not the old run.
        if (mine or sk).get('kind') == 'rule':
            continue
        if mine is None:
            mine = dict(sk)
            mine.update({'tried': 0, 'passed': 0, 'adopted': 0,
                         'advantages': [], 'notes': []})
            data['skills'].append(mine)
        mine['confidence'] = 'avoid'
        mine['caveats'] = ((mine.get('caveats') or [])[-1:]
                           + ['%s measured this not worth trying again, %s. If '
                              'the design has moved on, say why before spending '
                              'a candidate on it.' % (run_name, where)])
        carried.append(sk['id'])

    facts = [f for f in (state.get('design_facts') or []) if f][-24:]
    if facts:
        data['design_facts'] = facts
    return data, carried, facts


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--distil', metavar='RUN_DIR', default=None)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    if args.distil:
        data, carried, facts = distil(args.distil)
        print('carried %d avoid entr%s: %s' % (len(carried),
                                               'y' if len(carried) == 1 else 'ies',
                                               ', '.join(carried) or '-'))
        print('carried %d verified fact(s)' % len(facts))
        if args.out:
            save(data, args.out)
            print('written to %s' % args.out)
        return 0
    data = load()
    print(format_for_prompt(data, max_entries=200))
    return 0


if __name__ == '__main__':
    sys.exit(main())
