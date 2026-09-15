#!/usr/bin/env python3
"""
Group-relative skill learning (Dr. RTL section 4.2, adapted).

After an iteration the loop has N candidates that started from the same
parent design under the same profile. Their scores are compared WITHIN the
group (advantage = how much better than the group mean, in standard
deviations), which is a steadier signal than absolute numbers. A cheap model
call then reads the group and the skill library and returns skill updates:
confidence changes, notes, and new pattern/strategy pairs. skills.py merges
them and bumps the counters.
"""

import os
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

import propose                       # noqa: E402
import skills as skills_mod          # noqa: E402
from tools import CONFIG             # noqa: E402

LEARN_SYSTEM = """\
You are the skill-learning agent of an RTL optimisation loop for vhsnunzip, a
VHDL-2008 hardware Snappy decompressor. You read one iteration's group of
candidates (same parent design, same profile, measured the same way) and the
skill library, and you distil what this group taught into reusable
pattern -> strategy skills. Be strict: a skill is only 'high' after it has
worked more than once; a mechanism that broke correctness or measured a loss
is 'avoid' with the reason; do not invent skills the evidence does not
support. Reply with JSON only."""


def _fmt(v, unit='%'):
    if v is None:
        return 'n/a'
    return '%+.2f%s' % (v, unit) if unit == '%' else '%.3f%s' % (v, unit)


def group_text(group):
    lines = []
    for c in group:
        lines.append('- candidate %s (%s)' % (c['label'], c.get('id') or 'no proposal'))
        lines.append('    direction: %s' % (c.get('focus') or ''))
        if c.get('rationale'):
            lines.append('    rationale: %s' % c['rationale'][:500])
        lines.append('    outcome: %s' % c.get('outcome', ''))
        if c.get('problem'):
            lines.append('    problem: %s' % c['problem'][:300])
        m = c.get('measured') or {}
        if m:
            lines.append('    measured: throughput %s, bytes/cycle %s, f_max %s, area %s'
                         % (_fmt(m.get('throughput_gain_pct')),
                            _fmt(m.get('bpc_gain_pct')),
                            _fmt(m.get('fmax_gain_pct')),
                            _fmt(m.get('area_gain_pct'))))
        if c.get('expected_gain_pct') is not None:
            lines.append('    predicted gain: %+.1f%%' % c['expected_gain_pct'])
        if c.get('advantage') is not None:
            lines.append('    group advantage: %+.2f sd' % c['advantage'])
        if c.get('skills_used'):
            lines.append('    skills used: %s' % ', '.join(c['skills_used']))
        lines.append('    adopted: %s' % ('YES' if c.get('adopted') else 'no'))
        if c.get('files_changed'):
            lines.append('    files: %s' % ', '.join(c['files_changed'])[:300])
    return '\n'.join(lines)


def build_prompt(ctx, group, skills_text):
    return """\
GOAL: %s
ITERATION %d. Parent design: %s
PROFILE OF THE PARENT: %s

THIS ITERATION'S GROUP
%s

SKILL LIBRARY (ids you may update)
%s

Return JSON only:
{"skill_updates": [
   {"id": "existing-id-or-new-kebab-id",
    "pattern": "when to apply (required for a new skill)",
    "strategy": "what to do (required for a new skill)",
    "confidence": "high|medium|low|avoid",
    "note": "one sentence of evidence from this group"}
 ],
 "lessons": ["one or two short sentences a future candidate session should know"]}
Rules: update only skills the evidence touches (usually 1-4 entries). A new
skill needs a concrete pattern and strategy. Keep ids stable.
""" % (ctx['goal_text'], ctx['iteration'], ctx['state_text'].splitlines()[0]
       if ctx['state_text'] else '', ctx['lever']['reason'],
       group_text(group), skills_text)


def learn(ctx, group, skills_data, log):
    """Update the skill library from one iteration's group. Returns
    (changed_list, lessons_list, call_result)."""
    text = skills_mod.format_for_prompt(skills_data, max_entries=60)
    res = propose.ask(build_prompt(ctx, group, text), system=LEARN_SYSTEM,
                      model=CONFIG['helper_model'], max_turns=1, budget=2.0,
                      timeout_s=600, effort='medium')
    changed, lessons = [], []
    if res.get('status') == 'ok':
        data = propose.extract_json(res.get('text', ''))
        if isinstance(data, dict):
            updates = data.get('skill_updates') or []
            if isinstance(updates, dict):
                updates = [updates]
            try:
                changed = skills_mod.apply_updates(skills_data, updates,
                                                   ctx['iteration'])
            except Exception as exc:          # a malformed update must not end a run
                log('  skill update ignored: %s' % str(exc)[:200])
            raw = data.get('lessons') or []
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, list):
                lessons = [str(l)[:300] for l in raw if l][:4]
    else:
        log('  learning call failed (%s): %s'
            % (res.get('status'), (res.get('error') or '')[:200]))
    return changed, lessons, res
