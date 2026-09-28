#!/usr/bin/env python3
"""
The loop. Give it a goal; it improves the RTL by itself.

    python agentic/loop.py --goal "increase throughput by 50%" --iters 100
    python agentic/loop.py --goal "maximize throughput" --iters 20 --candidates 3
    python agentic/loop.py --resume --run <name>          continue a run
    python agentic/loop.py --status [--run <name>]        what is it doing
    python agentic/loop.py --goal "..." --dry-run         baseline + plan only

One iteration (Dr. RTL shaped):

    1. ANALYSE   the best design so far: bytes/cycle per stimulus draw, stage
                 rates vs ceilings read from the RTL, f_max, area, the lever.
    2. PLAN      one cheap model call picks N different directions.
    3. WRITE     N Claude coding sessions, in parallel, each in its own git
                 worktree, each writing one RTL change and a PROPOSAL.json.
                 They may run only `python agentic/check.py`.
    4. MEASURE   every candidate: simulate all draws (oracle on every byte),
                 then synthesise. Score = -gain% + 0.15 x area growth%.
    5. SELECT    the best adoptable candidate becomes the new best design
                 (the run branch moves to its commit). Losers keep branches.
    6. LEARN     the group is compared (advantage in standard deviations)
                 and the skill library is updated.
    7. RECORD    state.json, status.json, report.html, loop.log.

The user's own checkout is never touched. Everything happens in worktrees
under .agentic/, and the result is a branch: agentic/<run>.
"""

import argparse
import concurrent.futures
import datetime
import os
import re
import statistics
import subprocess
import sys
import time
import traceback

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

import analyse                         # noqa: E402
import freeze                          # noqa: E402
import guide as guide_mod              # noqa: E402
import learn as learn_mod              # noqa: E402
import measure                         # noqa: E402
import propose                         # noqa: E402
import report                          # noqa: E402
import skills as skills_mod            # noqa: E402
from tools import (CONFIG, ROOT, GitError, Logger, eda_available, git,  # noqa: E402
                   git_ok, now_iso, pct, read_json, rmtree, write_json,
                   area_of, area_unit, backend_of)
from tools import run as tools_run                                    # noqa: E402
from tools import kill_all as tools_kill_all                          # noqa: E402

# The scoring data (agentic/data, test_data) is not tracked by git, so a new
# worktree never contains it. These are removed only if someone committed
# them by hand. vectors/ (the upstream unit-test vectors from a built-in
# sample) stays: it is not the scoring data and deleting tracked files makes
# the worktree look damaged to the session working in it.
BLIND_DIRS = ('test_data', os.path.join('agentic', 'data'))
BLIND_SUFFIXES = ('.parquet',)
METRIC_KEYS = {'throughput': 'throughput_gbps', 'bytes_per_cycle': 'bytes_per_cycle',
               'fmax': 'f_max_mhz', 'area': 'area'}

# Extra fields recorded per candidate and per iteration so the dashboard can
# plot real numbers; `area_um2` stays so older readers still find it.
ABSOLUTE_KEYS = ('area', 'area_unit', 'area_um2', 'f_max_mhz', 'throughput_gbps',
                 'bytes_per_cycle', 'wns_ns', 'regs', 'luts', 'bram', 'uram')


# ---------------------------------------------------------------------------
# goal

def parse_goal(text):
    low = text.lower()
    target = None
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', low)
    if m:
        target = float(m.group(1))
    else:
        m = re.search(r'(\d+(?:\.\d+)?)\s*x\b', low)
        if m:
            target = (float(m.group(1)) - 1.0) * 100.0
        elif 'quadruple' in low:
            target = 300.0
        elif 'triple' in low:
            target = 200.0
        elif 'double' in low or 'twice' in low:
            target = 100.0
    metric = 'throughput'
    if 'bytes per cycle' in low or 'bytes/cycle' in low:
        metric = 'bytes_per_cycle'
    elif 'f_max' in low or 'fmax' in low or 'clock' in low or 'frequency' in low:
        metric = 'fmax'
    elif 'area' in low and 'throughput' not in low:
        metric = 'area'
    return {'text': text.strip(), 'metric': metric, 'target_pct': target}


def goal_gain(metrics, base, goal):
    """Improvement on the goal metric in percent (positive = better)."""
    if goal['metric'] == 'area':
        val = pct(area_of(metrics), area_of(base))
    else:
        key = METRIC_KEYS[goal['metric']]
        val = pct(metrics.get(key), base.get(key))
    if val is None:
        return None
    return -val if goal['metric'] == 'area' else val


# ---------------------------------------------------------------------------
# git worktrees

def remove_worktree(path):
    """Remove a worktree. Raises GitError if something still holds it open."""
    if not os.path.exists(path):
        git_ok(['worktree', 'prune'])
        return
    if not git_ok(['worktree', 'remove', '--force', path]):
        git_ok(['worktree', 'remove', '--force', '--force', path])
    if os.path.exists(path):
        rmtree(path)
    git_ok(['worktree', 'prune'])
    if os.path.exists(path):
        raise GitError('worktree %s is held open by another process; '
                       'close editors/terminals inside it' % path)


