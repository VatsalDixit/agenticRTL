#!/usr/bin/env python3
"""
Checks for the parts of the kit that decide what a session is told.

These are the failures that cost real money in earlier campaigns: a notebook
that demoted a skill when the learner only wanted to add a note, a strategy
rewritten into its own opposite, an analyser that pooled two element kinds and
so never saw a saturated stage. None of this needs a model or a simulator, so
it runs in a second.

    python agentic/test_kit.py
"""

import json
import os
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import analyse                                       # noqa: E402
import guide as guide_mod                            # noqa: E402
import measure                                       # noqa: E402
import skills as skills_mod                          # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    if condition:
        print('ok    %s' % name)
    else:
        FAILED.append(name)
        print('FAIL  %s%s' % (name, ('  -- ' + detail) if detail else ''))


def seed():
    return skills_mod.load(os.path.join(KIT, 'skills.json'))


# ---------------------------------------------------------------------------
# the notebook

def test_note_only_keeps_the_rating():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['confidence']
    skills_mod.apply_updates(data, [{'id': sk['id'], 'note': 'lost once here'}], 3)
    check('a note-only update does not change the rating',
          sk['confidence'] == before, '%s -> %s' % (before, sk['confidence']))


def test_unknown_confidence_keeps_the_rating():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['confidence']
    skills_mod.apply_updates(data, [{'id': sk['id'], 'confidence': 'unchanged',
                                     'note': 'still fine'}], 3)
    check('an unreadable rating is not treated as "low"',
          sk['confidence'] == before, '%s -> %s' % (before, sk['confidence']))


def test_named_rating_still_works():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    skills_mod.apply_updates(data, [{'id': sk['id'], 'confidence': 'medium',
                                     'note': 'measured worse twice'}], 3)
    check('a named rating still moves one step', sk['confidence'] == 'medium',
          sk['confidence'])


def test_strategy_is_not_overwritten():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['strategy']
    skills_mod.apply_updates(data, [{
        'id': sk['id'],
        'strategy': 'Be very cautious about widening the line, it cost area '
                    'for nothing the one time it was tried on this design.'}], 3)
    check('advice is kept and the rewrite becomes a caveat',
          sk['strategy'] == before and len(sk['caveats']) == 1)


def test_strategy_replaced_only_with_evidence():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['strategy']
    skills_mod.apply_updates(data, [{'id': sk['id'], 'replace_strategy': True,
                                     'strategy': 'x' * 200}], 3)
    kept = sk['strategy'] == before
    sk['adopted'] = 1
    skills_mod.apply_updates(data, [{'id': sk['id'], 'replace_strategy': True,
                                     'strategy': 'y' * 200}], 4)
    check('a rewrite needs an adoption behind it',
          kept and sk['strategy'].startswith('y') and sk.get('strategy_history'))


def test_rules_are_never_counted():
    data = seed()
    rule = skills_mod.by_id(data, 'chunk-boundary-flush-invariant')
    check('the invariants are rules', rule.get('kind') == 'rule')
    skills_mod.record_outcome(data, [rule['id']], passed=True, adopted=False,
                              advantage=-1.0)
    check('a rule gets no counters from a candidate that cited it',
          rule['tried'] == 0 and not rule['advantages'])


def test_two_candidate_advantage_is_dropped():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    skills_mod.record_outcome(data, [sk['id']], passed=True, adopted=False,
                              advantage=-1.0, count_advantage=False)
    check('a two-candidate coin flip is not recorded as evidence',
          sk['tried'] == 1 and not sk['advantages'])


def test_notes_are_clean():
    data = seed()
    long_note = ('i7: i7: c1 lost again. ' + 'The mechanism deepens the '
                 'decoder recurrence and costs more clock than the extra '
                 'element buys back. ' * 4)
    skills_mod.apply_updates(data, [{'id': 'raise-bytes-per-command',
                                     'note': long_note}], 7)
    note = skills_mod.by_id(data, 'raise-bytes-per-command')['notes'][-1]
    check('a note carries one iteration number and ends at a sentence',
          note.startswith('i7: c1 lost') and note.rstrip().endswith('.'), note[:80])


