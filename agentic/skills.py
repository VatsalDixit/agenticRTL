#!/usr/bin/env python3
"""
The skill library: what has worked, what has not, and why.

Each skill is a pattern (a bottleneck shape you can recognise in the profile
or the RTL) paired with a strategy (what to change), a confidence level and
simple counters. The loop reads the library into every candidate's brief and
updates it after every iteration from the group of candidates it just
measured (see learn.py). The file is plain JSON so a person can read and edit
it too.

    confidence   high    worked repeatedly, try it first
                 medium  worked sometimes, depends on the design state
                 low     unproven or risky
                 avoid   measured useless, absorbed by synthesis, or broke
                         correctness; do not spend a candidate on it

Usage:
    python agentic/skills.py            print the library
"""

import json
import os
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


def record_outcome(data, skill_ids, passed, adopted, advantage):
    """Bump the counters of every skill a candidate used."""
    for sid in skill_ids or []:
        sk = by_id(data, sid)
        if not sk:
            continue
        sk['tried'] += 1
        if passed:
            sk['passed'] += 1
        if adopted:
            sk['adopted'] += 1
        if advantage is not None:
            sk['advantages'] = (sk['advantages'] + [round(advantage, 3)])[-20:]


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
        conf = str(up.get('confidence') or 'low').lower().strip()
        if conf not in ORDER:
            conf = 'low'
        pattern = str(up.get('pattern') or '').strip()
        strategy = str(up.get('strategy') or '').strip()
        note = str(up.get('note') or '').strip()
        sk = by_id(data, sid)
        if sk is None:
            if not pattern or not strategy:
                continue
            sk = {'id': sid, 'pattern': pattern[:600], 'strategy': strategy[:900],
                  'confidence': conf, 'tried': 0, 'passed': 0, 'adopted': 0,
                  'advantages': [], 'notes': [],
                  'source': 'learned at iteration %d' % iteration}
            data['skills'].append(sk)
            changed.append('new: ' + sid)
        else:
            if conf != sk['confidence']:
                changed.append('%s: %s -> %s' % (sid, sk['confidence'], conf))
                sk['confidence'] = conf
            if strategy and len(strategy) > len(sk['strategy']) + 40:
                sk['strategy'] = strategy[:900]
        if note:
            sk['notes'] = (sk['notes'] + ['i%d: %s' % (iteration, note[:300])])[-6:]
    return changed


def mean_advantage(sk):
    adv = sk.get('advantages') or []
    return sum(adv) / len(adv) if adv else None


def format_for_prompt(data, max_entries=40):
    """The library as text for a brief, best first, avoid last."""
    lines = []
    # A cap per group, so a growing library never pushes the AVOID entries
    # (the ones that stop a session repeating a known failure) off the end.
    per_group = max(4, max_entries // 3)
    for conf in ORDER:
        group = [s for s in data['skills'] if s['confidence'] == conf]
        group.sort(key=lambda s: (-(mean_advantage(s) or 0), -s['adopted']))
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
                adv = mean_advantage(sk)
                stats = ' [tried %d, correct %d, adopted %d%s]' % (
                    sk['tried'], sk['passed'], sk['adopted'],
                    ', mean advantage %+.2f' % adv if adv is not None else '')
            lines.append('- %s%s' % (sk['id'], stats))
            lines.append('    pattern:  %s' % sk['pattern'])
            lines.append('    strategy: %s' % sk['strategy'])
            for note in sk['notes'][-2:]:
                lines.append('    note: %s' % note)
        lines.append('')
    return '\n'.join(lines).strip()


def main():
    data = load()
    print(format_for_prompt(data, max_entries=200))
    return 0


if __name__ == '__main__':
    sys.exit(main())