def add_worktree(path, branch, start, replace_branch=True):
    remove_worktree(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if git_ok(['rev-parse', '--verify', 'refs/heads/' + branch]):
        if not replace_branch:
            raise GitError('branch %s already exists' % branch)
        git(['branch', '-D', branch])
    git(['worktree', 'add', '-b', branch, path, start])
    return path


def ram_latency(rtl_dir):
    """The simulation RAM's command/response stage counts, as text."""
    path = os.path.join(rtl_dir, 'vhsnunzip_ram.sim.vhd')
    try:
        with open(path, encoding='utf-8', errors='replace') as fil:
            text = fil.read()
    except IOError:
        return ''
    found = re.findall(r'\b(CMD_STAGES|RESP_STAGES)\s*:\s*\w+\s*:=\s*(\d+)', text)
    return ' '.join('%s=%s' % kv for kv in sorted(found))


# ---------------------------------------------------------------------------
# a model-free stand-in for the coding sessions, for testing the loop itself

FAKE_EDITS = [
    ('comment-only', 'rtl/vhsnunzip_unbuffered.vhd',
     lambda text: '-- fake candidate: no functional change\n' + text,
     {'id': 'fake-comment-only', 'rationale': 'test: identical design',
      'expected_gain_pct': 0.0}),
    ('broken-cnt', 'rtl/vhsnunzip_unbuffered.vhd',
     lambda text: text.replace('de_cnt      => de_cnt,', 'de_cnt      => de_cnt,'),
     {'id': 'fake-no-edit', 'rationale': 'test: the session changed nothing',
      'expected_gain_pct': 0.0}),
]


def fake_write_candidates(assignments, log):
    """Apply scripted edits instead of running model sessions."""
    out = []
    for idx, asg in enumerate(assignments):
        name, rel, transform, proposal = FAKE_EDITS[idx % len(FAKE_EDITS)]
        path = os.path.join(asg['worktree'], rel)
        with open(path, encoding='utf-8', errors='replace') as fil:
            text = fil.read()
        # Each edit is unique to its candidate, so two scripted candidates
        # are never the same commit.
        with open(path, 'w', encoding='utf-8', newline='\n') as fil:
            fil.write(transform(text).replace(
                'no functional change', 'no functional change (%s)' % asg['branch'], 1))
        write_json(os.path.join(asg['worktree'], 'PROPOSAL.json'), proposal)
        with open(os.path.join(asg['worktree'], 'NOTES.md'), 'w',
                  encoding='utf-8', newline='\n') as fil:
            fil.write('## How it works\nfake session: nothing was read.\n'
                      '## What I tried\n%s\n## Why it worked or failed\n'
                      'scripted edit, not measured for meaning.\n'
                      '## Facts\n- fake sessions write notes so this path is tested\n'
                      % name)
        log('    %s | fake edit %s' % (asg['label'], name))
        out.append({'label': asg['label'], 'direction': asg['direction'],
                    'session': {'status': 'ok', 'cost_usd': 0.0, 'turns': 0,
                                'seconds': 0.0, 'error': '', 'subtype': 'fake'},
                    'proposal': propose.read_proposal(asg['worktree'])})
    return out


RETRY_MIN_GAIN_PCT = 5.0
RETRY_PER_ITERATION = 2


def retry_candidates(run, state, k, iter_dir, log):
    """Re-merge earlier rejected candidates with a large gain onto the current
    best and offer them as extra candidates.

    A candidate can be rejected for a rule that later changes (area pricing),
    or lose only because a sibling scored better that iteration. If its
    branch still merges cleanly onto the best design, measuring it again
    costs half a minute. Each old candidate is retried once.
    """
    retried = state.setdefault('retried', [])
    pool = []
    for it in state['iterations']:
        for c in it.get('candidates', []):
            m = c.get('measured') or {}
            if c.get('outcome') in ('too_expensive', 'candidate') and c.get('branch') \
                    and (m.get('gain_pct') or 0) >= RETRY_MIN_GAIN_PCT \
                    and c['branch'] not in retried \
                    and git_ok(['rev-parse', '--verify', 'refs/heads/' + c['branch']]):
                pool.append(c)
    pool.sort(key=lambda c: -(c['measured'].get('gain_pct') or 0))
    out = []
    for j, old in enumerate(pool[:RETRY_PER_ITERATION], 1):
        retried.append(old['branch'])
        label = 'r%d' % j
        branch = 'agentic-cand/%s/i%d-%s' % (state['run'], k, label)
        path = os.path.join(iter_dir, label)
        try:
            add_worktree(path, branch, state['best']['commit'])
            merged = git_ok(['-c', 'user.name=agentic-loop', '-c', 'user.email=agentic@localhost',
                             'merge', '--no-edit', '-m', 'agentic i%d %s: retry %s'
                             % (k, label, old.get('id')), old['branch']], cwd=path)
            if not merged:
                git_ok(['merge', '--abort'], cwd=path)
                log('  %s: retry of %s does not merge onto the current best; skipped'
                    % (label, old.get('id')))
                remove_worktree(path)
                git_ok(['branch', '-D', branch])
                continue
            sha = git(['rev-parse', 'HEAD'], cwd=path)
            if sha == state['best']['commit']:
                remove_worktree(path)
                git_ok(['branch', '-D', branch])
                continue
            files = git(['diff', '--name-only', 'HEAD^1', 'HEAD'], cwd=path).splitlines()
        except GitError as exc:
            log('  %s: retry of %s failed: %s' % (label, old.get('id'), exc))
            continue
        log('  %s: retrying %s (measured %+.2f%% at iteration %d) merged onto the current best'
            % (label, old.get('id'), old['measured'].get('gain_pct') or 0,
               it_of(state, old)))
        out.append(({'label': label, 'branch': branch,
                     'direction': {'focus': 'retry of %s: %s' % (old.get('id'), old['direction'].get('focus', '')),
                                   'hypothesis': 'rejected or outscored earlier with %+.2f%% gain; '
                                                 'may pass under the current rule or on the current best'
                                                 % (old['measured'].get('gain_pct') or 0),
                                   'skill_ids': old['direction'].get('skill_ids') or []},
                     'id': 'retry-' + str(old.get('id')), 'rationale': old.get('rationale', ''),
                     'expected_gain_pct': old['measured'].get('gain_pct'),
                     'expected_effect': old.get('expected_effect', ''), 'risk': old.get('risk', ''),
                     'skills_used': old.get('skills_used') or [],
                     'session': {'status': 'retry', 'cost_usd': 0.0, 'turns': 0, 'seconds': 0.0,
                                 'error': '', 'subtype': 'retry'},
                     'commit': sha, 'files_changed': files, 'outcome': '', 'reason': '',
                     'measured': {}, 'score': None, 'advantage': None, 'adopted': False},
                    {'label': label, 'worktree': path, 'direction': {}, 'branch': branch}))
    return out


def it_of(state, cand):
    for it in state['iterations']:
        if cand in it.get('candidates', []):
            return it['iteration']
    return 0


def blind(path):
    """Remove the scoring stimulus from a candidate worktree."""
    for rel in BLIND_DIRS:
        rmtree(os.path.join(path, rel))
    for dirpath, dirnames, filenames in os.walk(path):
        if '.git' in dirnames:
            dirnames.remove('.git')
        for name in filenames:
            if name.endswith(BLIND_SUFFIXES):
                try:
                    os.remove(os.path.join(dirpath, name))
                except OSError:
                    pass


def commit_candidate(path, message):
    """Commit rtl/ changes in a worktree. Returns (sha, files) or (None, [])."""
    git(['add', '-A', 'rtl'], cwd=path)
    if git_ok(['diff', '--cached', '--quiet'], cwd=path):
        return None, []
    git(['-c', 'user.name=agentic-loop', '-c', 'user.email=agentic@localhost',
         'commit', '-q', '-m', message], cwd=path)
    sha = git(['rev-parse', 'HEAD'], cwd=path)
    files = git(['diff', '--name-only', 'HEAD~1', 'HEAD'], cwd=path).splitlines()
    return sha, files


# ---------------------------------------------------------------------------
# scoring

def evaluate(metrics, parent, goal):
    """Score one measured candidate against the parent design."""
    out = {'measured': {}, 'score': None, 'adoptable': False, 'outcome': '',
           'reason': ''}
    if metrics.get('error'):
        out['outcome'] = 'failed_compile'
        out['reason'] = metrics['error']
        return out
    if not metrics.get('oracle_pass'):
        out['outcome'] = 'failed_correctness'
        out['reason'] = metrics.get('first_problem') or 'oracle failed'
        return out
    meas = {
        'throughput_gain_pct': pct(metrics.get('throughput_gbps'), parent.get('throughput_gbps')),
        'bpc_gain_pct': pct(metrics.get('bytes_per_cycle'), parent.get('bytes_per_cycle')),
        'fmax_gain_pct': pct(metrics.get('f_max_mhz'), parent.get('f_max_mhz')),
        'area_gain_pct': pct(area_of(metrics), area_of(parent)),
        'gain_pct': goal_gain(metrics, parent, goal),
    }
    meas = {k: (round(v, 3) if v is not None else None) for k, v in meas.items()}
    out['measured'] = meas
    if metrics.get('synth_error') or meas['gain_pct'] is None:
        out['outcome'] = 'failed_synth'
        out['reason'] = metrics.get('synth_error') or 'no synthesis numbers'
        return out
    if backend_of(metrics) != backend_of(parent):
        # LUTs against square micrometres, an FPGA clock against an ASIC one:
        # any percentage taken across the two would be a number about the
        # instruments, not the design.
        out['measured'] = {}
        out['outcome'] = 'failed_synth'
        out['reason'] = ('measured with %s synthesis but the design it is compared '
                         'with was measured with %s' % (backend_of(metrics),
                                                         backend_of(parent)))
        return out
    gain = meas['gain_pct']
    area = meas['area_gain_pct'] or 0.0
    # Area is priced, not capped: a throughput goal on this design is reached
    # by widening, and widening costs silicon. The one hard rule is an
    # efficiency floor (gain per percent of area) so that a small gain cannot
    # buy a lot of area. An earlier version added a heavy penalty above 10%
    # area growth; it rejected a +28.9% throughput widening at +91% area in
    # favour of +3% at +10%, which is the wrong trade for a throughput goal.
    score = -gain + float(CONFIG['area_weight']) * max(area, 0.0)
    out['score'] = round(score, 4)
    if gain < float(CONFIG['min_gain_pct']):
        out['outcome'] = 'no_gain' if gain > -float(CONFIG['min_gain_pct']) else 'regressed'
        out['reason'] = 'goal metric %+.2f%% (needs at least +%.2f%%)' % (
            gain, float(CONFIG['min_gain_pct']))
        return out
    # Place-and-route moves f_max by about a megahertz on any edit, so a small
    # clock-carried gain is indistinguishable from re-placement. Bytes/cycle
    # comes from simulation and has no such noise: a gain it carries counts.
    noise = float(CONFIG['pnr_noise_pct'])
    if (backend_of(metrics) != 'yosys' and goal['metric'] in ('throughput', 'fmax')
            and gain < noise
            and not (goal['metric'] == 'throughput'
                     and (meas['bpc_gain_pct'] or 0) >= float(CONFIG['min_gain_pct']))):
        out['outcome'] = 'no_gain'
        out['reason'] = ('goal metric %+.2f%% comes from f_max alone and is inside '
                         'the %.1f%% place-and-route noise' % (gain, noise))
        return out
    ei = gain / area if area > 0 else float('inf')
    meas['efficiency'] = round(ei, 3) if ei != float('inf') else None
    if area > float(CONFIG['max_area_growth_pct']) and ei < float(CONFIG['ei_floor']):
        out['outcome'] = 'too_expensive'
        out['reason'] = ('area +%.1f%% for %+.2f%% gain: efficiency %.2f is under the '
                         'floor of %.2f' % (area, gain, ei, float(CONFIG['ei_floor'])))
        return out
    out['outcome'] = 'candidate'
    out['adoptable'] = True
    return out


def advantages(cands):
    """Group-relative advantage: positive = better than the group."""
    scored = [c for c in cands if c.get('score') is not None]
    if len(scored) < 2:
        for c in scored:
            c['advantage'] = 0.0
        return
    vals = [c['score'] for c in scored]
    mean = statistics.mean(vals)
    sd = statistics.pstdev(vals) or 1.0
    for c in scored:
        c['advantage'] = round(-(c['score'] - mean) / sd, 3)


# ---------------------------------------------------------------------------
# context for the model calls

def state_text(metrics, base):
    w = metrics.get('widths') or {}
    lines = ['bytes/cycle %.3f on real Parquet data (geomean), f_max %.1f MHz, area %.0f %s, '
             'throughput %.3f GB/s, worst slack %+.3f ns at a %d ps clock target'
             % (metrics.get('bytes_per_cycle') or 0, metrics.get('f_max_mhz') or 0,
                area_of(metrics) or 0, area_unit(metrics) or 'um2',
                metrics.get('throughput_gbps') or 0,
                metrics.get('wns_ns') or 0, int(CONFIG['clock_period_ps']))]
    if metrics.get('luts') is not None:
        lines.append('measured by Vivado place-and-route on %s: %d LUTs, %d registers, '
                     '%s BRAM tiles, %s URAM'
                     % (metrics.get('part'), metrics['luts'], metrics.get('regs') or 0,
                        metrics.get('bram', 'n/a'), metrics.get('uram', 'n/a')))
    lines.append('ports: co_data %d bytes (cnt %d bits), de_data %d bytes (cnt %d bits); '
                 'cores %d; core line %d bytes; registers %s'
                 % (w.get('in_bytes', 0), w.get('in_cnt_bits', 0), w.get('out_bytes', 0),
                    w.get('out_cnt_bits', 0), w.get('cores', 1),
                    int(w.get('core_line_bytes', 0)), metrics.get('regs', 'n/a')))
    worst = metrics.get('critical_path')
    if worst and metrics.get('failing_endpoints') is not None:
        lines.append('worst timing path from place-and-route: %s -> %s '
                     '(%d of %d timing endpoints miss the clock target)'
                     % (worst.get('from'), worst.get('to'),
                        metrics['failing_endpoints'], metrics.get('total_endpoints') or 0))
    elif worst:
        lines.append('worst timing path from synthesis: %s -> %s '
                     '(the clock is what this path costs; everything else has slack)'
                     % (worst.get('from'), worst.get('to')))
    for rec in metrics.get('draws', []):
        if rec.get('visible') and rec.get('oracle_pass'):
            lines.append('  draw %-16s %7.3f bytes/cycle, output idle %5.1f%%, input stalled %5.1f%%  (%s)'
                         % (rec['name'], rec['bytes_per_cycle'], rec.get('output_idle_pct', 0),
                            rec.get('input_stall_pct', 0),
                            'real Parquet pages' if rec['kind'] == 'real' else 'synthetic chunks, not scored'))
    hidden = [r['bytes_per_cycle'] for r in metrics.get('draws', [])
              if not r.get('visible') and r.get('oracle_pass')]
    if hidden:
        lines.append('  plus %d hidden real-data draws (other tables) at %.3f to %.3f bytes/cycle'
                     % (len(hidden), min(hidden), max(hidden)))
    return '\n'.join(lines)


def profile_text(metrics):
    parts = []
    for rec in metrics.get('draws', []):
        if rec.get('visible') and rec.get('kind') == 'real' and rec.get('analysis'):
            parts.append(analyse.describe(rec['analysis'], rec['name']))
    # The visible draw is the fastest table and is literal-heavy, so on its own
    # it hides which stage binds the score. The busiest held-out table's rates
    # are shown without its name: the numbers are not the stimulus.
    hidden = [r for r in metrics.get('draws', [])
              if not r.get('visible') and r.get('oracle_pass') and r.get('analysis')]
    if hidden:
        worst = max(hidden, key=lambda r: r['analysis'].get('binding_utilisation') or 0)
        parts.append(analyse.describe(worst['analysis'],
                                      'the busiest held-out table (name withheld)'))
    prof = metrics.get('profile') or {}
    if prof:
        parts.append('across all real draws: binding stage %s (tightest %s at %.0f%%), '
                     'output idle %.1f%% on average'
                     % (prof.get('binding'), prof.get('tightest'),
                        100.0 * (prof.get('tightest_utilisation') or 0),
                        prof.get('mean_output_idle_pct') or 0))
    return '\n'.join(parts) or '(no profile)'


def history_text(state):
    lines = []
    # Lessons older than four iterations are dropped here: by then the learner
    # has attached what mattered to a skill, and keeping both copies spent a
    # fifth of the brief on the same evidence twice.
    fresh = {it['iteration'] for it in state['iterations'][-4:]}
    for it in state['iterations'][-12:]:
        for c in it.get('candidates', []):
            m = c.get('measured') or {}
            bits = ['i%d %s' % (it['iteration'], c.get('id') or 'no proposal'),
                    c.get('outcome', '')]
            if m.get('throughput_gain_pct') is not None:
                bits.append('throughput %+.2f%%, bytes/cycle %+.2f%%, f_max %+.2f%%, area %+.2f%%'
                            % (m['throughput_gain_pct'], m.get('bpc_gain_pct') or 0,
                               m.get('fmax_gain_pct') or 0, m.get('area_gain_pct') or 0))
            if c.get('expected_gain_pct') is not None:
                bits.append('predicted %+.1f%%' % c['expected_gain_pct'])
            if c.get('adopted'):
                bits.append('ADOPTED')
            line = '- ' + ': '.join(bits[:2]) + ('; ' + '; '.join(bits[2:]) if len(bits) > 2 else '')
            if c.get('reason') and c.get('outcome') not in ('adopted', 'candidate'):
                line += ' -- ' + c['reason'][:160]
            # A rejected attempt with a real gain is worth rebuilding; the
            # session can read its files with `git show <branch>:<path>`.
            if c.get('outcome') in ('too_expensive', 'regressed') and c.get('branch') \
                    and (m.get('bpc_gain_pct') or 0) >= 5.0:
                line += (' [its files are on git branch %s: read them with '
                         '`git show %s:rtl/<file>`]' % (c['branch'], c['branch']))
            lines.append(line)
        if it['iteration'] in fresh:
            for les in it.get('lessons', []):
                lines.append('  lesson: ' + les)
    return '\n'.join(lines)


def progress_text(state):
    p = state.get('progress') or {}
    if not p:
        return 'baseline (nothing adopted yet)'
    return ('throughput %+.2f%% (%.3f GB/s), bytes/cycle %+.2f%%, f_max %+.2f%%, area %+.2f%%'
            % (p.get('throughput_gain_pct') or 0, (state['best']['metrics'].get('throughput_gbps') or 0),
               p.get('bpc_gain_pct') or 0, p.get('fmax_gain_pct') or 0, p.get('area_gain_pct') or 0))


def build_ctx(state, skills_data, iteration, guide_text=''):
    best = state['best']['metrics']
    return {
        'guide_text': guide_text,
        'goal_text': state['goal_text'],
        'iteration': iteration,
        'max_iters': state['max_iters'],
        'progress_text': progress_text(state),
        'state_text': state_text(best, state['baseline']),
        'profile_text': profile_text(best),
        'lever': best.get('lever') or {'lever': 'unknown', 'reason': 'no profile'},
        'skills_text': skills_mod.format_for_prompt(skills_data, max_entries=30,
                                                    notes_per_skill=1),
        'facts_text': '\n'.join('- %s' % f for f in (state.get('design_facts') or [])),
        'history_text': history_text(state),
    }


# ---------------------------------------------------------------------------
# the run

class Run(object):
    def __init__(self, name):
        self.name = name
        self.dir = report.run_dir(name)
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, 'state.json')
        self.skills_path = os.path.join(self.dir, 'skills.json')
        self.guide_path = os.path.join(self.dir, 'guide.md')
        self.log = Logger(os.path.join(self.dir, 'loop.log'))
        self.status = report.Status(os.path.join(self.dir, 'status.json'))
        self.base_dir = os.path.join(self.dir, 'base')
        self.state = read_json(self.state_path, None)
        self.limit_message = ''
        self.check_text = ''

    def save(self):
        self.state['updated'] = now_iso()
        write_json(self.state_path, self.state)
        report.write_report(self.state, os.path.join(self.dir, 'report.html'))

    def guide_text(self):
        """The design guide for the best design so far, or ''."""
        try:
            with open(self.guide_path, encoding='utf-8') as fil:
                return fil.read()
        except IOError:
            return ''

    def write_guide(self, notes=None, facts=None):
        """Rebuild the guide from the design the base worktree now holds.

        Called once after the baseline and again after every adoption, so a
        session never reads a guide describing a design that no longer exists.
        """
        try:
            text = guide_mod.write_guide(
                self.guide_path, os.path.join(self.base_dir, 'rtl'),
                widths=((self.state or {}).get('best') or {}).get('metrics', {}).get('widths'),
                notes=notes, facts=facts)
            self.log('design guide: %d characters (about %d tokens) in %s'
                     % (len(text), len(text) // 4, os.path.basename(self.guide_path)))
        except Exception as exc:              # a missing guide is never fatal
            self.log('could not write the design guide: %s' % str(exc)[:200])

    def set_progress(self):
        base = self.state['baseline']
        best = self.state['best']['metrics']
        self.state['progress'] = {
            'throughput_gain_pct': pct(best.get('throughput_gbps'), base.get('throughput_gbps')),
            'bpc_gain_pct': pct(best.get('bytes_per_cycle'), base.get('bytes_per_cycle')),
            'fmax_gain_pct': pct(best.get('f_max_mhz'), base.get('f_max_mhz')),
            'area_gain_pct': pct(area_of(best), area_of(base)),
        }
        self.status.set(best=self.state['progress'])


def preflight(log, need_model=True):
    problems = []
    if ' ' in ROOT:
        problems.append('the repository path contains a space (%s); the EDA shell '
                        'cannot handle that. Move or clone it to a path without spaces.' % ROOT)
    if not git_ok(['rev-parse', '--git-dir']):
        problems.append('%s is not a git repository' % ROOT)
    ok, why = eda_available()
    if not ok:
        problems.append(why)
    else:
        log('EDA tools: %s' % why)
    if CONFIG['synth_backend'] == 'hacc':
        ok, why = measure.hacc.available()
        if not ok:
            problems.append('synth_backend is hacc but the host is not usable: %s' % why)
        else:
            log('synthesis: %s' % why)
    elif not os.path.exists(measure.LIB_FILE):
        problems.append('missing liberty file %s (run bash agentic/syn/get_lib.sh)' % measure.LIB_FILE)
    fz = freeze.check()
    if fz:
        problems.extend(fz)
    if need_model:
        ok, why = propose.availability()
        if not ok:
            problems.append(why)
    return problems


def call_record(res):
    """What one model call cost, for the run record."""
    res = res or {}
    return {k: res.get(k) for k in ('status', 'cost_usd', 'seconds', 'turns',
                                    'usage', 'error', 'text')}


def log_session(log, label, sess):
    log('  %s: session %s, %d turns, $%.2f, %.0f s%s' % (
        label, sess.get('status'), sess.get('turns') or 0,
        sess.get('cost_usd') or 0, sess.get('seconds') or 0,
        (' (' + (sess.get('error') or '')[:100] + ')') if sess.get('error') else ''))
    if sess.get('first_edit_s'):
        reads = (sess.get('tool_counts') or {}).get('Read', 0)
        log('       spent %.1f min and %d read(s) before its first edit'
            % (sess['first_edit_s'] / 60.0, reads))
    use = sess.get('usage') or {}
    if use.get('output_tokens'):
        thinking = (use.get('output_tokens_details') or {}).get('thinking_tokens')
        log('       tokens: %s written%s, %s cache write, %s cache read'
            % (use.get('output_tokens', 0),
               (' (%s thinking)' % thinking) if thinking else '',
               use.get('cache_creation_input_tokens', 0),
               use.get('cache_read_input_tokens', 0)))


def sum_usage(cands):
    """Token totals over one iteration's sessions."""
    total = {}
    for cand in cands:
        use = (cand.get('session') or {}).get('usage') or {}
        for key, val in use.items():
            if isinstance(val, int):
                total[key] = total.get(key, 0) + val
    return total


def latest_window(results):
    """The most recent usage-window reading any session saw."""
    seen = [(r.get('session') or {}).get('rate_limit') for r in results]
    seen = [w for w in seen if w]
    if not seen:
        return None
    return max(seen, key=lambda w: w.get('seen_at') or 0)


def window_text(win):
    bits = [str(win.get('status'))]
    if win.get('utilization') is not None:
        bits.append('%.0f%% used' % (100.0 * win['utilization']))
    if win.get('resets_at'):
        bits.append('resets in %.0f min' % ((win['resets_at'] - time.time()) / 60.0))
    if win.get('window'):
        bits.append(str(win['window']))
    return ', '.join(bits)


def kit_files():
    return [os.path.join(KIT, f) for f in sorted(os.listdir(KIT))
            if f.endswith('.py') or f == 'config.json']


def kit_mtime():
    try:
        return max(os.path.getmtime(f) for f in kit_files())
    except (OSError, ValueError):
        return 0.0


KIT_MTIME = kit_mtime()


def kit_compiles(log):
    """Never restart into a half-saved edit."""
    import py_compile
    for path in kit_files():
        if not path.endswith('.py'):
            continue
        try:
            py_compile.compile(path, doraise=True)
        except Exception as exc:
            log('  the kit does not compile (%s); keeping the running code'
                % str(exc)[:200])
            return False
    return True


def restart(name, log):
    """Re-run this loop with the kit as it is on disk now.

    os.execv is not usable here: on Windows it re-quotes the program path and
    an interpreter installed under a user folder whose name contains a
    space comes back split at that space.
    Running the new process as a child and exiting with its code
    keeps the console and the exit status intact.
    """
    gen = int(os.environ.get('AGENTIC_RELOAD_GEN', '0')) + 1
    if gen > 20:
        log('  the kit keeps changing; staying on the running code')
        return False
    env = dict(os.environ, AGENTIC_RELOAD_GEN=str(gen))
    cmd = [sys.executable, os.path.abspath(__file__)] + restart_argv(name)
    log('restarting to pick up the kit change (restart %d of at most 20)' % gen)
    sys.stdout.flush()
    raise SystemExit(subprocess.call(cmd, env=env))


def restart_argv(name):
    """This process's arguments, forced to resume the current run."""
    argv, skip = [], False
    for arg in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if arg in ('--run', '--goal'):
            skip = True
            continue
        if arg == '--resume' or arg.startswith('--run=') or arg.startswith('--goal='):
            continue
        argv.append(arg)
    return argv + ['--resume', '--run', name]


def starting_check(base_dir, log):
    """Run the candidates' check once, on the design they all start from.

    Every session used to spend its first turn on this, and the answer is the
    same for all of them. The worktree's own copy of check.py is used, so it
    reports on the design in that worktree.
    """
    script = os.path.join(base_dir, 'agentic', 'check.py')
    if not os.path.exists(script):
        return ''
    res = tools_run([sys.executable, script], timeout=900, cwd=base_dir)
    text = (res.out or '') + (res.err or '')
    if not res.ok:
        log('  the starting check did not pass here (%s); sessions will run it '
            'themselves' % (text.strip().splitlines() or [''])[-1][:160])
        return ''
    keep = [ln for ln in text.splitlines()
            if ln.strip() and not ln.startswith('This is invented stimulus')]
    return '\n'.join(keep[:40])


def measure_one(args):
    rtl_dir, work_dir, draws = args
    try:
        return measure.measure(rtl_dir, work_dir, draws, synth=True)
    except Exception as exc:                # never let one candidate kill the run
        return {'oracle_pass': False, 'error': 'measurement crashed: %s' % exc,
                'draws': []}


# ---------------------------------------------------------------------------
# the steps of an iteration, shared by both schedules

def new_assignment(state, k, label, direction, start, iter_dir):
    # Not under the run branch's name: git cannot have both a branch
    # 'agentic/run' and a branch 'agentic/run/i1-c1'.
    branch = 'agentic-cand/%s/i%d-%s' % (state['run'], k, label)
    path = os.path.join(iter_dir, label)
    add_worktree(path, branch, start)
    blind(path)
    return {'label': label, 'worktree': path, 'direction': direction,
            'branch': branch, 'start': start}


def candidate_from_session(asg, res, k, iter_dir, log):
    """Commit what one session produced and describe it as a candidate."""
    sess = res['session']
    prop = res['proposal'] or {}
    cand = {'label': asg['label'], 'branch': asg['branch'],
            'direction': asg['direction'], 'id': prop.get('id'),
            'rationale': prop.get('rationale', ''),
            'expected_gain_pct': prop.get('expected_gain_pct'),
            'expected_effect': prop.get('expected_effect', ''),
            'risk': prop.get('risk', ''), 'skills_used': prop.get('skills_used') or [],
            'model': CONFIG['model'], 'effort': CONFIG.get('effort'),
            'primary_skill': prop.get('primary_skill'),
            'truncated': sess.get('status') in ('budget', 'timeout'),
            'session': sess, 'commit': None, 'files_changed': [],
            'outcome': '', 'reason': '', 'measured': {}, 'score': None,
            'advantage': None, 'adopted': False}
    try:
        sha, files = commit_candidate(asg['worktree'], 'agentic i%d %s: %s'
                                      % (k, asg['label'], prop.get('id') or 'no proposal'))
    except GitError as exc:
        sha, files = None, []
        log('  %s: could not commit: %s' % (asg['label'], exc))
    cand['commit'] = sha
    cand['files_changed'] = files
    notes = propose.read_notes(asg['worktree'])
    cand['notes'] = notes
    if notes:
        try:
            with open(os.path.join(iter_dir, '%s-NOTES.md' % asg['label']),
                      'w', encoding='utf-8', newline='\n') as fil:
                fil.write(notes)
        except OSError as exc:
            log('  %s: could not save notes: %s' % (asg['label'], exc))
    if not sha or (prop.get('id') in (None, 'none')):
        cand['outcome'] = 'no_proposal'
        cand['reason'] = (prop.get('rationale') or sess.get('error') or
                          'the session made no change to rtl/')[:300]
        log('  %s: no proposal (%s)' % (asg['label'], cand['reason'][:120]))
    else:
        log('  %s: proposal %s touching %s; predicted %s%%' % (
            asg['label'], cand['id'], ', '.join(files)[:120],
            cand['expected_gain_pct']))
    return cand


def keep_facts(state, cands):
    """Facts a session verified are true of the design whatever is tried
    next, so they are kept for every later session (and for the guide)."""
    facts = state.setdefault('design_facts', [])
    for cand in cands:
        for line in propose.fact_lines(cand.get('notes') or ''):
            if line not in facts:
                facts.append(line)
    del facts[:-24]


def score_candidate(c, asg, metrics, parent, goal, parent_ram, measure_dir, log):
    """Evaluate one measured candidate against its parent, in place."""
    ev = evaluate(metrics, parent, goal)
    c.update({'measured': ev['measured'], 'score': ev['score'],
              'outcome': ev['outcome'], 'reason': ev['reason'],
              # Kept in the saved record (unlike 'metrics', which is
              # stripped) so the dashboard can plot a candidate's real
              # numbers instead of rebuilding them from percentages.
              'absolute': {kk: metrics.get(kk) for kk in ABSOLUTE_KEYS},
              'measure_seconds': {'sim': metrics.get('sim_seconds'),
                                  'synth': metrics.get('synth_seconds')},
              'metrics': {kk: vv for kk, vv in metrics.items() if kk != 'draws'},
              'draws': [{kk: vv for kk, vv in r.items() if kk not in ('analysis', 'counters')}
                        for r in metrics.get('draws', [])]})
    c['adoptable'] = ev['adoptable']
    mine_ram = ram_latency(os.path.join(asg['worktree'], 'rtl'))
    if c['adoptable'] and mine_ram != parent_ram:
        c['adoptable'] = False
        c['outcome'] = 'ram_latency_changed'
        c['reason'] = ('the simulation-only RAM latency changed (%s -> %s); '
                       'synthesis uses a fixed memory, so this gain would '
                       'not be real' % (parent_ram, mine_ram))
    m = ev['measured']
    if m:
        log('  %s %s: %s  throughput %+.2f%% (bytes/cycle %+.2f%%, f_max %+.2f%%), area %+.2f%%, score %s'
            % (c['label'], c['id'], c['outcome'], m.get('throughput_gain_pct') or 0,
               m.get('bpc_gain_pct') or 0, m.get('fmax_gain_pct') or 0,
               m.get('area_gain_pct') or 0, ev['score']))
    else:
        log('  %s %s: %s -- %s' % (c['label'], c['id'], c['outcome'], ev['reason'][:200]))
    rmtree(os.path.join(measure_dir, 'sim'))
    for junk in ('vhsnunzip_unbuffered', 'work-obj08.cf', 'netlist.v'):
        try:
            os.remove(os.path.join(measure_dir, 'synth', junk))
        except OSError:
            pass


def adopt(run, winner, metrics, k, log):
    """Move the run to a winner: its branch, its numbers, its guide."""
    state = run.state
    run.status.set(phase='adopt', detail=winner['id'])
    if not git_ok(['merge', '--ff-only', winner['commit']], cwd=run.base_dir):
        git(['reset', '--hard', winner['commit']], cwd=run.base_dir)
    winner['adopted'] = True
    winner['outcome'] = 'adopted'
    # The best design keeps the FULL measurement (per-draw stage analyses
    # included) because the next brief is built from it; the iteration
    # record keeps the stripped copy.
    state['best'] = {'commit': winner['commit'], 'metrics': dict(metrics),
                     'iteration': k, 'id': winner['id']}
    run.set_progress()
    log('ADOPTED %s: %s' % (winner['id'], progress_text(state)))
    # Kept so a later refresh of the guide (new facts, same design) still
    # explains how the design it describes works.
    state['guide_notes'] = propose.notes_section(winner.get('notes') or '', 'how it works')
    run.write_guide(notes=state['guide_notes'], facts=state.get('design_facts'))
    run.check_text = ''            # the design changed; the check must too


def record_outcomes(skills_data, cands):
    """Move the skill counters for the candidates that were measured.

    A session that timed out or hit a limit says nothing about a skill, and
    neither does one cut off mid-change by its budget: what got measured then
    is not the mechanism it set out to build.
    """
    for c in cands:
        if c['commit'] and c['outcome'] != 'no_proposal' and not c.get('truncated'):
            primary = (c.get('primary_skill')
                       or (c['direction'].get('skill_ids') or [None])[0])
            if primary:
                skills_mod.record_outcome(
                    skills_data, [primary],
                    passed=c['outcome'] in ('candidate', 'adopted', 'no_gain',
                                            'regressed', 'too_expensive',
                                            'ram_latency_changed'),
                    adopted=c['adopted'], advantage=c['advantage'],
                    count_advantage=len([x for x in cands
                                         if x.get('score') is not None]) >= 3)


def learn_step(ctx, cands, skills_data, iter_dir, log, fake=False):
    """The model call that turns an iteration's outcomes into skill edits."""
    group = []
    for c in cands:
        group.append({'label': c['label'], 'id': c['id'], 'focus': c['direction'].get('focus'),
                      'rationale': c['rationale'], 'outcome': c['outcome'],
                      'problem': c['reason'], 'measured': c['measured'],
                      'expected_gain_pct': c['expected_gain_pct'],
                      'advantage': c['advantage'], 'adopted': c['adopted'],
                      'skills_used': c['skills_used'], 'files_changed': c['files_changed'],
                      'primary_skill': c.get('primary_skill'),
                      'truncated': c.get('truncated'), 'turns': (c['session'] or {}).get('turns'),
                      'facts': propose.fact_lines(c.get('notes') or '')})
    changed, lessons = [], []
    if not fake:
        try:
            changed, lessons, _res = learn_mod.learn(ctx, group, skills_data, log)
            write_json(os.path.join(iter_dir, 'learn.json'), call_record(_res))
        except Exception as exc:              # learning is optional; never fatal
            log('  learning step failed: %s' % str(exc)[:200])
    for ch in changed:
        log('  skill %s' % ch)
    for les in lessons:
        log('  lesson: %s' % les)
    return changed, lessons


def make_record(state, k, directions, cands, winner, changed, lessons, timing):
    record = {'iteration': k, 'at': now_iso(), 'directions': directions,
              'candidates': [{kk: (vv[:1200] if kk == 'notes' else vv)
                              for kk, vv in c.items() if kk != 'metrics'}
                             for c in cands],
              'winner': ({'id': winner['id'], 'label': winner['label'],
                          'commit': winner['commit'], 'measured': winner['measured']}
                         if winner else None),
              'best_after': {kk: state['best']['metrics'].get(kk) for kk in
                             ('throughput_gbps', 'bytes_per_cycle', 'f_max_mhz', 'area_um2',
                              'area', 'area_unit')},
              'skills_changed': changed, 'lessons': lessons,
              'model': CONFIG['model'], 'effort': CONFIG.get('effort'),
              'tokens': sum_usage(cands),
              # What those tokens price out at, as a cross-check on the cost
              # the CLI reports and as the tokens-to-dollars mapping for
              # comparing one model's iteration with another's.
              'tokens_cost_estimate': round(
                  propose.estimate_cost(sum_usage(cands), CONFIG['model']), 2),
              'cost_usd': round(sum((c['session'].get('cost_usd') or 0) for c in cands), 2),
              'timing': timing}
    return record


def append_record(run, record, log):
    state = run.state
    if record['tokens'].get('output_tokens'):
        log('iteration tokens: %s written, %s cache write, %s cache read '
            '(about $%.2f at %s rates; the provider billed $%.2f)'
            % (record['tokens'].get('output_tokens', 0),
               record['tokens'].get('cache_creation_input_tokens', 0),
               record['tokens'].get('cache_read_input_tokens', 0),
               record['tokens_cost_estimate'], CONFIG['model'], record['cost_usd']))
    log('time i%d: %s' % (record['iteration'], timing_text(record['timing'])))
    state['iterations'].append(record)
    state['cost_usd'] = round((state.get('cost_usd') or 0) + record['cost_usd'], 2)
    run.save()


TIMING_ORDER = (('plan_s', 'plan'), ('check_s', 'check'), ('write_s', 'write'),
                ('measure_s', 'measure'), ('sim_s', 'of which simulation'),
                ('synth_s', 'of which synthesis'), ('learn_s', 'learn'))


def timing_text(timing):
    def fmt(sec):
        return '%.0f s' % sec if sec < 120 else '%.1f min' % (sec / 60.0)
    bits = ['%s %s' % (name, fmt(timing[key])) for key, name in TIMING_ORDER
            if timing.get(key) is not None]
    return ', '.join(bits) or 'nothing timed'


def run_iteration(run, draws, skills_data, k, n_cands, dry_run=False, fake=False):
    state = run.state
    log = run.log
    timing = {}
    # Sessions the previous attempt at this iteration left half-finished when
    # the usage window ran out. Their worktrees were kept.
    pending = state.get('pending') or {}
    resuming = pending.get('slots') if pending.get('iteration') == k else None
    if resuming:
        resuming = [slot for slot in resuming if os.path.isdir(slot.get('worktree', ''))]
    state['pending'] = None
    goal = state['goal']
    parent = state['best']['metrics']
    run.status.set(phase='plan', iteration=k, detail='choosing %d directions' % n_cands)
    log('== iteration %d of %d ==' % (k, state['max_iters']))
    log('best so far: %s' % progress_text(state))
    log('lever: %s -- %s' % (parent.get('lever', {}).get('lever'),
                             parent.get('lever', {}).get('reason')))

    ctx = build_ctx(state, skills_data, k, guide_text=run.guide_text())
    ctx['check_text'] = run.check_text
    if resuming:
        directions = [slot['direction'] for slot in resuming]
        plan_res = {'status': 'reused', 'text': 'the plan from the interrupted attempt'}
    if not resuming:
        t0 = time.time()
        directions, plan_res = propose.plan_directions(ctx, n_cands, log)
        timing['plan_s'] = round(time.time() - t0, 1)
    for j, d in enumerate(directions, 1):
        log('  direction c%d: %s' % (j, d['focus'][:140]))
    if dry_run:
        log('dry run: stopping before any coding session')
        return None

    iter_dir = os.path.join(run.dir, 'iter-%d' % k)
    os.makedirs(iter_dir, exist_ok=True)
    write_json(os.path.join(iter_dir, 'plan.json'),
               {'directions': directions, 'call': call_record(plan_res)})
    start = git(['rev-parse', 'HEAD'], cwd=run.base_dir)
    assignments = []
    if resuming:
        log('continuing %d session(s) in the worktrees the usage limit '
            'interrupted' % len(resuming))
        assignments = [dict(slot, resumed=True) for slot in resuming]
        directions = [slot['direction'] for slot in assignments]
    else:
        try:
            for j, d in enumerate(directions, 1):
                assignments.append(new_assignment(state, k, 'c%d' % j, d, start, iter_dir))
        except GitError as exc:
            log('could not prepare a candidate worktree: %s' % exc)
            return 'error'

    def discard_all(spent_on=None):
        spent = round(sum((r['session'].get('cost_usd') or 0)
                          for r in (spent_on or [])), 2)
        if spent:
            state['discarded_usd'] = round((state.get('discarded_usd') or 0) + spent, 2)
            log('  discarding %d session(s) that produced nothing: $%.2f spent, '
                '$%.2f discarded so far this run'
                % (len(spent_on), spent, state['discarded_usd']))
        for asg in assignments:
            try:
                remove_worktree(asg['worktree'])
            except GitError as exc:
                log('  (%s)' % exc)
            git_ok(['branch', '-D', asg['branch']])

    if not run.check_text:
        run.status.set(phase='check', detail='running the starting check once')
        t0 = time.time()
        run.check_text = starting_check(run.base_dir, log)
        timing['check_s'] = round(time.time() - t0, 1)
        # Into this iteration's briefs too, not only the next one's.
        ctx['check_text'] = run.check_text
        if run.check_text:
            log('starting check passes; its output goes into every brief')
    run.status.set(phase='write', detail='%d coding sessions in parallel' % n_cands)
    log('writing %d candidates in parallel (model %s, up to %d min each)...'
        % (n_cands, CONFIG['model'], int(CONFIG['session_timeout_min'])))
    t0 = time.time()
    if fake:
        results = fake_write_candidates(assignments, log)
    else:
        results = propose.write_candidates(
            ctx, assignments, log, rate=propose.burn_rate(state, CONFIG['model']))
    timing['write_s'] = round(time.time() - t0, 1)
    log('sessions finished in %.0f min' % (timing['write_s'] / 60.0))
    for asg, res in zip(assignments, results):
        log_session(log, asg['label'], res['session'])
    window = latest_window(results)
    if window:
        state['window'] = window
        log('  usage window: %s' % window_text(window))

    statuses = [r['session'].get('status') for r in results]
    no_proposal = not any(r['proposal'] and r['proposal'].get('id') not in (None, 'none')
                          for r in results)
    if no_proposal and any(s == 'limit' for s in statuses):
        # The provider is refusing; nothing was produced. Do not spend an
        # iteration on it: wait for the reset and try this one again.
        msgs = ' '.join((r['session'].get('error') or '') for r in results)
        run.limit_message = msgs
        spent = round(sum((r['session'].get('cost_usd') or 0) for r in results), 2)
        state['discarded_usd'] = round((state.get('discarded_usd') or 0) + spent, 2)
        state['pending'] = {'iteration': k, 'slots': [
            {'label': a['label'], 'worktree': a['worktree'], 'branch': a['branch'],
             'direction': a['direction'], 'start': a.get('start')} for a in assignments]}
        log('  keeping %d worktree(s) so the sessions can be continued after the '
            'wait ($%.2f spent so far on this attempt)' % (len(assignments), spent))
        return 'limit'
    if no_proposal and all(s not in ('ok', 'budget') for s in statuses):
        errs = '; '.join((r['session'].get('error') or '')[:120] for r in results)
        log('every session failed: %s' % errs)
        discard_all(results)
        # 1073807364 is DBG_TERMINATE_PROCESS: something outside killed the
        # whole tree (a closed terminal, a reboot, Ctrl-C on the console).
        # Retrying that twelve times helps nobody.
        if re.search(r'exit code (?:1073807364|3221225786|3221225794)', errs):
            return 'killed'
        return 'error'

    cands = [candidate_from_session(asg, res, k, iter_dir, log)
             for asg, res in zip(assignments, results)]
    facts_before = list(state.get('design_facts') or [])
    keep_facts(state, cands)

    # earlier rejected candidates with a big gain, re-merged onto the best
    if not fake:
        for cand, asg in retry_candidates(run, state, k, iter_dir, log):
            cands.append(cand)
            assignments.append(asg)

    # measure the ones that changed something
    to_measure = [(c, asg) for c, asg in zip(cands, assignments) if c['commit']
                  and c['outcome'] != 'no_proposal']
    raw = {}
    run.status.set(phase='measure', detail='%d candidates: simulate + synthesise' % len(to_measure))
    if to_measure:
        log('measuring %d candidate(s)...' % len(to_measure))
        jobs = [(os.path.join(asg['worktree'], 'rtl'),
                 os.path.join(iter_dir, c['label'] + '-measure'), draws)
                for c, asg in to_measure]
        t0 = time.time()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs))
        try:
            metrics_list = list(pool.map(measure_one, jobs))
        except KeyboardInterrupt:
            tools_kill_all()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        timing['measure_s'] = round(time.time() - t0, 1)
        # The slowest candidate's, which is what the parallel round waited for.
        for key, src in (('sim_s', 'sim_seconds'), ('synth_s', 'synth_seconds')):
            vals = [m.get(src) for m in metrics_list if m.get(src) is not None]
            if vals:
                timing[key] = max(vals)
        parent_ram = ram_latency(os.path.join(run.base_dir, 'rtl'))
        for (c, asg), metrics in zip(to_measure, metrics_list):
            raw[c['label']] = metrics
            score_candidate(c, asg, metrics, parent, goal, parent_ram,
                            os.path.join(iter_dir, c['label'] + '-measure'), log)
    advantages(cands)

    # select
    winner = None
    adoptable = [c for c in cands if c.get('adoptable')]
    if adoptable:
        winner = min(adoptable, key=lambda c: c['score'])
        adopt(run, winner, raw[winner['label']], k, log)
    else:
        log('nothing adopted this iteration')
        # The coding sessions read the facts only through the guide, which
        # adopt() rebuilds. Without this, facts verified in an iteration that
        # adopted nothing reached the planner but not the next sessions --
        # and most iterations adopt nothing.
        if state.get('design_facts') != facts_before:
            run.write_guide(notes=state.get('guide_notes'), facts=state.get('design_facts'))

    # learn
    run.status.set(phase='learn', detail='updating the skill library')
    record_outcomes(skills_data, cands)
    t0 = time.time()
    changed, lessons = learn_step(ctx, cands, skills_data, iter_dir, log, fake)
    if not fake:
        timing['learn_s'] = round(time.time() - t0, 1)
    skills_mod.save(skills_data, run.skills_path)

    append_record(run, make_record(state, k, directions, cands, winner,
                                   changed, lessons, timing), log)
    for asg in assignments:
        remove_worktree(asg['worktree'])
    return 'adopted' if winner else 'none'


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--goal', default=None, help='e.g. "increase throughput by 50%%"')
    ap.add_argument('--run', default=None, help='run name (default: run-<date>)')
    ap.add_argument('--iters', type=int, default=None,
                    help='iteration cap (default 100; on --resume keeps the run\'s cap)')
    ap.add_argument('--candidates', type=int, default=None)
    ap.add_argument('--skills-from', dest='skills_from', default=None,
                    help='start the skill library from a finished run: its '
                         'verified facts and its avoid entries, counters reset')
    ap.add_argument('--model', default=None)
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--fake', action='store_true',
                    help='test the loop with scripted edits instead of model sessions')
    ap.add_argument('--patience', type=int, default=0,
                    help='stop after this many iterations without a winner (0 = never)')
    ap.add_argument('--max-hours', type=float, default=0.0)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args()

    if args.status:
        name = args.run or report.latest_run()
        if not name:
            print('no runs yet')
            return 1
        report.print_status(name)
        return 0

    if args.model:
        CONFIG['model'] = args.model
    n_cands = args.candidates or int(CONFIG['candidates'])

    if args.resume:
        name = args.run or report.latest_run()
        if not name or not os.path.exists(os.path.join(report.run_dir(name), 'state.json')):
            print('nothing to resume')
            return 1
    else:
        if not args.goal:
            print('give a goal, e.g. --goal "increase throughput by 50%"')
            return 2
        name = args.run or datetime.datetime.now().strftime('run-%Y%m%d-%H%M')

    run = Run(name)
    log = run.log
    log('agentic loop starting: run %s in %s' % (name, ROOT))
    problems = preflight(log, need_model=not args.dry_run)
    if problems:
        for p in problems:
            log('PROBLEM: %s' % p)
        run.status.set(phase='failed', detail=problems[0][:200], finished=now_iso())
        return 1

    fresh = run.state is None
    if fresh:
        goal = parse_goal(args.goal)
        base_commit = git(['rev-parse', 'HEAD'])
        branch = 'agentic/' + name
        if git_ok(['rev-parse', '--verify', 'refs/heads/' + branch]):
            log('PROBLEM: branch %s already exists (an earlier run with this name). '
                'Pick another --run name, or --resume it.' % branch)
            run.status.set(phase='failed', detail='branch exists', finished=now_iso())
            return 1
        run.state = {
            'run': name, 'goal_text': goal['text'], 'goal': goal,
            'max_iters': args.iters or 100, 'candidates': n_cands,
            'synth_backend': CONFIG['synth_backend'],
            'started': now_iso(), 'base_commit': base_commit,
            'branch': branch, 'baseline': None,
            'best': None, 'iterations': [], 'stopped': None, 'cost_usd': 0.0,
            'model': CONFIG['model'], 'seed': args.seed,
        }
        log('goal: %s  (metric %s, target %s)' % (
            goal['text'], goal['metric'],
            '%+.0f%%' % goal['target_pct'] if goal['target_pct'] else 'as far as possible'))
    else:
        measured_by = backend_of(run.state.get('baseline'))
        if run.state.get('baseline') and measured_by != CONFIG['synth_backend']:
            # Every candidate from here would be scored against a baseline in
            # another unit on another target. The kit also restarts itself
            # into a resume whenever config.json changes, so this is exactly
            # where a mid-run backend switch would otherwise slip through.
            log('PROBLEM: run %s was measured with %s synthesis and the config now '
                'says %s. Resume it with AGENTIC_SYNTH_BACKEND=%s, or start a new run.'
                % (name, measured_by, CONFIG['synth_backend'], measured_by))
            run.status.set(phase='failed', detail='synthesis backend changed',
                           finished=now_iso())
            return 1
        run.state['stopped'] = None
        if args.goal:
            log('note: --goal ignored on resume; the run keeps its goal')
        if args.iters is not None:
            run.state['max_iters'] = args.iters
        log('resuming at iteration %d, best so far: %s'
            % (len(run.state['iterations']) + 1, progress_text(run.state)))

    state = run.state
    draws = None
    skills_data = None
    try:
        if fresh:
            add_worktree(run.base_dir, state['branch'], state['base_commit'])
            run.save()
        else:
            if not os.path.exists(os.path.join(run.base_dir, '.git')):
                remove_worktree(run.base_dir)
                git(['worktree', 'add', run.base_dir, state['branch']])
            if state.get('best') and state['best'].get('commit'):
                head = git(['rev-parse', 'HEAD'], cwd=run.base_dir)
                if head != state['best']['commit']:
                    log('base worktree was at %s, moving it back to the recorded best %s'
                        % (head[:12], state['best']['commit'][:12]))
                    git(['reset', '--hard', state['best']['commit']], cwd=run.base_dir)

        run.status.set(phase='corpus', iteration=len(state['iterations']),
                       detail='building stimulus')
        draws = measure.prepare_corpus(seed=state.get('seed', 0))
        log('corpus: %d draws (%d scored real, %d synthetic)' % (
            len(draws), sum(1 for d, _p, _c in draws if d.scored),
            sum(1 for d, _p, _c in draws if d.kind == 'synthetic')))

        if os.path.exists(run.skills_path):
            skills_data = skills_mod.load(run.skills_path)
        elif args.skills_from:
            prev = report.run_dir(args.skills_from)
            skills_data, carried, facts = skills_mod.distil(prev)
            log('skill library distilled from %s: %d avoid entr%s and %d '
                'verified fact(s) carried, everything else reset'
                % (args.skills_from, len(carried),
                   'y' if len(carried) == 1 else 'ies', len(facts)))
            skills_mod.save(skills_data, run.skills_path)
        else:
            skills_data = skills_mod.load()
            skills_mod.save(skills_data, run.skills_path)
        if not state.get('design_facts'):
            state['design_facts'] = list(skills_data.get('design_facts') or [])

        if not state.get('baseline'):
            run.status.set(phase='baseline', detail='measuring the starting design')
            log('measuring the baseline design...')
            base = measure.measure(os.path.join(run.base_dir, 'rtl'),
                                   os.path.join(run.dir, 'baseline-measure'), draws)
            if base.get('error') or not base.get('oracle_pass'):
                log('the starting design does not pass: %s'
                    % (base.get('error') or base.get('first_problem')))
                state['stopped'] = 'baseline design fails the oracle'
                run.save()
                run.status.set(phase='failed', detail=state['stopped'], finished=now_iso())
                return 1
            if base.get('synth_error'):
                log('baseline synthesis failed: %s' % base['synth_error'])
                state['stopped'] = 'baseline synthesis failed'
                run.save()
                run.status.set(phase='failed', detail=state['stopped'], finished=now_iso())
                return 1
            state['baseline'] = base
            state['best'] = {'commit': state['base_commit'], 'metrics': base,
                             'iteration': 0, 'id': 'baseline'}
            run.set_progress()
            run.save()
            run.write_guide(facts=state.get('design_facts'))
            log('baseline: %s' % state_text(base, base).splitlines()[0])
            rmtree(os.path.join(run.dir, 'baseline-measure', 'sim'))

        target = state['goal'].get('target_pct')
        since_winner = 0
        limit_waits = 0
        error_waits = 0
        t_start = time.time()
        k = len(state['iterations']) + 1
        while k <= state['max_iters']:
            gain = goal_gain(state['best']['metrics'], state['baseline'], state['goal'])
            if target and gain is not None and gain >= target:
                state['stopped'] = 'target met: %+.2f%% on %s' % (gain, state['goal']['metric'])
                break
            if args.patience and since_winner >= args.patience:
                state['stopped'] = '%d iterations without a winner' % since_winner
                break
            if args.max_hours and (time.time() - t_start) > args.max_hours * 3600:
                state['stopped'] = 'time limit of %.1f h reached' % args.max_hours
                break
            # Re-read the skill library each iteration so a person can edit
            # it while the run is going.
            if os.path.exists(run.skills_path):
                try:
                    skills_data = skills_mod.load(run.skills_path)
                except Exception as exc:
                    log('  could not reload skills.json (%s); keeping the copy in memory' % exc)
            win = state.get('window') or {}
            left = (win.get('resets_at') or 0) - time.time()
            if (win.get('utilization') or 0) >= 0.9 and 0 < left < 25 * 60 \
                    and not args.dry_run and not args.fake:
                log('the usage window is %.0f%% gone and resets in %d min; '
                    'waiting rather than starting sessions that would be cut off'
                    % (100.0 * win['utilization'], left // 60))
                run.status.set(phase='waiting', detail='window nearly spent')
                t_wait = time.time()
                time.sleep(max(0, (win.get('resets_at') or 0) - time.time() + 60))
                state['wait_s'] = round((state.get('wait_s') or 0) + time.time() - t_wait, 1)
                continue
            if kit_mtime() > KIT_MTIME + 0.5 and not args.dry_run:
                if kit_compiles(log):
                    log('the kit changed on disk; restarting to pick it up '
                        '(resuming run %s at iteration %d)' % (name, k))
                    run.status.set(phase='reloading', detail='kit changed on disk')
                    run.save()
                    if not restart(name, log):
                        globals()['KIT_MTIME'] = kit_mtime()
                else:
                    globals()['KIT_MTIME'] = kit_mtime()
            outcome = run_iteration(run, draws, skills_data, k, n_cands,
                                    dry_run=args.dry_run, fake=args.fake)
            if args.dry_run:
                state['stopped'] = 'dry run'
                break
            if outcome == 'limit':
                limit_waits += 1
                if limit_waits > 24:
                    state['stopped'] = 'the model provider kept refusing for a day'
                    break
                win = state.get('window') or {}
                wait = None
                if win.get('resets_at'):
                    wait = int(win['resets_at'] - time.time())
                    if wait > 0:
                        log('  the provider reports the window resets in %d min'
                            % (wait // 60))
                    else:
                        wait = None
                if wait is None:
                    wait = propose.reset_wait_seconds(run.limit_message)
                if wait is None:
                    wait = int(CONFIG['limit_wait_min']) * 60
                wait = min(max(wait + 120, 300), 6 * 3600)
                log('the model provider reports a usage limit; waiting %d min (wait %d)'
                    % (wait // 60, limit_waits))
                t_wait = time.time()
                run.status.set(phase='waiting',
                               detail='usage limit; retrying in %d min' % (wait // 60))
                time.sleep(wait)
                state['wait_s'] = round((state.get('wait_s') or 0) + time.time() - t_wait, 1)
                continue
            if outcome == 'killed':
                state['stopped'] = ('the sessions were killed from outside '
                                    '(not a provider error); stopping instead '
                                    'of retrying')
                break
            if outcome == 'error':
                error_waits += 1
                if error_waits > 12:
                    state['stopped'] = 'every coding session failed twelve times in a row'
                    break
                log('every session failed; waiting 5 min then retrying (attempt %d)'
                    % error_waits)
                run.status.set(phase='waiting', detail='sessions failed; retrying in 5 min')
                time.sleep(300)
                continue
            limit_waits = error_waits = 0
            since_winner = 0 if outcome == 'adopted' else since_winner + 1
            k += 1
        else:
            state['stopped'] = 'iteration cap of %d reached' % state['max_iters']
    except KeyboardInterrupt:
        tools_kill_all()
        state['stopped'] = 'interrupted'
        log('interrupted; state saved, resume with --resume --run %s' % name)
    except Exception:
        state['stopped'] = 'crashed: ' + traceback.format_exc().strip().splitlines()[-1]
        log(traceback.format_exc())

    try:
        run.save()
    except Exception as exc:
        log('could not save state: %s' % exc)
    run.status.set(phase='finished', detail=state.get('stopped') or '', finished=now_iso())
    if args.dry_run and fresh:
        try:
            remove_worktree(run.base_dir)
        except GitError:
            pass
        git_ok(['branch', '-D', state['branch']])
    log('stopped: %s' % state.get('stopped'))
    if state.get('best'):
        log('best design: branch %s at %s (%s)' % (state['branch'], state['best']['commit'][:12],
                                                     progress_text(state)))
    log('total model cost this run: $%.2f%s' % (
        state.get('cost_usd') or 0,
        (' (plus $%.2f spent on sessions that were discarded)'
         % state['discarded_usd']) if state.get('discarded_usd') else ''))
    totals = timing_totals(state)
    if totals:
        log('time spent, summed over iterations: %s' % timing_text(totals))
        if state.get('wait_s'):
            log('time spent waiting for the usage window: %.1f min' % (state['wait_s'] / 60.0))
    log('report: %s' % os.path.join(run.dir, 'report.html'))
    return 0


def timing_totals(state):
    """Each step's time summed over the run's iterations."""
    totals = {}
    for it in state.get('iterations', []):
        for key, val in (it.get('timing') or {}).items():
            if isinstance(val, (int, float)):
                totals[key] = round(totals.get(key, 0.0) + val, 1)
    return totals


if __name__ == '__main__':
    sys.exit(main())