def test_render_has_rules_and_no_coin_flips():
    data = seed()
    text = skills_mod.format_for_prompt(data, max_entries=30, notes_per_skill=1)
    check('the rules block is rendered once, before the mechanisms',
          text.count('RULES (these always apply') == 1
          and text.index('RULES') < text.index('HIGH confidence'))
    check('mean advantage is gone from the brief',
          'mean advantage' not in text)
    check('the library is still small', len(text) // 4 < 4000,
          '%d tokens' % (len(text) // 4))


# ---------------------------------------------------------------------------
# the analyser

def test_copy_and_literal_lanes():
    state_path = os.path.join(ROOT, '.agentic', 'runs', 'campaign2', 'state.json')
    rtl = os.path.join(ROOT, '.agentic', 'runs', 'campaign2', 'base', 'rtl')
    if not (os.path.exists(state_path) and os.path.isdir(rtl)):
        print('skip  copy/literal replay (campaign2 records not present)')
        return
    widths = analyse.rtl_widths(rtl)
    with open(state_path, encoding='utf-8') as fil:
        best = json.load(fil)['best']['metrics']
    reports, lineitem = [], None
    for draw in best['draws']:
        counters = draw.get('counters')
        cs_tv = os.path.join(ROOT, '.agentic', 'corpus', draw['name'], 'cs.tv')
        if not counters or not os.path.exists(cs_tv):
            continue
        rep = analyse.analyse(counters, cs_tv, widths)
        if draw['scored']:
            reports.append(rep)
        if draw['name'] == 'train-lineitem':
            lineitem = rep
    if not reports:
        print('skip  copy/literal replay (no counters saved)')
        return
    lever = analyse.lever(analyse.combine(reports), best.get('wns_ns'))
    check('the saturated copy lane is what the lever names',
          lever['lever'] == 'width' and 'copy slot' in lever['reason'], str(lever))
    check('the visible draw still reads as it did',
          lineitem and abs(lineitem['bytes_per_cycle'] - 18.898) < 0.01)


# ---------------------------------------------------------------------------
# the design guide

def test_guide():
    rtl = os.path.join(ROOT, '.agentic', 'runs', 'campaign2', 'base', 'rtl')
    if not os.path.isdir(rtl):
        rtl = os.path.join(ROOT, 'rtl')
    if not os.path.isdir(rtl):
        print('skip  guide (no rtl/ to describe)')
        return
    text = guide_mod.build_guide(rtl)
    files = [f for f in os.listdir(rtl) if f.endswith('.vhd')]
    check('every RTL file is in the guide',
          all(('rtl/' + f) in text for f in files), '%d files' % len(files))
    check('the guide is small enough to paste into every brief',
          len(text) // 4 < 6000, '%d tokens' % (len(text) // 4))
    check('it gives no advice', 'should' not in text.lower().split('## files')[0])
    # every line number must land on the block it names
    bad = []
    for name in files:
        info = guide_mod.file_map(rtl, name)
        lines = guide_mod.read(os.path.join(rtl, name)).splitlines()
        for kind, label, start, _end in info['blocks']:
            if label not in lines[start - 1]:
                bad.append('%s:%d is not %s' % (name, start, label))
    check('every line number points at the block it names', not bad, '; '.join(bad[:3]))


# ---------------------------------------------------------------------------
# the timing report

def test_critical_path_naming(tmp=None):
    tmp = tmp or os.path.join(ROOT, '.agentic', 'ktest')
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, 'dffs.txt'), 'w', encoding='utf-8') as fil:
        fil.write('  cell \\DFF_X1 $auto$ff.cc:337:slice$36578\n'
                  '    connect \\CK \\clk\n'
                  '    connect \\D $flatten\\datapath_inst.\\x$3329\n'
                  '    connect \\Q \\datapath_inst.cmd_gen_2_inst.proc.c1h [0]\n'
                  '    connect \\QN $auto$dfflibmap.cc:539:dfflibmap$238014\n'
                  '  end\n'
                  '  cell \\DFF_X1 $auto$ff.cc:337:slice$11858\n'
                  '    connect \\CK \\clk\n'
                  '    connect \\D $auto$rtlil.cc:3510:MuxGate$219305\n'
                  '    connect \\Q \\datapath_inst.cmd_gen_2_inst.proc.li_off [5]\n'
                  '    connect \\QN $auto$dfflibmap.cc:539:dfflibmap$238916\n'
                  '  end\n')
    text = ('ABC: Start-point = pi19775 ($auto$dfflibmap.cc:539:dfflibmap$238014).'
            '  End-point = po18498 ($auto$rtlil.cc:3510:MuxGate$219305).')
    found = measure.critical_path(tmp, text)
    check('the worst path is named in RTL terms',
          found and found['from'].endswith('c1h [0]')
          and found['to'].endswith('li_off [5]'), str(found))


def main():
    for name, func in sorted(globals().items()):
        if name.startswith('test_') and callable(func):
            func()
    print()
    if FAILED:
        print('%d check(s) failed: %s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('all checks pass')
    return 0


if __name__ == '__main__':
    sys.exit(main())
