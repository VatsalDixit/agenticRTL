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
                 then synthesise. Score = -gain% on the goal metric. Area
                 is measured and reported, not scored.
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
import copy
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
import hacc                            # noqa: E402
import learn as learn_mod              # noqa: E402
import measure                         # noqa: E402
import probe as probe_mod              # noqa: E402
import propose                         # noqa: E402
import report                          # noqa: E402
import skills as skills_mod            # noqa: E402
from tools import (CONFIG, ROOT, GitError, Logger, eda_available, git,  # noqa: E402
                   git_ok, now_iso, pct, read_json, rmtree, write_json,
                   area_of, area_unit, backend_of, geomean)
from tools import run as tools_run                                    # noqa: E402
from tools import kill_all as tools_kill_all                          # noqa: E402
packmodel = None

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


RAM_SIM_FILE = 'vhsnunzip_ram.sim.vhd'


def _vhdl_normal(text):
    """VHDL as tokens, comments dropped, lower case (VHDL ignores case), one
    space between tokens: an edit to comments, case or whitespace, inside a
    line too ('a := b' to 'a:=b'), is no change to the RAM."""
    text = re.sub(r'--[^\n]*', '', text or '').lower()
    return ' '.join(re.findall(r'"[^"\n]*"|[a-z0-9_]+|[^\sa-z0-9_]', text))


def ram_signature(text):
    """The simulation RAM's command/response stage counts, as text ('' when
    there is no such file): the veto that refuses a candidate, exactly as
    before v3. Synthesis swaps in a fixed memory, so a gain from fewer RAM
    stages would be scored and never built."""
    if not text:
        return ''
    found = re.findall(r'\b(CMD_STAGES|RESP_STAGES)\s*:\s*\w+\s*:=\s*(\d+)', text)
    return ' '.join('%s=%s' % kv for kv in sorted(found))


def ram_model(text):
    """A hash of the whole simulation RAM model and of its entity's port
    list, comments and whitespace ignored, e.g. 'model=... ports=...'. Only
    logged, never scored: the brief lets no rule but the area gate change
    what is adopted, so an edit to the model beyond its stage counts is
    shown for a person to judge rather than refused. '' with no file."""
    if not text:
        return ''
    norm = _vhdl_normal(text)
    ports = re.search(r'entity vhsnunzip_ram is (.*?)\bend\b', norm)
    return 'model=%s ports=%s' % (_sha(norm)[:12], _sha(ports.group(1) if ports else '')[:12])


def ram_model_of(rtl_dir):
    try:
        with open(os.path.join(rtl_dir, RAM_SIM_FILE), encoding='utf-8',
                  errors='replace') as fil:
            return ram_model(fil.read())
    except IOError:
        return ''


def ram_latency(rtl_dir):
    """ram_signature of the simulation RAM in one rtl/ folder."""
    try:
        with open(os.path.join(rtl_dir, RAM_SIM_FILE), encoding='utf-8',
                  errors='replace') as fil:
            return ram_signature(fil.read())
    except IOError:
        return ''


def ram_latency_at(commit, cwd=None):
    """ram_signature of the simulation RAM as committed at ``commit``."""
    try:
        return ram_signature(git(['show', '%s:rtl/%s' % (commit, RAM_SIM_FILE)], cwd=cwd))
    except GitError:
        return ''


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


FAKE_SPEC = ('# Scripted track spec (--fake)\n\nNothing here was designed. A fake '
             'design session writes this so the loop\'s track path is exercised: '
             'the design is judged without measurement, its steps replace the '
             'planner\'s, and each build step is a comment-only edit.\n\n'
             + ('Filler so the spec reaches the minimum length the loop accepts. ' * 30))


def fake_track_session(asg, log):
    """A scripted track session: the design step writes a spec and a step
    list (one golden step, then a throughput step predicting 5% over the
    base); a build step makes a comment-only edit to rtl/."""
    tr = asg['direction'].get('track') or {}
    tid = tr.get('id') or asg.get('track_id')
    if asg.get('kind') == 'design':
        docs = os.path.join(asg['worktree'], 'docs', 'track-%s' % tid)
        os.makedirs(docs, exist_ok=True)
        with open(os.path.join(docs, 'SPEC.md'), 'w', encoding='utf-8', newline='\n') as fil:
            fil.write(FAKE_SPEC)
        base = float(tr.get('base_bpc') or 1.0)
        write_json(os.path.join(docs, 'steps.json'), [
            {'text': 'golden model, as a unit test', 'kind': 'golden', 'predicted_bpc': None},
            {'text': 'the scripted widening', 'kind': 'throughput',
             'widths': {'K': 4, 'L': 32, 'N': 32, 'rules': 'ideal'},
             'predicted_bpc': round(base * 1.05, 3)}])
        proposal = {'id': 'track-%s-design' % tid, 'rationale': 'scripted design'}
        name = 'design docs'
    else:
        path = os.path.join(asg['worktree'], 'rtl', 'vhsnunzip_unbuffered.vhd')
        with open(path, encoding='utf-8', errors='replace') as fil:
            text = fil.read()
        with open(path, 'w', encoding='utf-8', newline='\n') as fil:
            fil.write('-- fake track step: no functional change (%s)\n' % asg['branch'] + text)
        proposal = {'id': 'track-%s-step-%s' % (tid, asg.get('step')),
                    'rationale': 'scripted step', 'step_kind': asg.get('kind')}
        name = 'comment-only step'
    write_json(os.path.join(asg['worktree'], 'PROPOSAL.json'), proposal)
    with open(os.path.join(asg['worktree'], 'NOTES.md'), 'w', encoding='utf-8',
              newline='\n') as fil:
        fil.write('## How it works\nfake track session.\n## What I tried\n%s\n'
                  '## Why it worked or failed\nscripted.\n## Facts\n' % name)
    log('    %s | fake track %s' % (asg['label'], name))
    return name


def fake_write_candidates(assignments, log):
    """Apply scripted edits instead of running model sessions."""
    out = []
    for idx, asg in enumerate(assignments):
        if asg.get('track_id'):
            fake_track_session(asg, log)
            out.append({'label': asg['label'], 'direction': asg['direction'],
                        'session': {'status': 'ok', 'cost_usd': 0.0, 'turns': 0,
                                    'seconds': 0.0, 'error': '', 'subtype': 'fake'},
                        'proposal': propose.read_proposal(asg['worktree'])})
            continue
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


# Reasons a synthesis host gives for failing, rather than a design. Kits
# before the host wait recorded these as failed_synth.
HOST_FAILURE = re.compile(r'ssh to \S+ (?:failed|did not finish)|could not create a '
                          r'working directory on|ended without an exit status|'
                          r'could not be reached')


def never_measured(c):
    """Whether a candidate's measurement did not happen: its simulators were
    killed from outside, or the synthesis host stayed out of reach. Its design
    was never judged, so it is measured again, even on the same base."""
    if c.get('outcome') == 'measure_error':
        return True
    return (c.get('outcome') == 'failed_synth'
            and bool(HOST_FAILURE.search(c.get('reason') or '')))


def synth_only_failure(metrics):
    """Whether every draw simulated byte-exact and only synthesis is
    missing: a synthesis error, or a host that stayed out of reach. A
    non-final track step is judged on simulation alone, so for it this
    changes nothing (a killed simulator is not this: nothing was judged)."""
    metrics = metrics or {}
    err = metrics.get('measure_error') or ''
    if err and not HOST_FAILURE.search(err):
        return False
    return (bool(err or metrics.get('synth_error')) and not metrics.get('error')
            and bool(metrics.get('oracle_pass'))
            and metrics.get('bytes_per_cycle') is not None)


def retry_eligible(c, retried):
    """Whether a candidate may be offered again (git aside).

    A retry is its original candidate again, on its own new branch, and the
    original has had its one retry. Without this check the same design
    could come back after every adoption and be measured again each time.
    """
    if not c.get('branch') or c['branch'] in retried \
            or str(c.get('id') or '').startswith('retry-'):
        return False
    if c.get('track'):
        # A track step is a part of a larger build, judged against its own
        # prediction; merged alone onto the best it would be a half design.
        return False
    if never_measured(c):
        return True
    m = c.get('measured') or {}
    return (c.get('outcome') in ('too_expensive', 'candidate')
            and (m.get('gain_pct') or 0) >= RETRY_MIN_GAIN_PCT)


def retry_candidates(run, state, k, iter_dir, log):
    """Re-merge earlier rejected candidates with a large gain onto the current
    best and offer them as extra candidates.

    A candidate can be rejected for a rule that later changes (area pricing),
    or lose only because a sibling scored better that iteration. If its
    branch still merges cleanly onto the best design, it is measured again.
    Each old candidate is retried once, and a retry is never retried.

    A candidate built on the current best is left alone until the best moves:
    the merge would fast-forward to the very design already measured, and a
    measurement is no longer half a minute but half an hour of simulation.
    A candidate that was never measured is the exception, and goes first:
    its session was paid for and its design never judged.
    """
    retried = state.setdefault('retried', [])
    best = state['best']['commit']
    pool = []
    for it in state['iterations']:
        for c in it.get('candidates', []):
            if retry_eligible(c, retried) \
                    and git_ok(['rev-parse', '--verify', 'refs/heads/' + c['branch']]) \
                    and (never_measured(c)
                         or not git_ok(['merge-base', '--is-ancestor', best, c['branch']])):
                pool.append(c)
    pool.sort(key=lambda c: (not never_measured(c),
                             -((c.get('measured') or {}).get('gain_pct') or 0)))
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
        measured = old.get('measured') or {}
        if never_measured(old):
            then = 'never measured at iteration %d: %s' % (it_of(state, old),
                                                           (old.get('reason') or '')[:90])
            hypothesis = 'not measured earlier (%s); measured now' % then
        else:
            then = 'measured %+.2f%% at iteration %d' % (measured.get('gain_pct') or 0,
                                                         it_of(state, old))
            hypothesis = ('rejected or outscored earlier with %+.2f%% gain; may pass '
                          'under the current rule or on the current best'
                          % (measured.get('gain_pct') or 0))
        log('  %s: retrying %s (%s) merged onto the current best' % (label, old.get('id'), then))
        out.append(({'label': label, 'branch': branch,
                     'direction': {'focus': 'retry of %s: %s' % (old.get('id'), (old.get('direction') or {}).get('focus', '')),
                                   'hypothesis': hypothesis,
                                   'skill_ids': (old.get('direction') or {}).get('skill_ids') or []},
                     'id': 'retry-' + str(old.get('id')), 'rationale': old.get('rationale', ''),
                     'expected_gain_pct': measured.get('gain_pct', old.get('expected_gain_pct')),
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


def commit_candidate(path, message, extra_paths=()):
    """Commit rtl/ changes in a worktree (plus ``extra_paths``, a track's
    docs/track-<id>, when they exist). Returns (sha, files) or (None, []).
    The harness the loop synced into agentic/ is never committed."""
    git(['add', '-A', 'rtl'], cwd=path)
    for rel in extra_paths:
        if os.path.exists(os.path.join(path, rel)):
            git(['add', '-A', '--', rel], cwd=path)
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
    if metrics.get('measure_error'):
        # The tools failed, not the design: never a correctness verdict, and
        # never counted against the skill the candidate used.
        out['outcome'] = 'measure_error'
        out['reason'] = metrics['measure_error']
        return out
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
    # Throughput alone decides; area is measured and shown, never scored.
    # The user wants single-stream speed, and one decompressor fits the U55C
    # many times over, so area does not limit it. A price on area growth
    # would reject large structural changes along with every partial step
    # towards them.
    score = -gain
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
            # "output port idle": the port counters alone, which can read a
            # busy stage as bubbles; where there is a probe its limit is
            # named instead.
            limit = limit_phrase((rec.get('analysis') or {}).get('probe'))
            lines.append('  draw %-16s %7.3f bytes/cycle, output port idle %5.1f%%, input stalled '
                         '%5.1f%%%s  (%s)'
                         % (rec['name'], rec['bytes_per_cycle'], rec.get('output_idle_pct', 0),
                            rec.get('input_stall_pct', 0), ('; ' + limit) if limit else '',
                            'a whole Parquet row group' if rec['kind'] == 'real'
                            else 'synthetic chunks, not scored'))
    hidden = [r['bytes_per_cycle'] for r in metrics.get('draws', [])
              if not r.get('visible') and r.get('scored') and r.get('oracle_pass')]
    if hidden:
        lines.append('  plus %d hidden real-data draws (other tables) at %.3f to %.3f bytes/cycle'
                     % (len(hidden), min(hidden), max(hidden)))
    limit = limit_phrase((metrics.get('profile') or {}).get('probe'))
    if limit:
        lines.append('  probe over all scored draws (cycle-weighted): %s' % limit)
    return '\n'.join(lines)


def limit_phrase(verdict):
    """One short phrase for the probe's verdict, or '' without one, e.g.
    'limit: datapath_inst/main_dec_inst (moves 99%, input waits 22%)'."""
    try:
        if not verdict or not verdict.get('handshakes') or not verdict.get('limit'):
            return ''
        io, _order = probe_mod.stage_io(verdict['handshakes'])
        stage = verdict['limit']
        name = analyse.stage_name(stage, verdict.get('stages'))
        mine = io.get(stage) or {'in': [], 'out': []}
        if verdict.get('kind') == 'rate' and mine['out'] and mine['in']:
            out = max(mine['out'], key=lambda h: h['moved'])
            inp = max(mine['in'], key=lambda h: h['blocked'])
            return 'limit: %s (moves %.0f%%, input waits %.0f%%)' % (
                name, 100.0 * out['moved'], 100.0 * inp['blocked'])
        if verdict.get('kind') == 'backpressure' and mine['in']:
            inp = max(mine['in'], key=lambda h: h['blocked'])
            return 'holds the pipe back: %s (%s waits %.0f%%)' % (
                name, inp['key'], 100.0 * inp['blocked'])
        if verdict.get('kind') == 'starved' and mine['in']:
            inp = max(mine['in'], key=lambda h: h['empty'])
            return 'starved: %s (input empty %.0f%%)' % (name, 100.0 * inp['empty'])
    except Exception:                         # advice only; never a crash
        return ''
    return ''


def _without_probe(analysis):
    """A draw's analysis with its per-draw probe verdict removed."""
    if not isinstance(analysis, dict) or 'probe' not in analysis:
        return analysis
    out = dict(analysis)
    out.pop('probe', None)
    return out


def best_view(metrics):
    """The measurement as the best design keeps it: per-draw probe data only
    for the visible draws (and never their window counts), plus the
    cycle-weighted totals. A held-out table's own handshake fractions would
    say something about that table; only the aggregate is ever shown."""
    out = dict(metrics)
    draws = []
    for rec in metrics.get('draws', []) or []:
        rec = dict(rec)
        if not rec.get('visible'):
            rec.pop('probe', None)
            rec['analysis'] = _without_probe(rec.get('analysis'))
        elif isinstance(rec.get('probe'), dict):
            rec['probe'] = dict((k, v) for k, v in rec['probe'].items() if k != 'windows')
        draws.append(rec)
    out['draws'] = draws
    return out


def profile_text(metrics):
    parts = []
    for rec in metrics.get('draws', []):
        if rec.get('visible') and rec.get('kind') == 'real' and rec.get('analysis'):
            parts.append(analyse.describe(rec['analysis'], rec['name']))
    # One visible table can hide which stage binds the score. The busiest
    # scored held-out table's rates are shown without its name: the numbers
    # are not the stimulus.
    hidden = [r for r in metrics.get('draws', [])
              if not r.get('visible') and r.get('scored') and r.get('oracle_pass')
              and r.get('analysis')]
    if hidden:
        worst = max(hidden, key=lambda r: r['analysis'].get('binding_utilisation') or 0)
        # Its rates, without its own probe lines: inside the design a
        # held-out table is shown only as part of the aggregate below.
        parts.append(analyse.describe(_without_probe(worst['analysis']),
                                      'the busiest held-out table (name withheld)'))
    prof = metrics.get('profile') or {}
    if prof:
        parts.append('across all real draws: binding stage %s (tightest %s at %.0f%%), '
                     'output port idle %.1f%% on average'
                     % (prof.get('binding'), prof.get('tightest'),
                        100.0 * (prof.get('tightest_utilisation') or 0),
                        prof.get('mean_output_idle_pct') or 0))
    verdict = prof.get('probe')
    if verdict:
        verdict = dict((k, v) for k, v in verdict.items() if k != 'window_limit_share')
        try:
            lines = analyse.describe_probe(verdict)
        except Exception:
            lines = ''
        if lines:
            parts.append('probe over all scored real draws, cycle-weighted (the held-out '
                         'tables are never shown one by one):\n' + lines)
    elif metrics.get('probe_status') and metrics['probe_status'] != 'on':
        parts.append('(no probe inside the design: %s)' % metrics['probe_status'][:160])
    return '\n'.join(parts) or '(no profile)'


_HELD_NAME = re.compile(r'\bheld-[A-Za-z0-9_.-]+')
_PAGE_BYTES = re.compile(r'\b[0-9a-fA-F]{16,}\b')


def withheld(text):
    """A recorded reason as the planner and sessions may read it. An oracle
    failure's reason names the draw and quotes the bytes it got and
    expected ('held-nation: wrong output ... got 6865..., expected ...'):
    for a held-out draw that is the table's name and its page content, which
    a session must never see. The verdict and the offset stay."""
    text = _HELD_NAME.sub('a held-out table', text or '')
    return _PAGE_BYTES.sub('<bytes withheld>', text)


def history_text(state):
    lines = []
    # Lessons older than four iterations are dropped here: by then the learner
    # has attached what mattered to a skill, and keeping both copies spent a
    # fifth of the brief on the same evidence twice.
    fresh = {it['iteration'] for it in state['iterations'][-4:]}
    for it in state['iterations'][-12:]:
        for c in it.get('candidates', []):
            m = c.get('measured') or {}
            outcome = c.get('outcome', '')
            if c.get('track'):
                lines.append(track_history_line(it['iteration'], c))
                continue
            if outcome == 'too_expensive':
                # Recorded under the area rule v3 removed. Its gain_pct was
                # always the throughput gain, so say what it is worth now.
                outcome = ('too_expensive under the area rule removed in v3; on '
                           'throughput alone it was %+.1f%%' % (m.get('gain_pct') or 0))
            bits = ['i%d %s' % (it['iteration'], c.get('id') or 'no proposal'), outcome]
            if m.get('throughput_gain_pct') is not None:
                bits.append('throughput %+.2f%%, bytes/cycle %+.2f%%, f_max %+.2f%%, area %+.2f%%'
                            % (m['throughput_gain_pct'], m.get('bpc_gain_pct') or 0,
                               m.get('fmax_gain_pct') or 0, m.get('area_gain_pct') or 0))
            if c.get('expected_gain_pct') is not None:
                bits.append('predicted %+.1f%%' % c['expected_gain_pct'])
            if c.get('adopted'):
                bits.append('ADOPTED')
            line = '- ' + ': '.join(bits[:2]) + ('; ' + '; '.join(bits[2:]) if len(bits) > 2 else '')
            if c.get('reason') and c.get('outcome') not in ('adopted', 'candidate',
                                                            'too_expensive'):
                line += ' -- ' + withheld(c['reason'])[:160]
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


# ---------------------------------------------------------------------------
# the roadmap: the engineer's advice, ranked by modelled gain
#
# roadmap.md is advice, not orders: a step that sessions have proved empty
# must not hold the loop for iterations. Each step carries a modelled gain, a
# session can
# dispute its step with a proof, and a step disputed twice (or measured at
# about zero twice) is dropped and says so in the log. The engineer overrules
# a drop by editing the step's text.

ROADMAP_HEAD = re.compile(r'^STEP\s+(\w[\w.-]*)\s*[:,]\s*(.*)$')
MODEL_TAG = re.compile(r'\[model\s+([^\]]+)\]', re.I)
_TAG_PCT = re.compile(r'^([+-]?\d+(?:\.\d+)?)\s*%$')
_TAG_BPC = re.compile(r'^(\d+(?:\.\d+)?)\s*B/cycle$', re.I)


def parse_roadmap(text):
    """(preamble, [{'id', 'title', 'body', 'text', 'tag'}]) from roadmap.md.

    A step starts at a line 'STEP <id>: title' or 'STEP <id>, title' (a
    colon-only rule would fold 'STEP 4, if ...' into the step before). tag is
    the text of a '[model ...]' tag, or None.
    """
    pre, steps = [], []
    for line in (text or '').splitlines():
        m = ROADMAP_HEAD.match(line)
        if m:
            steps.append({'id': propose.norm_step_id(m.group(1)),
                          'title': m.group(2).strip(), 'lines': []})
            continue
        (steps[-1]['lines'] if steps else pre).append(line)
    out, seen = [], set()
    for st in steps:
        if not st['id'] or st['id'] in seen:
            continue                      # a repeated heading: the first one counts
        seen.add(st['id'])
        body = '\n'.join(st['lines']).strip('\n')
        whole = st['title'] + '\n' + body
        tag = MODEL_TAG.search(whole)
        out.append({'id': st['id'], 'title': st['title'], 'body': body,
                    'text': whole, 'tag': tag.group(1).strip() if tag else None})
    return '\n'.join(pre).strip(), out


def roadmap_specs(steps):
    """The tags of the steps an optional model can evaluate (none in this
    kit). A tag left out here leaves its step unmodelled."""
    out = []
    for st in steps:
        tag = st.get('tag')
        if not tag or _TAG_PCT.match(tag) or _TAG_BPC.match(tag) or tag in out:
            continue
        try:
            if packmodel is None:
                continue
            packmodel.parse_spec(tag)
        except Exception:
            continue
        out.append(tag)
    return out


def model_step(tag, best_bpc, extra):
    """(modelled bytes/cycle, modelled gain %) for one tag, or (None, None).

    '[model +x%]' and '[model y B/cycle]' are taken literally; any other
    tag is an optional model's number (extra), unused unless calibrated.
    """
    if not tag or not best_bpc:
        return None, None
    m = _TAG_PCT.match(tag)
    if m:
        gain = float(m.group(1))
        return round(best_bpc * (1.0 + gain / 100.0), 3), round(gain, 1)
    m = _TAG_BPC.match(tag)
    if m:
        bpc = float(m.group(1))
    else:
        got = (extra or {}).get(tag) or {}
        if not got.get('calibrated') or not got.get('all'):
            return None, None
        bpc = float(got['all'])
    return round(bpc, 3), round(100.0 * (bpc / best_bpc - 1.0), 1)


def _sha(text):
    import hashlib
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:16]


def new_roadmap_step(st):
    return {'title': st['title'][:200], 'text_sha': _sha(st['text']),
            'modelled_bpc': None, 'modelled_gain_pct': None, 'status': 'open',
            'disputes': [], 'zero_results': [], 'dropped_at': None,
            'drop_reason': '', 'in_file': True}


def state_defaults(state):
    """Keys newer kits keep in state.json, added to a state from an older
    kit so it loads unchanged otherwise."""
    state.setdefault('roadmap', {}).setdefault('steps', {})
    if not isinstance(state.get('tracks'), list):
        state['tracks'] = []
    return state


def sync_roadmap_state(state, steps, best_bpc, extra, log=None):
    """Bring state['roadmap'] in line with the file. Idempotent, so a redone
    iteration changes nothing twice. New steps are added; a dropped step
    whose text the engineer edited reopens with its counters cleared (that
    is how a drop is overruled); a step gone from the file stays in state,
    hidden. Modelled gains are recomputed for the current best."""
    known = state_defaults(state)['roadmap']['steps']
    ids = set()
    for st in steps:
        ids.add(st['id'])
        rec = known.get(st['id'])
        if rec is None:
            rec = known[st['id']] = new_roadmap_step(st)
        sha = _sha(st['text'])
        if rec.get('text_sha') != sha:
            if rec.get('status') == 'dropped':
                rec.update(status='open', disputes=[], zero_results=[], dropped_at=None,
                           drop_reason='')
                if log:
                    log('roadmap step %s was edited after it was dropped; it is open again'
                        % st['id'])
            rec['text_sha'] = sha
        rec['title'] = st['title'][:200]
        rec['in_file'] = True
        rec['modelled_bpc'], rec['modelled_gain_pct'] = model_step(st.get('tag'), best_bpc,
                                                                   extra)
    for sid, rec in known.items():
        if sid not in ids:
            rec['in_file'] = False
    return known


def _rank_key(item):
    idx, _st, rec = item
    gain = rec.get('modelled_gain_pct')
    return (gain is None, -(gain or 0.0), idx)


def roadmap_brief(state, preamble, steps):
    """The roadmap as the planner and the sessions read it: open and disputed
    steps first, ranked by modelled gain (unmodelled last, in file order),
    each with its text and any proofs against it; then dropped steps, one
    line each with the reason."""
    if not steps:
        return ''
    known = (state.get('roadmap') or {}).get('steps') or {}
    items = [(i, st, known.get(st['id']) or new_roadmap_step(st))
             for i, st in enumerate(steps)]
    live = sorted([it for it in items if it[2].get('status') != 'dropped'], key=_rank_key)
    dead = [it for it in items if it[2].get('status') == 'dropped']
    lines = []
    if preamble:
        lines += ["Engineer's note:", preamble, '']
    for _i, st, rec in live:
        if rec.get('modelled_gain_pct') is not None:
            model = 'modelled %.1f B/cycle = %+.0f%% bytes/cycle' % (
                rec.get('modelled_bpc') or 0, rec['modelled_gain_pct'])
        else:
            model = 'unmodelled'
        disputes = rec.get('disputes') or []
        if rec.get('status') == 'disputed' and disputes:
            status = 'DISPUTED %s' % ('once' if len(disputes) == 1
                                      else '%d times' % len(disputes))
        else:
            status = 'open'
        lines.append('STEP %s [%s; %s]: %s' % (st['id'], status, model, st['title']))
        if st['body']:
            lines.append(st['body'])
        for d in disputes:
            lines.append('    proof from i%s %s: "%s"' % (d.get('iteration'), d.get('label'),
                                                      (d.get('proof') or '')[:900]))
        zeros = rec.get('zero_results') or []
        if zeros:
            lines.append('    measured within %.0f%% of zero: %s'
                         % (float(CONFIG.get('roadmap_zero_pct', 1.0)),
                            ', '.join('i%s %+.1f%%' % (z.get('iteration'), z.get('gain_pct') or 0)
                                      for z in zeros)))
        lines.append('')
    for _i, st, rec in dead:
        lines.append('STEP %s [DROPPED at i%s %s]: %s' % (
            st['id'], rec.get('dropped_at'), rec.get('drop_reason') or '', st['title'][:120]))
    return '\n'.join(lines).strip()


def vet_directions(state, directions, log):
    """Clear a direction's roadmap_step when it names no known step, or a
    dropped one. The direction itself is kept."""
    known = (state.get('roadmap') or {}).get('steps') or {}
    for d in directions:
        sid = propose.norm_step_id(d.get('roadmap_step'))
        if not sid:
            d['roadmap_step'] = None
            continue
        rec = known.get(sid)
        if rec is None or not rec.get('in_file', True):
            log('  planner named unknown roadmap step %s; ignored' % sid)
            d['roadmap_step'] = None
        elif rec.get('status') == 'dropped':
            log('  planner assigned roadmap step %s, which is dropped; the direction is '
                'kept without it' % sid)
            d['roadmap_step'] = None
        else:
            d['roadmap_step'] = sid
    return directions


ROADMAP_COUNTED = ('candidate', 'no_gain', 'regressed')


def roadmap_events(cands, k):
    """The roadmap events this iteration's candidates produced.

    Nothing is applied here: events go into state only in append_record,
    in the same save as the iteration record, so an interrupted iteration
    that is redone cannot count a dispute twice.

    dispute      the session declined (no_proposal) with a 'dispute' field
                 naming the very step its direction carried, and finished
                 normally (not cut off by budget, time or a limit). A bare
                 rationale is not a dispute.
    zero result  the direction carried a step and the measured gain was
                 within roadmap_zero_pct of zero. Compile, correctness and
                 measurement failures say nothing about the step, and
                 neither does a truncated session or a retry.
    """
    events = []
    zero_pct = float(CONFIG.get('roadmap_zero_pct', 1.0))
    for c in cands:
        sid = propose.norm_step_id((c.get('direction') or {}).get('roadmap_step'))
        if not sid:
            continue
        sess = c.get('session') or {}
        if c.get('outcome') == 'no_proposal':
            if c.get('dispute') and propose.norm_step_id(c.get('dispute_step')) == sid \
                    and sess.get('status') == 'ok' and not c.get('truncated'):
                why = propose.notes_section(c.get('notes') or '', 'why it worked')
                proof = ' '.join(x for x in (c['dispute'], c.get('rationale') or '', why) if x)
                events.append({'kind': 'roadmap_dispute', 'step': sid, 'iteration': k,
                               'label': c.get('label'), 'proof': proof[:2000]})
            continue
        gain = (c.get('measured') or {}).get('gain_pct')
        if c.get('outcome') in ROADMAP_COUNTED and gain is not None \
                and abs(gain) < zero_pct and not c.get('truncated') \
                and not str(c.get('id') or '').startswith('retry-'):
            events.append({'kind': 'roadmap_zero', 'step': sid, 'iteration': k,
                           'label': c.get('label'), 'gain_pct': round(gain, 2)})
    return events


def apply_roadmap_events(state, events, log=None):
    """Apply dispute and zero-result events; drop a step at its limit.
    Returns the drop lines (for the log banner and the status line)."""
    known = state_defaults(state)['roadmap']['steps']
    lim_d = int(CONFIG.get('roadmap_dispute_limit', 2))
    lim_z = int(CONFIG.get('roadmap_zero_limit', 2))
    zero_pct = float(CONFIG.get('roadmap_zero_pct', 1.0))
    drops = []
    for ev in events or []:
        rec = known.get(ev.get('step'))
        if rec is None or rec.get('status') == 'dropped':
            continue
        if ev['kind'] == 'roadmap_dispute':
            rec.setdefault('disputes', []).append(
                {'iteration': ev['iteration'], 'label': ev['label'], 'proof': ev['proof']})
            rec['status'] = 'disputed'
            if log:
                log('ROADMAP STEP %s DISPUTED by i%s %s: %s'
                    % (ev['step'], ev['iteration'], ev['label'], ev['proof'][:300]))
        elif ev['kind'] == 'roadmap_zero':
            rec.setdefault('zero_results', []).append(
                {'iteration': ev['iteration'], 'label': ev['label'],
                 'gain_pct': ev['gain_pct']})
            if log:
                log('roadmap step %s measured %+.2f%% (i%s %s): within %.0f%% of zero'
                    % (ev['step'], ev['gain_pct'], ev['iteration'], ev['label'], zero_pct))
        else:
            continue
        disputes, zeros = rec.get('disputes') or [], rec.get('zero_results') or []
        reason = ''
        if len(disputes) >= lim_d:
            reason = 'after %d disputes (%s)' % (len(disputes), ', '.join(
                'i%s %s' % (d['iteration'], d['label']) for d in disputes))
        elif len(zeros) >= lim_z:
            reason = 'after %d results within %.0f%%: %s' % (len(zeros), zero_pct, ', '.join(
                '%+.1f%%' % (z.get('gain_pct') or 0) for z in zeros))
        if reason:
            rec.update(status='dropped', dropped_at=ev['iteration'], drop_reason=reason)
            drops.append('ROADMAP STEP %s DROPPED %s' % (ev['step'], reason))
    for line in drops:
        if log:
            log('=' * 72)
            log(line)
            log('=' * 72)
    return drops


# ---------------------------------------------------------------------------
# optional models for the prompts: none in this kit

def run_packmodel(run_name, commit, specs):
    return {'text': '', 'extra': {}}


def calibrate_packmodel(run, log):
    return None


def packmodel_context(run, specs, log, timing):
    return '', {}


# A fixed line before the skill library. Its notes are the learner's own text
# and are not rewritten; this says how to read the ones from the area era.
AREA_ERA_NOTE = ('(Skill notes that mention efficiency, an EI floor or too_expensive were '
                 'written under the area rule removed in v3; judge those ideas on '
                 'throughput alone.)')


def build_ctx(state, skills_data, iteration, guide_text='', roadmap_text='',
              packmodel_text=''):
    best = state['best']['metrics']
    return {
        'guide_text': guide_text,
        'roadmap_text': roadmap_text,
        'packmodel_text': packmodel_text,
        'best_commit': state['best'].get('commit'),
        'goal_text': state['goal_text'],
        'iteration': iteration,
        'max_iters': state['max_iters'],
        'progress_text': progress_text(state),
        'state_text': state_text(best, state['baseline']),
        'profile_text': profile_text(best),
        'lever': best.get('lever') or {'lever': 'unknown', 'reason': 'no profile'},
        'skills_text': AREA_ERA_NOTE + '\n' + skills_mod.format_for_prompt(
            skills_data, max_entries=30, notes_per_skill=1),
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
        self.packmodel_cache = None

    def save(self):
        self.state['updated'] = now_iso()
        write_json(self.state_path, self.state)
        # The state is on disk by now; a report that cannot be drawn must not
        # crash the loop from inside a save.
        try:
            report.write_report(self.state, os.path.join(self.dir, 'report.html'))
        except Exception as exc:
            self.log('could not write the report: %s' % str(exc)[:200])

    def roadmap_text(self):
        """roadmap.md in the run folder: the engineer's staged plan, read every
        iteration so it can be edited while the run goes on, or ''."""
        try:
            with open(os.path.join(self.dir, 'roadmap.md'), encoding='utf-8') as fil:
                return fil.read().strip()
        except IOError:
            return ''

    def context(self, skills_data, k, log, timing):
        """build_ctx with the roadmap brought up to date:
        the roadmap's steps synced into state (new steps, edits, modelled
        gains for the current best) and ranked by modelled gain."""
        state = self.state
        preamble, steps = parse_roadmap(self.roadmap_text())
        pm_text, extra = packmodel_context(self, roadmap_specs(steps), log, timing)
        sync_roadmap_state(state, steps, state['best']['metrics'].get('bytes_per_cycle'),
                           extra, log)
        return build_ctx(state, skills_data, k, guide_text=self.guide_text(),
                         roadmap_text=roadmap_brief(state, preamble, steps),
                         packmodel_text=pm_text)

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
    # The child owns the run from here and saves it itself. This process must
    # never save again: its state is older than the child's. It used to fall
    # through to the interrupt handler when the window closed and write its
    # copy last, which rolled a run back by many iterations.
    # subprocess.call would also kill the child on Ctrl+C before it saved.
    child = subprocess.Popen(cmd, env=env)
    while True:
        try:
            rc = child.wait()
            break
        except KeyboardInterrupt:
            continue            # the child got the same signal and is saving
    sys.stdout.flush()
    os._exit(rc)


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
    # 60, not 40: the check now prints the probe's handshake lines too.
    return '\n'.join(keep[:60])


def prepare_base(run, log):
    """The base worktree with the current harness in it, for the starting
    check. The worktree holds the kit as committed at the run's start, and
    every `git reset --hard` of it (adopt, resume) puts that back; without
    this the check would run without the probe. Same
    allowlist and manifest as a session's worktree. Returns the base dir."""
    try:
        propose.sync_harness(run.base_dir, os.path.join(run.dir, 'base.harness.json'))
    except Exception as exc:
        log('  could not bring the base worktree\'s harness up to date: %s' % str(exc)[:160])
    return run.base_dir


# ---------------------------------------------------------------------------
# the probe: never allowed to change a score

PROBE_COUNTERS = ('cycles', 'bytes_out', 'co_beats', 'de_beats', 'co_stall', 'de_bubble')


def _probe_off_prep(rtl_dir, build_dir):
    """measure.prepare_probe's answer with the probe switched off."""
    saved = measure.PROBE_OFF_REASON
    measure.PROBE_OFF_REASON = 'a clean re-run, to compare with the probed one'
    try:
        return measure.prepare_probe(rtl_dir, build_dir)
    finally:
        measure.PROBE_OFF_REASON = saved


def probe_clean_check(rtl_dir, work_dir, metrics, draws, jobs, log):
    """Simulate the winner's visible draws once more without the probe and
    compare the counters with the probed run's. The probe only reads, so
    they must be equal; this makes that true of every adoption rather than
    assumed. Returns None when they agree (or nothing was probed), else the
    reason. A clean run that cannot finish is reported and not held against
    the probe: it proves nothing either way."""
    status = metrics.get('probe_status') or ''
    if not (status == 'on' or status.startswith('partial')):
        return None
    probed = dict((r['name'], r) for r in metrics.get('draws', []) if r.get('probe'))
    visible = [d for d in draws if d[0].visible and d[0].name in probed]
    if not visible:
        return None
    bdir = os.path.join(work_dir, 'clean')
    t0 = time.time()
    try:
        widths = metrics.get('widths') or analyse.rtl_widths(rtl_dir)
        clean = measure.simulate(rtl_dir, bdir, visible, widths, jobs=jobs,
                                 probe_rtl=_probe_off_prep(rtl_dir, bdir))
    except Exception as exc:
        log('  the clean re-run before adoption could not run (%s); adopting on the '
            'probed numbers' % str(exc)[:160])
        return None
    finally:
        rmtree(bdir)
    bad = []
    for rec in clean:
        want = (probed.get(rec['name']) or {}).get('counters') or {}
        got = rec.get('counters') or {}
        diff = [k for k in PROBE_COUNTERS if want.get(k) != got.get(k)]
        if not rec.get('oracle_pass') or diff:
            bad.append('%s: %s' % (rec['name'], ', '.join(
                '%s %s probed vs %s clean' % (k, want.get(k), got.get(k)) for k in diff)
                or (rec.get('problem') or 'failed without the probe')[:120]))
    log('  clean re-run of %d visible draw(s) without the probe: %s (%.0f s)'
        % (len(clean), 'identical' if not bad else 'DIFFERENT', time.time() - t0))
    return '; '.join(bad)[:600] if bad else None


def probe_disagreed(run, cands, reason, log):
    """The probe changed a measurement: switch it off for the rest of the
    run and call this iteration's measured candidates never measured, so
    the retry path measures them again without it. Nothing is adopted."""
    run.state['probe_disabled'] = reason
    measure.PROBE_OFF_REASON = 'disabled for this run: ' + reason[:200]
    log('=' * 72)
    log('PROBE DISABLED: a probed run and a clean run of the same design differ (%s). '
        'Nothing is adopted this iteration; its candidates are measured again without '
        'the probe.' % reason[:300])
    log('=' * 72)
    for c in cands:
        if c.get('commit') and c.get('outcome') not in ('no_proposal', 'measure_error'):
            c.update(outcome='measure_error', adoptable=False, score=None,
                     reason='measured with a probe that changed the counters; '
                            'measured again without it')


def probe_backfill(run, draws, log):
    """Give the best design a probe once when it was measured before the
    probe existed (a resumed run), so the planner sees which stage limits.
    Simulates every scored draw (no synthesis), and merges only if every
    draw's bytes/cycle equals the stored value. Tried once per best commit."""
    state = run.state
    best = state['best']['metrics']
    commit = state['best']['commit']
    if best.get('probe') or state.get('probe_disabled') \
            or not CONFIG.get('probe_enabled', True) \
            or (state.get('probe_backfill') or {}).get('commit') == commit:
        return None
    scored = [d for d in draws if d[0].scored]
    if not scored:
        return None
    log('measuring the best design once more with the probe, to see inside it '
        '(simulation only, %d scored draws)...' % len(scored))
    run.status.set(phase='probe', detail='probing the best design once')
    work = os.path.join(run.dir, 'probe-backfill')
    t0 = time.time()
    try:
        got = measure.measure(os.path.join(run.base_dir, 'rtl'), work, scored,
                              synth=False, jobs=sim_jobs(1))
    except Exception as exc:
        got = {'error': 'crashed: %s' % exc}
    finally:
        rmtree(os.path.join(work, 'sim'))
    seconds = round(time.time() - t0, 1)
    want = dict((r['name'], r.get('bytes_per_cycle')) for r in best.get('draws', [])
                if r.get('scored'))
    have = dict((r['name'], r.get('bytes_per_cycle')) for r in got.get('draws', []))
    differ = [n for n, v in want.items()
              if v is None or have.get(n) is None or abs(have[n] - v) > 1e-9]
    rec = {'commit': commit, 'seconds': seconds, 'status': got.get('probe_status'),
           'merged': False}
    if got.get('error') or got.get('measure_error') or not got.get('oracle_pass') or differ:
        why = (got.get('error') or got.get('measure_error') or got.get('first_problem')
               or 'bytes/cycle differs on %d draw(s)' % len(differ))
        rec['why'] = str(why)[:300]
        log('  not merged: %s (%.1f min)' % (rec['why'], seconds / 60.0))
    elif not got.get('probe'):
        rec['why'] = 'no probe data (%s)' % got.get('probe_status')
        log('  nothing to merge: %s (%.1f min)' % (rec['why'], seconds / 60.0))
    else:
        fresh = best_view(got)
        by_name = dict((r['name'], r) for r in fresh['draws'])
        for draw in best.get('draws', []):
            new = by_name.get(draw['name'])
            if new:
                draw['analysis'] = new.get('analysis')
                if draw.get('visible') and new.get('probe'):
                    draw['probe'] = new['probe']
        best['probe'] = got['probe']
        best['probe_status'] = got.get('probe_status')
        best['profile'] = got.get('profile')
        best['lever'] = analyse.lever(got.get('profile'), best.get('wns_ns'))
        rec['merged'] = True
        log('  merged: identical bytes/cycle on all %d draws; %s (%.1f min)'
            % (len(want), limit_phrase((best['profile'] or {}).get('probe')) or
               'no stage named', seconds / 60.0))
    state['probe_backfill'] = rec
    run.save()
    return rec


# ---------------------------------------------------------------------------
# the corpus a run is scored on never changes under it

def corpus_guard(state, draws, fresh, fake):
    """(draws, problem). AGENTIC_DRAWS (a comma list of draw names) cuts the
    corpus for the loop's own dry runs, and is honoured only with --fake on
    a new run: a real run scored on a subset, or resumed onto a different
    corpus, would compare numbers that measure different things.

    The cut is stored as state['test_draws'], so the same --fake run can be
    resumed (with --fake, and AGENTIC_DRAWS unset or the same) to test
    Ctrl-C and --resume; a run without that key is never cut. The draws a
    fake run keeps are all scored: the small ones it can afford to simulate
    (synth-512B, synth-8192B) are not scored in a real run, and without this
    its baseline read 0 B/cycle and every candidate 'no synthesis numbers'."""
    names = os.environ.get('AGENTIC_DRAWS', '').strip()
    want = [n.strip() for n in names.split(',') if n.strip()]
    stored = state.get('test_draws')
    if stored and not fake:
        return draws, ('this is a --fake test run on draws %s; resume it with --fake'
                       % ', '.join(stored))
    if want and not (fake and (fresh or sorted(want) == sorted(stored or []))):
        return draws, ('AGENTIC_DRAWS is a test-only setting: it is honoured only with '
                       '--fake on a new run (or the same draws on its resume). Unset it.')
    want = want or (stored if fake else None)
    if want:
        cut = []
        for d in draws:
            if d[0].name in want:
                mine = copy.copy(d[0])
                mine.scored = True
                cut.append((mine,) + tuple(d[1:]))
        draws = cut
        missing = sorted(set(want) - set(d[0].name for d in draws))
        if missing or not draws:
            return draws, 'AGENTIC_DRAWS names draws the corpus does not have: %s' % (
                ', '.join(missing) or names)
        state['test_draws'] = sorted(want)
    have = sorted(d[0].name for d in draws)
    recorded = state.get('draw_names') or sorted(
        r['name'] for r in (state.get('baseline') or {}).get('draws', []) or [])
    if recorded and sorted(recorded) != have:
        return draws, ('this run was scored on draws %s and the corpus now has %s; resume '
                       'it with the same corpus, or start a new run'
                       % (', '.join(sorted(recorded)), ', '.join(have)))
    return draws, None


HOST_POLL_S = 120


def wait_for_host(run, state, log):
    """Hold the next iteration while the synthesis host cannot be reached:
    sessions written now could not be measured. Polls until it answers."""
    ok, msg = hacc.available()
    if ok:
        return
    log('the synthesis host cannot be reached (%s); waiting for it before '
        'starting sessions (is the VPN up?)' % msg[:160])
    run.status.set(phase='waiting', detail='synthesis host unreachable (VPN?)')
    t0 = time.time()
    while not ok:
        time.sleep(HOST_POLL_S)
        ok, msg = hacc.available()
    waited = time.time() - t0
    state['wait_s'] = round((state.get('wait_s') or 0) + waited, 1)
    log('the synthesis host answers again after %.0f min' % (waited / 60.0))


def sim_jobs(candidates):
    """Draws each candidate simulates at once: sim_slots shared out, never
    fewer than the three the script runs by default."""
    return max(3, int(CONFIG['sim_slots']) // max(1, candidates))


def measure_one(args):
    """measure.measure for one candidate: (rtl, work, draws, jobs[, synth]).
    A non-final track step is judged by simulation alone, so it may skip
    synthesis (15-20 minutes on the shared host)."""
    rtl_dir, work_dir, draws, jobs = args[:4]
    synth = args[4] if len(args) > 4 else True
    try:
        return measure.measure(rtl_dir, work_dir, draws, synth=synth, jobs=jobs)
    except Exception as exc:                # never let one candidate kill the run
        # A crash inside the measuring code is the kit's fault, not the
        # design's: never measured, so it is measured again once (the retry
        # path allows one per branch) instead of being scored failed_compile
        # and counted against the skill the candidate used.
        return {'oracle_pass': False, 'measure_error': 'measurement crashed: %s' % exc,
                'draws': []}


# ---------------------------------------------------------------------------
# fast and guard iterations
#
# With fast_draws set and guard_every > 0, most iterations are FAST: they
# simulate the small draws plus fast_draws only and do not synthesise.
# Candidates are judged on bytes/cycle over those tables, the parent's last
# synthesised f_max being carried for both sides, so the gain is exactly the
# bytes/cycle gain. Every guard_every-th iteration is a GUARD: every draw and
# synthesis, for the candidates and, when the best was adopted in a fast
# iteration, for the best too. A best that breaks any draw, cannot be
# synthesised, or whose bytes/cycle over all scored draws fell below the last
# guarded design's is rolled back to that design. When a guard finds f_max
# lower than the guard before it, the next iteration is a CLOCK one: the fast
# draws plus synthesis, judged on throughput, so a clock repair can win.

def fast_names():
    """fast_draws as a list: a JSON list in config.json, or a comma list
    (AGENTIC_FAST_DRAWS)."""
    val = CONFIG.get('fast_draws') or []
    if isinstance(val, str):
        val = val.split(',')
    return [v.strip() for v in val if v and v.strip()]


def iteration_kind(state, k):
    """'full' (scheme off), 'guard', 'clock' or 'fast' for iteration k."""
    every = int(CONFIG.get('guard_every') or 0)
    if not fast_names() or every <= 0:
        return 'full'
    if state.get('guard_due') or k % every == 0:
        return 'guard'
    if state.get('clock_due'):
        return 'clock'
    return 'fast'


def kind_draws(draws, kind):
    """The draws an iteration of this kind simulates: all of them, or the
    small ones (cheap, and they catch most breakage) plus fast_draws."""
    if kind in ('full', 'guard'):
        return draws
    keep = set(fast_names())
    return [d for d in draws
            if d[0].name in keep or measure.draw_bytes(d) <= measure.QUICK_DRAW_BYTES]


def throughput_of(bpc, f_max_mhz):
    if not bpc or not f_max_mhz:
        return None
    return round(bpc * f_max_mhz / 1000.0, 4)


def subset_view(metrics, draws):
    """``metrics`` as measured on ``draws`` alone: bytes/cycle is the geomean
    of the scored draws among them, throughput that times the f_max last
    synthesised. Every draw's bytes/cycle is deterministic, so the best's
    recorded per-draw numbers stand in for a re-run of it."""
    names = set(d[0].name for d in draws)
    scored = [r['bytes_per_cycle'] for r in (metrics.get('draws') or [])
              if r.get('name') in names and r.get('scored') and r.get('bytes_per_cycle')]
    bpc = geomean(scored)
    out = dict(metrics)
    out['bytes_per_cycle'] = round(bpc, 4) if bpc else None
    out['throughput_gbps'] = throughput_of(out['bytes_per_cycle'], metrics.get('f_max_mhz'))
    return out


def carry_clock(metrics, ref):
    """A candidate simulated but not synthesised, given the parent's f_max
    (marked as carried) so that its throughput gain is its bytes/cycle gain.
    Area and timing stay unmeasured."""
    if (metrics.get('f_max_mhz') is not None or not metrics.get('oracle_pass')
            or not metrics.get('bytes_per_cycle') or not ref.get('f_max_mhz')):
        return metrics
    out = dict(metrics)
    out['f_max_mhz'] = ref['f_max_mhz']
    out['f_max_from'] = ref.get('f_max_from', 0)
    out['f_max_carried'] = True
    out['synth_backend'] = backend_of(ref)
    out['throughput_gbps'] = throughput_of(out['bytes_per_cycle'], out['f_max_mhz'])
    return out


def guard_snapshot(state, k):
    """What a rollback returns to: the best as a guard confirmed it."""
    return {'best': copy.deepcopy(state['best']), 'iteration': k,
            'guide_notes': state.get('guide_notes')}


def rollback(run, why, k, log):
    """Back to the last guarded design: its branch, its numbers, its guide."""
    state = run.state
    last = state['last_guarded']
    lost = state['best']
    git(['reset', '--hard', last['best']['commit']], cwd=run.base_dir)
    state['best'] = copy.deepcopy(last['best'])
    state['guide_notes'] = last.get('guide_notes')
    run.set_progress()
    run.write_guide(notes=state.get('guide_notes'), facts=state.get('design_facts'))
    run.check_text = ''
    banner(log, 'GUARD: rolled back from %s (i%s) to %s (i%s): %s'
           % (lost.get('id'), lost.get('iteration'), state['best'].get('id'),
              state['best'].get('iteration'), why))
    return {'kind': 'rollback', 'iteration': k, 'from': lost.get('commit'),
            'from_id': lost.get('id'), 'to': state['best'].get('commit'), 'why': why}


def settle_guard(run, got, k, log):
    """Judge the best's own guard measurement (None: it was already guarded).
    Returns (the metrics this iteration's candidates are scored against,
    an event for the record or None)."""
    state = run.state
    if got is None:
        return state['best']['metrics'], None
    last = state['last_guarded']['best']['metrics']
    if got.get('measure_error'):
        # The tools failed, not the design: guard again next iteration, and
        # score this iteration's candidates against the guarded design.
        state['guard_due'] = True
        log('  GUARD: the best could not be measured (%s); guarding again next iteration'
            % got['measure_error'][:200])
        return last, {'kind': 'guard_unmeasured', 'iteration': k}
    if got.get('error') or not got.get('oracle_pass'):
        why = 'it breaks a draw: %s' % (got.get('error') or got.get('first_problem')
                                        or 'oracle failed')[:200]
        return last, rollback(run, why, k, log)
    if got.get('synth_error') or not got.get('f_max_mhz'):
        why = 'it cannot be synthesised: %s' % (got.get('synth_error') or 'no f_max')[:200]
        return last, rollback(run, why, k, log)
    floor = (last.get('bytes_per_cycle') or 0) * (1 - float(CONFIG['min_gain_pct']) / 100.0)
    if (got.get('bytes_per_cycle') or 0) < floor:
        why = ('bytes/cycle over all scored draws is %.4f, below the guarded %.4f: '
               'the fast-iteration gains did not hold'
               % (got.get('bytes_per_cycle') or 0, last.get('bytes_per_cycle') or 0))
        return last, rollback(run, why, k, log)
    fast_bpc = state['best']['metrics'].get('bytes_per_cycle')
    got = dict(got, f_max_from=k)
    state['best']['metrics'] = best_view(got)
    state['best']['guarded'] = True
    run.set_progress()
    log('  GUARD: %s holds on every draw: bytes/cycle %.4f (fast estimate %.4f), '
        'f_max %.1f MHz (guarded design %.1f MHz), throughput %.3f GB/s'
        % (state['best'].get('id'), got['bytes_per_cycle'], fast_bpc or 0,
           got['f_max_mhz'], last.get('f_max_mhz') or 0, got.get('throughput_gbps') or 0))
    return state['best']['metrics'], {'kind': 'guard_held', 'iteration': k,
                                      'commit': state['best'].get('commit')}


def close_guard(state, k, log):
    """After a guard iteration (and its adoption, if any): the best is
    confirmed; if its f_max fell below the previous guard's, the next
    iteration repairs the clock."""
    if not state['best'].get('guarded', True):
        return
    prev = state['last_guarded']['best']['metrics'].get('f_max_mhz') or 0
    now = state['best']['metrics'].get('f_max_mhz') or 0
    noise = float(CONFIG['pnr_noise_pct']) / 100.0
    state['clock_due'] = None
    if prev and now and now < prev * (1 - noise):
        state['clock_due'] = {'iteration': k, 'from_mhz': prev, 'to_mhz': now,
                              'path': state['best']['metrics'].get('critical_path')}
        log('  GUARD: f_max fell from %.1f to %.1f MHz; the next iteration repairs the clock'
            % (prev, now))
    state['last_guarded'] = guard_snapshot(state, k)
    state['guard_due'] = False


def kind_note(state, kind, k):
    """What this iteration measures, for the planner and the sessions."""
    every = int(CONFIG.get('guard_every') or 0)
    nxt = (k // every + 1) * every if every else 0
    if kind == 'fast':
        return ('MEASUREMENT THIS ITERATION: FAST. Candidates are simulated on the small '
                'draws and two held-out tables only and judged on bytes/cycle alone. '
                'Synthesis does not run, so f_max and area are not measured (the f_max '
                'shown is from the last synthesis) and a change that only helps the clock '
                'cannot win. Iteration %d is a GUARD: every table is simulated and every '
                'design synthesised; a best design that breaks any table there, or whose '
                'bytes/cycle over all tables does not hold, is rolled back.' % nxt)
    if kind == 'guard':
        return ('MEASUREMENT THIS ITERATION: GUARD. Every table is simulated and every '
                'candidate synthesised; candidates are judged on throughput '
                '(bytes/cycle x f_max).')
    if kind == 'clock':
        due = state.get('clock_due') or {}
        path = due.get('path') or {}
        return ('MEASUREMENT THIS ITERATION: CLOCK. The guard at iteration %s measured '
                'f_max at %.1f MHz, down from %.1f MHz at the guard before%s. Candidates '
                'are simulated on the small draws and two held-out tables and '
                'synthesised, and judged on throughput (bytes/cycle x f_max): recovering '
                'the clock without losing bytes/cycle wins.'
                % (due.get('iteration'), due.get('to_mhz') or 0, due.get('from_mhz') or 0,
                   (' (worst path %s -> %s)' % (path.get('from'), path.get('to')))
                   if path.get('from') else ''))
    return ''


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
            # A decline that disputes the roadmap step it was given.
            'dispute': prop.get('dispute') or '',
            'dispute_step': prop.get('roadmap_step'),
            'truncated': sess.get('status') in ('budget', 'timeout'),
            'session': sess, 'commit': None, 'files_changed': [],
            'outcome': '', 'reason': '', 'measured': {}, 'score': None,
            'advantage': None, 'adopted': False}
    extra = ()
    if asg.get('track_id'):
        # The record keeps the step, not the spec the session was given.
        cand['direction'] = slim_direction(asg['direction'])
        cand['track'] = track_mark(asg)
        cand['step_kind_claimed'] = prop.get('step_kind')
        cand['predicted_bpc_claimed'] = prop.get('predicted_bpc')
        extra = ('docs/track-%s' % asg['track_id'],)
    try:
        sha, files = commit_candidate(asg['worktree'], 'agentic i%d %s: %s'
                                      % (k, asg['label'], prop.get('id') or 'no proposal'),
                                      extra)
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
    if asg.get('track_id'):
        # Never the no-proposal test: a design step changes no rtl/ at all,
        # and a step's outcome comes from the track's own judging.
        log('  %s: track %s step %s (%s): %s' % (
            asg['label'], asg['track_id'], asg.get('step'), asg.get('kind'),
            ('committed %s' % ', '.join(files)[:120]) if sha else 'nothing committed'))
        if prop.get('step_kind') and prop['step_kind'] != asg.get('kind'):
            log('  %s: the session calls this a %s step; the track says %s, which is what '
                'is judged' % (asg['label'], prop['step_kind'], asg.get('kind')))
        return cand
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


def score_candidate(c, asg, metrics, parent, goal, parent_ram, measure_dir, log,
                    parent_model=None):
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
              # The probe's per-draw counts stay out of the record: a
              # held-out draw's would say something about that table.
              'draws': [{kk: vv for kk, vv in r.items()
                         if kk not in ('analysis', 'counters', 'probe')}
                        for r in metrics.get('draws', [])]})
    c['adoptable'] = ev['adoptable']
    mine_ram = ram_latency(os.path.join(asg['worktree'], 'rtl'))
    if c['adoptable'] and mine_ram != parent_ram:
        c['adoptable'] = False
        c['outcome'] = 'ram_latency_changed'
        c['reason'] = ('the simulation-only RAM latency changed (%s -> %s); '
                       'synthesis uses a fixed memory, so this gain would '
                       'not be real' % (parent_ram, mine_ram))
    elif parent_model and c['adoptable']:
        mine_model = ram_model_of(os.path.join(asg['worktree'], 'rtl'))
        if mine_model and mine_model != parent_model:
            log('  %s: NOTE: it edits the simulation RAM model beyond its stage counts '
                '(%s -> %s); synthesis uses a fixed memory, so check that the gain '
                'is not the model\'s' % (c['label'], parent_model, mine_model))
    status = metrics.get('probe_status')
    if status and status != 'on':
        log('  %s: probe %s' % (c['label'], status[:200]))
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
    state['best'] = {'commit': winner['commit'], 'metrics': best_view(metrics),
                     'iteration': k, 'id': winner['id'],
                     # Measured on every draw and synthesised, or only on a
                     # fast or clock iteration's draws (the next guard checks).
                     'guarded': metrics.get('measured_on', 'full') in ('full', 'guard')}
    if winner.get('track'):
        # The track that won. Its closing is saved only with the iteration
        # record; an interrupt in between saves this best alone, and the
        # redone iteration reads the mark to close the track as adopted
        # (adopted_unsaved) instead of building its final step again.
        state['best']['track'] = dict(winner['track'], iteration=k,
                                      label=winner.get('label'))
    run.set_progress()
    log('ADOPTED %s: %s' % (winner['id'], progress_text(state)))
    # Kept so a later refresh of the guide (new facts, same design) still
    # explains how the design it describes works.
    state['guide_notes'] = propose.notes_section(winner.get('notes') or '', 'how it works')
    run.write_guide(notes=state['guide_notes'], facts=state.get('design_facts'))
    run.check_text = ''            # the design changed; the check must too
    calibrate_packmodel(run, log)


# Outcomes that say nothing about the mechanism a candidate tried: no change
# was made, or the measurement itself failed. A track step's outcome is about
# a part of a larger build: a partial step that measures about zero must not
# demote a mechanism.
TRACK_OUTCOMES = ('track_design_done', 'track_design_invalid', 'track_step_passed',
                  'track_step_short', 'track_step_failed', 'track_never_measured',
                  'track_session_failed', 'track_pending', 'track_final_pending',
                  'track_final_not_better')
NOT_MEASURED = ('no_proposal', 'measure_error') + TRACK_OUTCOMES


def record_outcomes(skills_data, cands):
    """Move the skill counters for the candidates that were measured.

    A session that timed out or hit a limit says nothing about a skill, and
    neither does one cut off mid-change by its budget: what got measured then
    is not the mechanism it set out to build.
    """
    for c in cands:
        if c.get('track'):
            continue
        if c['commit'] and c['outcome'] not in NOT_MEASURED and not c.get('truncated'):
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
    """The model call that turns an iteration's outcomes into skill edits.

    Candidates whose measurement failed are left out: the learner would read
    "killed by the operating system" as a broken design, as it did before.
    """
    group = []
    for c in cands:
        # Track steps too: the learner would read "a ports step measured
        # -1%" as evidence against a mechanism. Their facts are kept anyway.
        if c.get('outcome') == 'measure_error' or c.get('track'):
            continue
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
    if not fake and group:
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


def append_record(run, record, log, events=None):
    """Append one iteration and save. Roadmap events are applied here and
    only here, in the same save as the record: an interrupt anywhere before
    it saves none of them, so a redone iteration cannot count one twice."""
    state = run.state
    road = [ev for ev in events or [] if str(ev.get('kind', '')).startswith('roadmap_')]
    tracks = [ev for ev in events or [] if str(ev.get('kind', '')).startswith('track_')]
    if road:
        record['roadmap_events'] = [dict(ev) for ev in road]
        drops = apply_roadmap_events(state, road, log)
        if drops:
            run.status.set(detail=drops[-1][:200])
    if tracks:
        # The track's whole new state travels as one 'track_state' event;
        # the record keeps the others (open, pass, abandon, ...) to read.
        record['track_events'] = [dict(ev) for ev in tracks if ev['kind'] != 'track_state']
        apply_track_events(state, tracks)
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
    if tracks:
        # Only after the save: state is the truth, and a crash before this
        # line is repaired by reconcile_tracks on resume.
        move_track_branches(state, [ev['track']['id'] for ev in tracks
                                    if ev['kind'] == 'track_state'], log)


# ---------------------------------------------------------------------------
# build tracks (change 4) and their step sessions (change 5)
#
# Some changes pay only when several parts change together: each part
# measures about zero (or worse) on its own, every partial step judged
# against the best is discarded, and one session cannot build it all. A
# track is such a change. A design session writes a
# spec and a step list; each later step builds one module plus its unit test
# on the track's own branch, and is judged against its own prediction, never
# against the best. Only the finished track is scored like a candidate.
#
# The track lives in state['tracks']. An iteration works on a copy of the
# live track and hands the copy to append_record as a 'track_state' event,
# so the track changes in the same save as the iteration record; an
# interrupt before that save keeps none of it (the rule the roadmap's events
# follow). Branches move only after the save; reconcile_tracks repairs a
# crash in between.

TRACK_LIVE = ('open', 'final_pending')
TRACK_FAILED = ('track_step_failed', 'track_step_short', 'track_design_invalid')
TRACK_PASSED = ('track_step_passed', 'track_design_done')
SPEC_CAP = 12000
PREV_NOTES_CAP = 6000
STEP_NOTES_CAP = 1500
SPEC_MIN_CHARS = 1500
UNIT_FILE = re.compile(r'^rtl/unit/([A-Za-z0-9_]{1,40})\.vhd$')
UNIT_TIMEOUT_S = 1500
TRACK_WORDS = {
    'track_design_done': 'design accepted', 'track_design_invalid': 'design rejected',
    'track_step_passed': 'passed', 'track_step_short': 'short of its prediction',
    'track_step_failed': 'failed', 'track_never_measured': 'not measured',
    'track_session_failed': 'session failed', 'track_pending': 'cut off by the usage limit',
    'track_final_pending': 'beats the best, waits for the next iteration',
    'track_final_not_better': 'correct, not better than the best', 'adopted': 'ADOPTED',
}


def banner(log, line):
    log('=' * 72)
    log(line)
    log('=' * 72)


def find_track(state, tid):
    for trk in (state or {}).get('tracks') or []:
        if trk.get('id') == tid:
            return trk
    return None


def open_track(state):
    """The live track (open, or a finished final step waiting to be compared
    again), or None. At most one is ever live."""
    live = [t for t in (state or {}).get('tracks') or [] if t.get('status') in TRACK_LIVE]
    return live[-1] if live else None


def track_branch(run_name, tid):
    # Not under the run branch's name (git cannot hold both 'agentic/run' and
    # 'agentic/run/x'), and not under agentic-cand/, whose branches are
    # single candidates.
    return 'agentic-track/%s/%s' % (run_name, tid)


def step_ref(trk, n):
    """The ref that keeps a passed step's commit after its candidate branch
    is deleted. Track ids never end in -s<digits> (sanitize_track_request),
    so this is never another track's branch."""
    return '%s-s%d' % (trk['branch'], n)


def unique_track_id(state, tid):
    taken = set(t.get('id') for t in (state or {}).get('tracks') or [])
    if tid not in taken:
        return tid
    for j in range(2, 1000):
        cand = '%s-%d' % (tid[:36], j)
        if cand not in taken:
            return cand
    raise ValueError('no free track id for %s' % tid)


def design_step(tid):
    return {'n': 0, 'kind': 'design',
            'text': ('design the track: write docs/track-%s/SPEC.md and docs/track-%s/'
                     'steps.json' % (tid, tid)),
            'predicted_bpc': None, 'widths': None, 'source': 'planner',
            'status': 'pending', 'ref': None, 'attempts': [], 'notes': ''}


def new_track(req, state, k):
    """A track object from a sanitised open_track request, opened on the
    current best. Not in state yet: it enters state['tracks'] in
    append_record. Its predictions stay calibrated to the best it opened on
    (calib_commit), so later adoptions do not move them."""
    best = state['best']
    tid = unique_track_id(state, req['id'])
    return {'id': tid, 'branch': track_branch(state['run'], tid),
            'head': best['commit'], 'base_commit': best['commit'],
            'base_bpc': best['metrics'].get('bytes_per_cycle'),
            'calib_commit': best['commit'], 'goal': req.get('goal', ''),
            'why': req.get('why', ''), 'opened_iteration': k, 'status': 'open',
            'reason': '', 'iterations_used': 0, 'current': 0,
            'never_measured_streak': 0, 'design_failures': 0, 'suspended': 0,
            'planned': req.get('steps') or [], 'steps': [design_step(tid)],
            'final': None, 'pending_slot': None, 'closed_iteration': None}


def _track_problem(trk):
    """Why a live track's state cannot be worked, or ''."""
    steps, cur = trk.get('steps'), trk.get('current')
    if not isinstance(steps, list) or not isinstance(cur, int) or isinstance(cur, bool) \
            or not 0 <= cur < len(steps) or not isinstance(steps[cur], dict):
        return 'state inconsistent: current=%s, steps=%s' % (
            cur, len(steps) if isinstance(steps, list) else steps)
    if not trk.get('head') or not trk.get('branch') or not trk.get('id'):
        return 'state inconsistent: no id, head or branch'
    if trk.get('status') == 'final_pending' and not (trk.get('final') or {}).get('commit'):
        return 'state inconsistent: final_pending without a final commit'
    return ''


def track_load_check(state, log):
    """Abandon a live track whose state cannot be worked (a hand edit, a kit
    bug), with a banner, so track_direction never meets a bad index."""
    for trk in state_defaults(state)['tracks']:
        if trk.get('status') not in TRACK_LIVE:
            continue
        bad = _track_problem(trk)
        if bad:
            trk['status'], trk['reason'] = 'abandoned', bad
            banner(log, 'TRACK %s ABANDONED: %s' % (trk.get('id'), bad))


def failed_attempts(step):
    return [a for a in step.get('attempts') or []
            if a.get('outcome') in TRACK_FAILED or a.get('counted')]


def tracks_text(state, live=None):
    """The TRACKS section of the planner prompt: the live track with its
    steps, predictions against measurements and what it has left; or the
    last closed track and why; or 'none'."""
    lines = []
    if not CONFIG.get('tracks_enabled', True):
        lines.append('(tracks are switched off in the config; none can be opened or worked)')
    trk = live if live is not None else open_track(state)
    if trk is not None and trk.get('status') in TRACK_LIVE:
        att_max = int(CONFIG.get('track_max_attempts_per_step', 2))
        lines.append('open track %s: %s' % (trk['id'], trk.get('goal', '')))
        if trk.get('why'):
            lines.append('  why: %s' % trk['why'][:400])
        lines.append('  opened at i%s on %s (%.3f B/cycle); %d of %d iterations used'
                     % (trk.get('opened_iteration'), str(trk.get('base_commit'))[:10],
                        trk.get('base_bpc') or 0, trk.get('iterations_used') or 0,
                        int(CONFIG.get('track_max_iterations', 8))))
        for st in trk['steps']:
            meas = [a.get('measured_bpc') for a in st.get('attempts') or []
                    if a.get('measured_bpc') is not None]
            bits = ['  step %d [%s] %s%s: %s' % (
                st['n'], st['kind'], st.get('status'),
                ' (current)' if st['n'] == trk.get('current') else '', st['text'][:200])]
            if st.get('predicted_bpc') is not None:
                bits.append('predicted %.2f B/cycle' % st['predicted_bpc'])
            if meas:
                bits.append('measured %s' % ', '.join('%.2f' % v for v in meas))
            fails = failed_attempts(st)
            if st['n'] == trk.get('current') and trk.get('status') == 'open':
                bits.append('%d of %d attempts failed' % (len(fails), att_max))
            for a in fails[-2:]:
                # withheld: an oracle failure's reason names a held-out
                # table and quotes its bytes.
                bits.append('i%s %s: %s' % (a.get('iteration'), a.get('outcome'),
                                            withheld(a.get('reason'))[:160]))
            lines.append('; '.join(bits))
        if trk.get('status') == 'final_pending':
            fin = trk.get('final') or {}
            lines.append('  the final step is correct and beat the best by %+.1f%% at i%s, '
                         'but a sibling won that iteration; it is compared with the best '
                         'again at the start of the next one'
                         % (fin.get('gain_pct') or 0, fin.get('iteration')))
        return '\n'.join(lines)
    closed = [t for t in (state or {}).get('tracks') or [] if t.get('status') not in TRACK_LIVE]
    if closed:
        last = closed[-1]
        lines.append('no open track. The last one, %s (%s), %s at i%s: %s'
                     % (last.get('id'), (last.get('goal') or '')[:160], last.get('status'),
                        last.get('closed_iteration'), withheld(last.get('reason'))[:400]))
    else:
        lines.append('none')
    return '\n'.join(lines)


def read_track_spec(trk, cwd=None):
    """The track's SPEC.md at its head, capped. Raises GitError: a build step
    without its spec cannot be briefed, and its slot is suspended."""
    return git(['show', '%s:docs/track-%s/SPEC.md' % (trk['head'], trk['id'])],
               cwd=cwd)[:SPEC_CAP]


def prev_step_notes(trk):
    """The notes of the passed steps, oldest first, capped."""
    parts = ['step %d (%s): %s' % (st['n'], st['kind'], st.get('notes') or '(no notes)')
             for st in trk.get('steps') or [] if st.get('status') == 'passed']
    return '\n'.join(parts)[-PREV_NOTES_CAP:]


def track_direction(trk, spec='', prev_notes=''):
    """The direction a track step's session is given. direction['track'] is
    everything the track's brief needs (propose.track_system_text)."""
    st = trk['steps'][trk['current']]
    m = len(trk['steps']) - 1
    if st['kind'] == 'design':
        focus = 'track %s, the design step: SPEC.md and steps.json for %s' % (
            trk['id'], trk.get('goal', ''))
        hyp = trk.get('why') or 'see the track brief'
    else:
        focus = 'track %s, step %d of %d (%s): %s' % (trk['id'], st['n'], m, st['kind'],
                                                      st['text'])
        hyp = ('predicted %.2f B/cycle after this step' % st['predicted_bpc']
               if st.get('predicted_bpc') is not None
               else 'checked for correctness only')
    return {'focus': focus[:400], 'hypothesis': hyp[:600], 'skill_ids': [],
            'where_to_look': (st.get('module') or 'docs/track-%s/SPEC.md' % trk['id'])[:300],
            'risk': '', 'roadmap_step': None, 'modelled_gain_pct': None,
            'track': {'id': trk['id'], 'goal': trk.get('goal', ''), 'why': trk.get('why', ''),
                      'branch': trk['branch'], 'n': st['n'], 'm': m, 'kind': st['kind'],
                      'text': st['text'], 'predicted_bpc': st.get('predicted_bpc'),
                      'calib_commit': trk.get('calib_commit'),
                      'base_bpc': trk.get('base_bpc'), 'planned': trk.get('planned') or [],
                      'spec': (spec or '')[:SPEC_CAP],
                      'prev_notes': (prev_notes or '')[-PREV_NOTES_CAP:]}}


def slim_direction(direction):
    """A track direction as records and pending slots keep it: without the
    spec, the notes and the plan, which live in git and in the track."""
    out = dict(direction or {})
    if isinstance(out.get('track'), dict):
        out['track'] = dict((k, v) for k, v in out['track'].items()
                            if k not in ('spec', 'prev_notes', 'planned'))
    return out


def track_mark(asg):
    return {'id': asg.get('track_id'), 'step': asg.get('step'), 'kind': asg.get('kind')}


def track_history_line(k, c):
    """e.g. '- i7 t2a1 track my-track step 2 (ports): passed -- byte-exact'."""
    tr = c.get('track') or {}
    line = '- i%s %s track %s step %s (%s): %s' % (
        k, c.get('label'), tr.get('id'), tr.get('step'), tr.get('kind'),
        TRACK_WORDS.get(c.get('outcome'), c.get('outcome')))
    if c.get('reason'):
        line += ' -- ' + withheld(c['reason'])[:160]
    return line


def judge_session(sess, has_commit, kind):
    """(outcome, reason, counts_as_attempt, uses_an_iteration) at the session
    level, or (None, '', True, True) when what was committed is judged next.

      limit, no commit           kept pending, continued after the reset
      error/timeout/budget, none track_session_failed (not an attempt)
      any status with a commit   judged: a cut-off session's work is real
      ok, no commit              a failed attempt ('no change made')
    """
    status = (sess or {}).get('status')
    if has_commit:
        return None, '', True, True
    if status == 'limit':
        return ('track_pending', 'the usage window ran out; the session is continued '
                'after the reset', False, False)
    if status in ('error', 'timeout', 'budget'):
        return ('track_session_failed', 'the session ended (%s) without committing '
                'anything' % status, False, True)
    if kind == 'design':
        return 'track_design_invalid', 'the design session wrote no spec', True, True
    return 'track_step_failed', 'no change made', True, True


def judge_design(worktree, trk, model_fn, min_gain_pct=None):
    """(ok, note, steps) for a design step's docs. Never measured.

    SPEC.md of at least 1500 characters; a steps.json of 1 to track_max_steps
    steps of valid kinds, ending with a throughput step; every throughput step
    with a prediction, and the final one must beat the base the track opened
    on. model_fn is unused in this kit."""
    tid = trk['id']
    docs = os.path.join(worktree, 'docs', 'track-' + tid)
    try:
        with open(os.path.join(docs, 'SPEC.md'), encoding='utf-8', errors='replace') as fil:
            spec = fil.read()
    except IOError:
        return False, 'no docs/track-%s/SPEC.md' % tid, None
    if len(spec.strip()) < SPEC_MIN_CHARS:
        return False, 'SPEC.md has %d characters; at least %d are needed' % (
            len(spec.strip()), SPEC_MIN_CHARS), None
    try:
        raw = read_json(os.path.join(docs, 'steps.json'), None)
    except ValueError as exc:
        return False, 'steps.json is not JSON: %s' % str(exc)[:120], None
    if raw is None:
        return False, 'no docs/track-%s/steps.json' % tid, None
    if isinstance(raw, dict):
        raw = raw.get('steps')
    steps, why = propose.sanitize_track_steps(raw, int(CONFIG.get('track_max_steps', 6)))
    if steps is None:
        return False, 'steps.json: %s' % why, None
    for j, st in enumerate(steps, 1):
        if st['kind'] == 'throughput' and not st['predicted_bpc']:
            return False, 'step %d (throughput) has no predicted_bpc' % j, None
    gain = float(CONFIG['min_gain_pct'] if min_gain_pct is None else min_gain_pct)
    need = (trk.get('base_bpc') or 0) * (1.0 + gain / 100.0)
    if steps[-1]['predicted_bpc'] <= need:
        return False, ('the final step predicts %.2f B/cycle, not above the %.2f the track '
                       'opened on' % (steps[-1]['predicted_bpc'], trk.get('base_bpc') or 0)), None
    out = []
    for j, st in enumerate(steps, 1):
        out.append({'n': j, 'kind': st['kind'], 'text': st['text'],
                    'predicted_bpc': st['predicted_bpc'], 'widths': st['widths'],
                    'module': st.get('module') or '', 'model_bpc': st.get('model_bpc'),
                    'source': 'spec', 'status': 'pending', 'ref': None, 'attempts': [],
                    'notes': ''})
    note = '%d steps accepted' % len(out)
    return True, note, out


def judge_step(step, metrics, tol_pct=None):
    """(outcome, reason) for a measured non-final step. Simulation only: a
    synthesis result, when there is one, is information and never judged.

      measure_error            track_never_measured (not an attempt)
      compile error / a draw not byte-exact     track_step_failed
      golden or ports, byte-exact               track_step_passed
      throughput, >= (1 - tol) x predicted      track_step_passed
      throughput, below                         track_step_short
    """
    tol = float(CONFIG.get('track_bpc_tolerance_pct', 10.0) if tol_pct is None else tol_pct)
    metrics = metrics or {}
    if metrics.get('measure_error'):
        return 'track_never_measured', metrics['measure_error']
    if metrics.get('error'):
        return 'track_step_failed', 'does not compile: %s' % str(metrics['error'])[:200]
    if not metrics.get('oracle_pass'):
        return 'track_step_failed', metrics.get('first_problem') or 'not byte-exact'
    if step['kind'] in ('golden', 'ports'):
        return 'track_step_passed', 'byte-exact on every draw (checked for correctness only)'
    bpc, pred = metrics.get('bytes_per_cycle'), step.get('predicted_bpc')
    if bpc is None:
        return 'track_step_failed', 'no bytes/cycle was measured'
    if not pred:
        return 'track_step_passed', 'byte-exact at %.3f B/cycle (no prediction to meet)' % bpc
    ratio = bpc / pred
    if ratio >= 1.0 - tol / 100.0:
        return 'track_step_passed', 'byte-exact at %.3f B/cycle, %.2f of its prediction %.2f%s' % (
            bpc, ratio, pred, '; model pessimistic' if ratio > 1.0 + tol / 100.0 else '')
    return 'track_step_short', '%.3f B/cycle is %.2f of its prediction %.2f (needs %.2f)' % (
        bpc, ratio, pred, 1.0 - tol / 100.0)


def step_changes(worktree, head, commit):
    """(top-level rtl files changed, unit test names changed) from the
    track's head to a step's commit."""
    files = git(['diff', '--name-only', head, commit], cwd=worktree).splitlines()
    top = [f for f in files if f.startswith('rtl/') and not f.startswith('rtl/unit/')]
    units = []
    for f in files:
        m = UNIT_FILE.match(f)
        if m and os.path.isfile(os.path.join(worktree, f)):
            units.append(m.group(1))
    return top, units


def run_unit_check(worktree, name):
    """(passed, last line) of `python agentic/check.py --unit <name>` in a
    track worktree, with the current harness synced into it."""
    try:
        propose.sync_harness(worktree, os.path.normpath(worktree) + '.harness.json')
    except Exception:
        pass
    res = tools_run([sys.executable, os.path.join('agentic', 'check.py'), '--unit', name],
                    timeout=UNIT_TIMEOUT_S, cwd=worktree)
    lines = [ln for ln in (res.text or '').splitlines() if ln.strip()]
    return res.ok, (lines[-1] if lines else ('timed out' if res.timed_out else 'no output'))


def track_pre_measure(trk, c, asg, model_fn, unit_fn=None, log=None):
    """What the track slot's candidate needs before anything is measured:
    {'judged': outcome, 'reason', ...} when it is judged here (session-level
    results, the design step, a golden or ports step that changed only
    rtl/unit/), else {'measure': True, 'synth': bool}."""
    unit_fn = unit_fn or run_unit_check
    st = trk['steps'][trk['current']]
    final = trk['current'] == len(trk['steps']) - 1
    outcome, reason, attempt, used = judge_session(c.get('session'), bool(c.get('commit')),
                                                   st['kind'])
    if outcome:
        return {'judged': outcome, 'reason': reason, 'attempt': attempt, 'used': used}
    if st['kind'] == 'design':
        ok, note, steps = judge_design(asg['worktree'], trk, model_fn)
        if ok:
            return {'judged': 'track_design_done', 'reason': note, 'spec_steps': steps}
        return {'judged': 'track_design_invalid', 'reason': note}
    try:
        top, units = step_changes(asg['worktree'], trk['head'], c['commit'])
    except GitError as exc:
        return {'judged': 'track_never_measured', 'reason': 'git: %s' % str(exc)[:200]}
    if st['kind'] in ('golden', 'ports') and not top:
        # Only rtl/unit/ (or docs) changed: the design the draws would
        # simulate is byte for byte the track's head. Its unit tests are what
        # this step built, so they are what is checked.
        if not units:
            return {'judged': 'track_step_failed',
                    'reason': 'changed no rtl/ file and no unit test'}
        bad = []
        for name in units:
            ok, last = unit_fn(asg['worktree'], name)
            if log:
                log('  %s: unit test %s: %s' % (c.get('label'), name, last[:160]))
            if not ok:
                bad.append('%s: %s' % (name, last[:120]))
        if bad:
            return {'judged': 'track_step_failed', 'reason': 'unit test failed: ' + '; '.join(bad)}
        return {'judged': 'track_step_passed',
                'reason': 'unit test%s %s pass%s (only rtl/unit/ changed)' % (
                    '' if len(units) == 1 else 's', ', '.join(units),
                    'es' if len(units) == 1 else '')}
    return {'measure': True, 'synth': bool(final or CONFIG.get('track_synth_steps', False))}


def track_after_measure(trk, c, metrics, rtl_dir, base_ram, log=None):
    """(outcome, reason) for a measured track candidate, scored already by
    score_candidate (c['track_final_ok'] says whether evaluate found it
    adoptable). A final step's outcome is 'final_ok' when it beats the best;
    the caller turns that into adopted or final_pending after selection."""
    st = trk['steps'][trk['current']]
    final = trk['current'] == len(trk['steps']) - 1
    mine_ram = ram_latency(rtl_dir)
    # With track_synth_steps on, a non-final step is synthesised too, but
    # only as information: a synthesis error or an unreachable host leaves
    # its simulation verdict as it is (plan 5.4), not a re-measure.
    sim_only = not final and synth_only_failure(metrics)
    if not sim_only and (c.get('outcome') == 'measure_error' or metrics.get('measure_error')
                         or never_measured(c)):
        c['track_final_ok'] = False
        return 'track_never_measured', (c.get('reason') or metrics.get('measure_error') or '')[:300]
    if sim_only:
        metrics = dict(metrics, measure_error=None, synth_error=None)
    if metrics.get('oracle_pass') and (mine_ram != base_ram
                                       or c.get('outcome') == 'ram_latency_changed'):
        c['track_final_ok'] = False
        return 'track_step_failed', ('the simulation RAM changed (%s -> %s); synthesis uses a '
                                     'fixed memory' % (base_ram, mine_ram))
    if not final:
        return judge_step(st, metrics)
    if metrics.get('error') or not metrics.get('oracle_pass'):
        c['track_final_ok'] = False
        return 'track_step_failed', (c.get('reason') or 'not byte-exact')[:300]
    if c.get('outcome') == 'failed_synth':
        c['track_final_ok'] = False
        return 'track_step_failed', 'synthesis failed: %s' % (c.get('reason') or '')[:240]
    pred, bpc = st.get('predicted_bpc'), metrics.get('bytes_per_cycle')
    if log and pred and bpc:
        log('  %s: final step at %.2f of its prediction (%.3f vs %.2f B/cycle)'
            % (c.get('label'), bpc / pred, bpc, pred))
    gain = (c.get('measured') or {}).get('gain_pct')
    if c.get('track_final_ok'):
        return 'final_ok', 'byte-exact; beats the best by %+.2f%%' % (gain or 0)
    return 'track_final_not_better', 'byte-exact, but %s' % (c.get('reason') or
                                                             'not better than the best')


def make_step_ref(trk, n, commit, cwd=None):
    """Point the step's ref at its commit. Done before the save: the saved
    state names the ref, and a ref to a commit is harmless if the save never
    happens."""
    ref = step_ref(trk, n)
    git(['branch', '-f', ref, commit], cwd=cwd)
    return ref


def abandon_track(trk, reason, k, events, log):
    trk.update(status='abandoned', reason=reason, closed_iteration=k)
    ps = trk.get('pending_slot') or {}
    trk['pending_slot'] = None
    if ps.get('worktree'):
        try:
            remove_worktree(ps['worktree'])
        except GitError:
            pass
    banner(log, 'TRACK %s ABANDONED: %s' % (trk['id'], reason))
    events.append({'kind': 'track_abandon', 'track': trk['id'], 'reason': reason,
                   'iteration': k})


def settle_track(trk, c, asg, outcome, reason, k, events, log, used=True, metrics=None,
                 spec_steps=None, cwd=None):
    """Apply one judged track slot to the working copy ``trk`` (never to
    state): the attempt, a pass (step ref, head, current), a failure count,
    the final step's fate, and the abandon rules, checked after judging so a
    final step judged in the last allowed iteration still counts. Sets the
    candidate's outcome and reason; appends events."""
    st = trk['steps'][trk['current']]
    c['outcome'], c['reason'] = outcome, reason
    c['adoptable'] = False if outcome != 'adopted' else c.get('adoptable')
    if outcome == 'track_pending':
        trk['pending_slot'] = {'label': asg['label'], 'worktree': asg['worktree'],
                               'branch': asg['branch'], 'start': asg.get('start'),
                               'step': st['n'], 'track_id': trk['id'], 'kind': st['kind']}
        log('  %s: track %s step %d cut off by the usage limit; its worktree is kept and the '
            'session is continued next iteration' % (c['label'], trk['id'], st['n']))
        events.append({'kind': 'track_pending', 'track': trk['id'], 'step': st['n'],
                       'label': c['label'], 'iteration': k})
        return
    if used:
        trk['iterations_used'] = (trk.get('iterations_used') or 0) + 1
    metrics = metrics or {}
    att = {'iteration': k, 'label': c.get('label'), 'branch': c.get('branch'),
           'commit': c.get('commit'), 'outcome': outcome,
           'measured_bpc': metrics.get('bytes_per_cycle'),
           'f_max_mhz': metrics.get('f_max_mhz'), 'reason': (reason or '')[:400],
           'cost_usd': round((c.get('session') or {}).get('cost_usd') or 0, 2),
           'files': (c.get('files_changed') or [])[:40]}
    st.setdefault('attempts', []).append(att)
    log('  %s: TRACK %s step %d (%s): %s -- %s' % (
        c.get('label'), trk['id'], st['n'], st['kind'], TRACK_WORDS.get(outcome, outcome),
        (reason or '')[:200]))
    if outcome == 'track_never_measured':
        trk['never_measured_streak'] = (trk.get('never_measured_streak') or 0) + 1
        if trk['never_measured_streak'] > int(CONFIG.get('track_max_never_measured', 2)):
            att['counted'] = True
            trk['never_measured_streak'] = 0
            log('  %s: never measured %d times in a row; this one counts as a failed attempt'
                % (c.get('label'), int(CONFIG.get('track_max_never_measured', 2)) + 1))
    elif outcome != 'track_session_failed':
        trk['never_measured_streak'] = 0
    ev = {'kind': 'track_attempt', 'track': trk['id'], 'step': st['n'], 'label': c.get('label'),
          'outcome': outcome, 'reason': (reason or '')[:300], 'iteration': k}
    if outcome in TRACK_PASSED or outcome in ('adopted', 'track_final_pending'):
        try:
            st['ref'] = make_step_ref(trk, st['n'], c['commit'], cwd=cwd)
        except GitError as exc:
            log('  could not set the step ref: %s' % str(exc)[:160])
    if outcome in TRACK_PASSED:
        st['status'] = 'passed'
        notes = c.get('notes') or ''
        st['notes'] = ('\n'.join(x for x in (propose.notes_section(notes, 'how it works'),
                                             propose.notes_section(notes, 'what i tried'))
                                 if x) or notes)[:STEP_NOTES_CAP]
        trk['head'] = c['commit']
        if outcome == 'track_design_done':
            trk['steps'] = [st] + list(spec_steps or [])
        trk['current'] = st['n'] + 1
        ev['kind'] = 'track_pass'
    elif outcome == 'track_design_invalid':
        trk['design_failures'] = (trk.get('design_failures') or 0) + 1
    elif outcome == 'adopted':
        st['status'] = 'passed'
        trk.update(status='adopted', head=c['commit'], closed_iteration=k,
                   reason='adopted at i%d: %s' % (k, reason))
        ev['kind'] = 'track_final'
        banner(log, 'TRACK %s ADOPTED: %s' % (trk['id'], reason))
    elif outcome == 'track_final_pending':
        st['status'] = 'passed'
        trk.update(status='final_pending', head=c['commit'],
                   final={'commit': c['commit'], 'branch': c.get('branch'),
                          'metrics': best_view(metrics), 'iteration': k,
                          'notes': (c.get('notes') or '')[:STEP_NOTES_CAP],
                          'gain_pct': (c.get('measured') or {}).get('gain_pct')})
        ev['kind'] = 'track_final'
        log('  TRACK %s: the final step beats the best but a sibling won; it is compared '
            'with the best again next iteration' % trk['id'])
    elif outcome == 'track_final_not_better':
        st['status'] = 'passed'
        trk.update(status='finished', head=c['commit'], closed_iteration=k,
                   reason='the final step is correct but not better than the best: %s'
                          % (reason or '')[:300])
        ev['kind'] = 'track_final'
        banner(log, 'TRACK %s FINISHED without adoption: %s' % (trk['id'], reason))
    events.append(ev)
    if trk['status'] != 'open':
        return
    att_max = int(CONFIG.get('track_max_attempts_per_step', 2))
    st = trk['steps'][min(trk['current'], len(trk['steps']) - 1)]
    fails = failed_attempts(st)
    if st['kind'] == 'design' and (trk.get('design_failures') or 0) >= att_max:
        abandon_track(trk, 'the design was judged invalid %d times: %s' % (
            trk['design_failures'], '; '.join((a.get('reason') or '')[:120] for a in fails)),
            k, events, log)
    elif len(fails) >= att_max:
        what = ('short' if all(a.get('outcome') == 'track_step_short' for a in fails)
                else 'failed')
        meas = [a.get('measured_bpc') for a in fails if a.get('measured_bpc') is not None]
        detail = ('%s vs %.2f predicted' % (', '.join('%.2f' % v for v in meas),
                                             st['predicted_bpc'])
                  if what == 'short' and meas and st.get('predicted_bpc')
                  else '; '.join((a.get('reason') or '')[:120] for a in fails))
        abandon_track(trk, 'step %d (%s) %s %d times: %s' % (
            st['n'], st['kind'], what, len(fails), detail), k, events, log)
    elif (trk.get('iterations_used') or 0) >= int(CONFIG.get('track_max_iterations', 8)):
        abandon_track(trk, 'used its %d iterations at step %d of %d' % (
            trk['iterations_used'], st['n'], len(trk['steps']) - 1), k, events, log)


def replaced_adoptions(state, trk):
    """The adoptions made since the track opened. A track is built on the
    best of the day it opened, so adopting it resets the run to the track's
    commit and drops these; rebasing the track instead would break its step
    predictions, so the loop only says so, loudly."""
    if state['best'].get('commit') == trk.get('base_commit'):
        return []
    out = []
    for it in state.get('iterations') or []:
        win = it.get('winner') or {}
        if it.get('iteration', 0) >= (trk.get('opened_iteration') or 0) and win.get('commit'):
            out.append('i%s %s' % (it['iteration'], win.get('id')))
    return out


def warn_replaced(state, trk, log):
    gone = replaced_adoptions(state, trk)
    if gone:
        banner(log, 'TRACK %s replaces the adoptions made while it was open: %s (the best '
               'moved from %s; the track was built on %s)'
               % (trk['id'], ', '.join(gone), str(state['best'].get('commit'))[:10],
                  str(trk.get('base_commit'))[:10]))
    return gone


def admit_track_candidate(trk, c):
    """After score_candidate: a track candidate is never adoptable as such.
    Only a FINAL step that evaluate found adoptable is marked
    track_final_ok, the one path by which a track enters selection; a
    partial step that happens to beat the best (it may, with
    track_synth_steps on) is a half-built design and is never adopted."""
    final = trk['current'] == len(trk['steps']) - 1
    c['track_final_ok'] = bool(final and c.get('adoptable'))
    c['adoptable'] = False


def select_winner(cands):
    """The adoptable candidate with the best score, or None: the normal
    adoptable ones plus a track's final step admitted above."""
    pool = [c for c in cands if (c.get('adoptable') and not c.get('track'))
            or c.get('track_final_ok')]
    return min(pool, key=lambda c: c['score']) if pool else None


def apply_track_events(state, events):
    """Put each 'track_state' event's track into state['tracks'] (replacing
    the one with its id). Called only from append_record."""
    tracks = state_defaults(state)['tracks']
    for ev in events or []:
        if ev.get('kind') != 'track_state' or not isinstance(ev.get('track'), dict):
            continue
        new = copy.deepcopy(ev['track'])
        for j, old in enumerate(tracks):
            if old.get('id') == new.get('id'):
                tracks[j] = new
                break
        else:
            tracks.append(new)


def move_track_branches(state, ids, log, cwd=None):
    """Point each named live track's branch at its head (after the save)."""
    for tid in ids:
        trk = find_track(state, tid)
        if not trk or not trk.get('head') or not trk.get('branch'):
            continue
        try:
            git(['branch', '-f', trk['branch'], trk['head']], cwd=cwd)
        except GitError as exc:
            log('  could not move track branch %s: %s' % (trk['branch'], str(exc)[:160]))


def reconcile_tracks(state, log, cwd=None):
    """On resume: state is the truth. Each live track's branch is reset to
    its head, and every recorded step ref to its commit. A crash after the
    save leaves a branch behind (it catches up here); a crash before it
    leaves head at a commit a step ref or the base already holds."""
    for trk in state_defaults(state)['tracks']:
        refs = []
        for st in trk.get('steps') or []:
            done = [a for a in st.get('attempts') or []
                    if a.get('commit') and (a.get('outcome') in TRACK_PASSED
                                            or a.get('outcome') in ('adopted',
                                                                    'track_final_pending'))]
            if st.get('ref') and done:
                refs.append((st['ref'], done[-1]['commit']))
        if trk.get('status') in TRACK_LIVE and trk.get('head') and trk.get('branch'):
            refs.append((trk['branch'], trk['head']))
        for ref, commit in refs:
            try:
                if git_ok(['rev-parse', '--verify', commit + '^{commit}'], cwd=cwd):
                    git(['branch', '-f', ref, commit], cwd=cwd)
                else:
                    log('  track %s: commit %s of %s is gone' % (trk.get('id'), commit[:10], ref))
            except GitError as exc:
                log('  track %s: could not reset %s: %s' % (trk.get('id'), ref, str(exc)[:160]))


def working_track(state, pending, log):
    """A copy of the live track for this iteration to work on, or None. An
    attempt the usage window interrupted left its own copy in the pending
    dict (a track it opened or changed and never recorded); that copy is
    used unless the recorded track was closed meanwhile."""
    track_load_check(state, log)
    snap = pending.get('track') if isinstance(pending.get('track'), dict) else None
    if snap and snap.get('status') in TRACK_LIVE and not _track_problem(snap):
        rec = find_track(state, snap.get('id'))
        if rec is None or rec.get('status') in TRACK_LIVE:
            return copy.deepcopy(snap)
    live = open_track(state)
    return copy.deepcopy(live) if live else None


def prepare_track_slot(run, trk, k, iter_dir, log):
    """The track's slot this iteration: {'mode': 'session', 'asg'} for a
    session on the track branch, {'mode': 'remeasure', 'asg', 'cand'} to
    measure a never-measured step's commit again with no session and no
    cost, or None when git failed (the slot is suspended this iteration and
    the spare direction runs instead)."""
    st = trk['steps'][trk['current']]
    meta = {'track_id': trk['id'], 'step': st['n'], 'kind': st['kind']}
    try:
        spec = '' if st['kind'] == 'design' else read_track_spec(trk)
        direction = track_direction(trk, spec, prev_step_notes(trk))
        ps = trk.get('pending_slot') or {}
        trk['pending_slot'] = None
        if ps.get('step') == st['n'] and os.path.isdir(ps.get('worktree') or ''):
            trk['suspended'] = 0
            log('  track %s: continuing step %d in the worktree the usage limit interrupted'
                % (trk['id'], st['n']))
            return {'mode': 'session', 'asg': dict(
                {'label': ps['label'], 'worktree': ps['worktree'], 'branch': ps['branch'],
                 'start': ps.get('start')}, direction=direction, resumed=True, **meta)}
        label = 't%da%d' % (st['n'], len(st.get('attempts') or []) + 1)
        last = (st.get('attempts') or [None])[-1]
        if last and last.get('outcome') == 'track_never_measured' and last.get('commit') \
                and git_ok(['rev-parse', '--verify', last['commit'] + '^{commit}']):
            asg = new_assignment(run.state, k, label, direction, last['commit'], iter_dir)
            asg.update(meta)
            cand = {'label': label, 'branch': asg['branch'],
                    'direction': slim_direction(direction), 'track': track_mark(asg),
                    'id': 'track-%s-s%d-remeasure' % (trk['id'], st['n']),
                    'rationale': 'measured again: never measured at i%s' % last.get('iteration'),
                    'expected_gain_pct': None, 'expected_effect': '', 'risk': '',
                    'skills_used': [], 'notes': '',
                    'session': {'status': 'remeasure', 'cost_usd': 0.0, 'turns': 0,
                                'seconds': 0.0, 'error': '', 'subtype': 'remeasure'},
                    'commit': last['commit'], 'files_changed': last.get('files') or [],
                    'outcome': '', 'reason': '', 'measured': {}, 'score': None,
                    'advantage': None, 'adopted': False}
            trk['suspended'] = 0
            return {'mode': 'remeasure', 'asg': asg, 'cand': cand}
        asg = new_assignment(run.state, k, label, direction, trk['head'], iter_dir)
        asg.update(meta)
        trk['suspended'] = 0
        return {'mode': 'session', 'asg': asg}
    except GitError as exc:
        trk['suspended'] = (trk.get('suspended') or 0) + 1
        log('TRACK %s slot suspended this iteration: %s' % (trk['id'], str(exc)[:240]))
        return None


def track_model_fn(run, trk):
    """An optional model for judging a design's predictions (none here)."""
    def model(specs):
        return run_packmodel(run.name, trk.get('calib_commit'), specs).get('extra') or {}
    return model


def adopted_unsaved(run, trk, k, events, log):
    """At the start of a redone iteration: the live track's final step is
    already the best (adopt() ran, then an interrupt came before the save
    that would have closed the track). Close it as adopted, with no session
    and no measurement, and return the adopted candidate for the record;
    else None. Without this the final step was built again and scored
    against itself (+0.00%) and the track recorded as not better."""
    state = run.state
    best = state.get('best') or {}
    mark = best.get('track') or {}
    if trk is None or trk.get('status') not in TRACK_LIVE or not mark or (
            mark.get('id') != trk.get('id')) or _track_problem(trk):
        return None
    st = trk['steps'][trk['current']]
    if trk['current'] != len(trk['steps']) - 1 or mark.get('step') != st['n']:
        return None
    m = best.get('metrics') or {}
    reason = ('already the best design: adopted at i%s, before an interrupt kept the '
              'track from being closed' % mark.get('iteration'))
    cand = {'label': mark.get('label') or 'tfinal', 'branch': None, 'id': best.get('id'),
            'direction': {'focus': 'track %s, its final step' % trk['id']},
            'track': {'id': trk['id'], 'step': st['n'], 'kind': st['kind']},
            'rationale': trk.get('goal', ''), 'expected_gain_pct': None,
            'skills_used': [], 'notes': '',
            'session': {'status': 'track_final', 'cost_usd': 0.0, 'turns': 0,
                        'seconds': 0.0, 'error': '', 'subtype': 'already_adopted'},
            'commit': best.get('commit'), 'files_changed': [], 'outcome': 'adopted',
            'reason': reason, 'measured': {}, 'score': None, 'advantage': None,
            'adopted': True}
    if trk.get('status') == 'open':
        # The attempt the interrupt kept out of state, as settle_track
        # would have recorded it.
        trk['iterations_used'] = (trk.get('iterations_used') or 0) + 1
        st.setdefault('attempts', []).append(
            {'iteration': k, 'label': cand['label'], 'branch': None,
             'commit': best.get('commit'), 'outcome': 'adopted',
             'measured_bpc': m.get('bytes_per_cycle'), 'f_max_mhz': m.get('f_max_mhz'),
             'reason': reason, 'cost_usd': 0.0, 'files': []})
        try:
            st['ref'] = make_step_ref(trk, st['n'], best['commit'])
        except GitError as exc:
            log('  could not set the step ref: %s' % str(exc)[:160])
    st['status'] = 'passed'
    trk.update(status='adopted', head=best.get('commit'), closed_iteration=k, final=None,
               reason='adopted at i%s (found already adopted on resume)' % mark.get('iteration'))
    banner(log, 'TRACK %s ADOPTED: %s' % (trk['id'], trk['reason']))
    events.append({'kind': 'track_final', 'track': trk['id'], 'outcome': 'adopted',
                   'iteration': k})
    return cand


def rescore_final_pending(run, trk, k, draws, events, log, ref_draws=None):
    """At the start of an iteration: a final step that beat the best but lost
    to a sibling is compared with the best again (no session, no
    measurement: throughput is absolute). Adopted if it still wins, after
    the same clean re-run without the probe every adoption gets; otherwise
    the track is finished. Returns the adopted candidate, or None."""
    state = run.state
    fin = trk.get('final') or {}
    st = trk['steps'][trk['current']]
    cand = {'label': 'tfinal', 'branch': fin.get('branch'), 'id': 'track-%s' % trk['id'],
            'direction': {'focus': 'track %s, its final step from i%s' % (trk['id'],
                                                                      fin.get('iteration'))},
            'track': {'id': trk['id'], 'step': st['n'], 'kind': st['kind']},
            'rationale': trk.get('goal', ''), 'expected_gain_pct': None,
            'skills_used': [], 'notes': fin.get('notes') or '',
            'session': {'status': 'track_final', 'cost_usd': 0.0, 'turns': 0,
                        'seconds': 0.0, 'error': '', 'subtype': 'rescore'},
            'commit': fin.get('commit'), 'files_changed': [], 'outcome': '', 'reason': '',
            'measured': {}, 'score': None, 'advantage': None, 'adopted': False}
    if fin.get('commit') == state['best']['commit']:
        # Adopted by an attempt at this iteration that was interrupted
        # before its record was saved.
        cand.update(adopted=True, outcome='adopted', reason='already the best design')
        trk.update(status='adopted', closed_iteration=k,
                   reason='adopted at i%d (found already adopted on resume)' % k)
        events.append({'kind': 'track_final', 'track': trk['id'], 'outcome': 'adopted',
                       'iteration': k})
        return cand
    fin_metrics, best_metrics = fin.get('metrics') or {}, state['best']['metrics']
    if ref_draws is not None:
        # A fast or clock iteration: both sides on the same draws.
        fin_metrics = subset_view(fin_metrics, ref_draws)
        best_metrics = subset_view(best_metrics, ref_draws)
    ev = evaluate(fin_metrics, best_metrics, state['goal'])
    best_ram = ram_latency(os.path.join(run.base_dir, 'rtl'))
    if ev['adoptable'] and ram_latency_at(fin['commit']) not in (best_ram,):
        ev['adoptable'] = False
        ev['reason'] = 'its simulation RAM differs from the current best\'s'
    cand.update(measured=ev['measured'], score=ev['score'])
    if not ev['adoptable']:
        trk.update(status='finished', closed_iteration=k,
                   reason='the final step no longer beats the best: %s' % (
                       ev.get('reason') or ev.get('outcome')))
        banner(log, 'TRACK %s FINISHED without adoption: %s' % (trk['id'], trk['reason']))
        events.append({'kind': 'track_final', 'track': trk['id'],
                       'outcome': 'track_final_not_better', 'iteration': k})
        return None
    path = os.path.join(run.dir, 'iter-%d' % k, 'tfinal')
    differs = None
    try:
        remove_worktree(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        git(['worktree', 'add', '--detach', path, fin['commit']])
        differs = probe_clean_check(os.path.join(path, 'rtl'), path + '-measure',
                                    fin.get('metrics') or {}, draws, sim_jobs(1), log)
    except GitError as exc:
        log('  could not check out the waiting final step (%s); compared again next '
            'iteration' % str(exc)[:160])
        return None
    finally:
        try:
            remove_worktree(path)
        except GitError:
            pass
    if differs:
        probe_disagreed(run, [], differs, log)
        # Measured again without the probe, through the never-measured path.
        st['attempts'].append({'iteration': k, 'label': 'tfinal', 'branch': fin.get('branch'),
                               'commit': fin['commit'], 'outcome': 'track_never_measured',
                               'reason': 'the probe changed its counters; measured again',
                               'measured_bpc': None, 'f_max_mhz': None, 'cost_usd': 0.0})
        trk.update(status='open', final=None)
        return None
    warn_replaced(state, trk, log)
    adopt(run, cand, fin.get('metrics') or {}, k, log)
    trk.update(status='adopted', closed_iteration=k,
               reason='adopted at i%d: still beats the best by %+.2f%%' % (
                   k, (ev['measured'] or {}).get('gain_pct') or 0))
    banner(log, 'TRACK %s ADOPTED: %s' % (trk['id'], trk['reason']))
    events.append({'kind': 'track_final', 'track': trk['id'], 'outcome': 'adopted',
                   'iteration': k})
    return cand


def apply_track_command(run, trk, cmd, k, events, log, dry_run=False):
    """The planner's track command: abandon the live track, or open one when
    none is live. Returns the working track (possibly new). In --dry-run no
    track is ever created; the loop says what it would open."""
    state = run.state
    if cmd.get('abandon') and trk is not None and trk.get('status') in TRACK_LIVE:
        if dry_run:
            log('dry run: would abandon track %s' % trk['id'])
        else:
            abandon_track(trk, 'the planner: %s' % cmd['abandon'], k, events, log)
    if cmd.get('open') and (trk is None or trk.get('status') not in TRACK_LIVE):
        if not CONFIG.get('tracks_enabled', True):
            log('  the planner asked for a track; tracks are switched off')
            return trk
        req, why = propose.sanitize_track_request(cmd['open'])
        if req is None:
            log('  the planner asked for a track that cannot be opened: %s' % why)
            return trk
        if dry_run:
            log('dry run: would open track %s (%s)' % (req['id'], req['goal'][:160]))
            return trk
        new = new_track(req, state, k)
        try:
            # State is the truth: a branch left by a crashed attempt is moved.
            git(['branch', '-f', new['branch'], new['head']])
        except GitError as exc:
            log('  could not open track %s: %s' % (new['id'], str(exc)[:200]))
            return trk
        banner(log, 'TRACK %s OPENED: %s (%d planned steps; design first)'
               % (new['id'], new['goal'][:200], len(new['planned'])))
        events.append({'kind': 'track_open', 'track': new['id'], 'iteration': k,
                       'goal': new['goal'][:200]})
        return new
    return trk


# Simulation and synthesis overlap inside measure, so they are not its parts.
TIMING_ORDER =(('packmodel_s', 'model'), ('plan_s', 'plan'), ('check_s', 'check'),
                ('write_s', 'write'),
                ('measure_s', 'measure'), ('sim_s', 'simulating'),
                ('synth_s', 'synthesising alongside'), ('learn_s', 'learn'))


def timing_text(timing):
    def fmt(sec):
        return '%.0f s' % sec if sec < 120 else '%.1f min' % (sec / 60.0)
    bits = ['%s %s' % (name, fmt(timing[key])) for key, name in TIMING_ORDER
            if timing.get(key) is not None]
    return ', '.join(bits) or 'nothing timed'


def pending_slot(asg):
    """What a pending dict keeps of one assignment: enough to continue the
    session in its worktree. A track slot keeps its track and step, not the
    spec, which is read again from the track's head."""
    slot = {'label': asg['label'], 'worktree': asg['worktree'], 'branch': asg['branch'],
            'direction': slim_direction(asg['direction']), 'start': asg.get('start')}
    for key in ('track_id', 'step', 'kind'):
        if asg.get(key) is not None:
            slot[key] = asg[key]
    return slot


def keep_track_slots(slots, trk, log):
    """Pending slots, without a track slot whose track was closed since (or
    whose step moved on); its worktree is removed."""
    out = []
    for slot in slots:
        tid = slot.get('track_id')
        if tid and (trk is None or trk.get('id') != tid or trk.get('status') != 'open'
                    or trk['steps'][trk['current']]['n'] != slot.get('step')):
            log('  dropping the interrupted session of track %s: the track was closed or '
                'moved on' % tid)
            try:
                remove_worktree(slot['worktree'])
            except GitError:
                pass
            git_ok(['branch', '-D', slot['branch']])
            continue
        out.append(slot)
    return out


def run_iteration(run, draws, skills_data, k, n_cands, dry_run=False, fake=False):
    state = run.state
    log = run.log
    timing = {}
    # Sessions the previous attempt at this iteration left half-finished when
    # the usage window ran out. Their worktrees were kept.
    pending = state.get('pending') or {}
    mine = pending if pending.get('iteration') == k else {}
    resuming = mine.get('slots')
    if resuming:
        resuming = [slot for slot in resuming if os.path.isdir(slot.get('worktree', ''))]
    state['pending'] = None
    goal = state['goal']
    # Roadmap and track events, applied to state only in append_record.
    events = []
    # The live track, as a copy this iteration works on.
    trk = working_track(state, mine, log)
    if resuming:
        resuming = keep_track_slots(resuming, trk, log)
    run.status.set(phase='plan', iteration=k, detail='choosing %d directions' % n_cands)
    log('== iteration %d of %d ==' % (k, state['max_iters']))
    kind = iteration_kind(state, k)
    it_draws = kind_draws(draws, kind)
    synth_on = kind in ('full', 'guard', 'clock')
    if kind != 'full':
        log('measurement: %s -- %d draws%s' % (
            kind.upper(), len(it_draws), ', with synthesis' if synth_on else ', no synthesis'))
    early = []
    if trk is not None and not dry_run:
        got = adopted_unsaved(run, trk, k, events, log)
        if got is None and trk['status'] == 'final_pending':
            got = rescore_final_pending(run, trk, k, draws, events, log,
                                        ref_draws=None if kind in ('full', 'guard')
                                        else it_draws)
        if got:
            early.append(got)
    parent = state['best']['metrics']
    log('best so far: %s' % progress_text(state))
    log('lever: %s -- %s' % (parent.get('lever', {}).get('lever'),
                             parent.get('lever', {}).get('reason')))

    ctx = run.context(skills_data, k, log, timing)
    ctx['check_text'] = run.check_text
    ctx['tracks_text'] = tracks_text(state, trk)
    note = kind_note(state, kind, k)
    if note:
        ctx['state_text'] = note + '\n\n' + ctx['state_text']
    if kind == 'clock':
        ctx['lever'] = {'lever': 'clock', 'reason': 'this is a CLOCK iteration: the last '
                        'guard found f_max lower than the guard before it'}
    elif kind == 'fast' and (ctx.get('lever') or {}).get('lever') == 'clock':
        ctx['lever'] = {'lever': 'bytes/cycle', 'reason': 'the profile points at the clock, '
                        'but this is a FAST iteration and only bytes/cycle is measured'}
    tracks_on = bool(CONFIG.get('tracks_enabled', True))
    track_cmd = {}
    if resuming:
        directions = [slot['direction'] for slot in resuming if not slot.get('track_id')]
        plan_res = {'status': 'reused', 'text': 'the plan from the interrupted attempt'}
    else:
        t0 = time.time()
        live = tracks_on and trk is not None and trk['status'] == 'open'
        if live and n_cands <= 1:
            # The one slot is the track's: a planner call would only choose
            # the spare direction, which runs if the track slot cannot.
            directions = [dict(propose.FALLBACK_DIRECTIONS[0], roadmap_step=None,
                               modelled_gain_pct=None)]
            plan_res = {'status': 'skipped', 'text': 'one slot, held by the open track',
                        'cost_usd': 0.0}
        elif fake:
            # --fake tests the loop, not the model: no paid planner call.
            directions, plan_res, track_cmd = propose.fake_plan_directions(
                n_cands, k, track_open=trk is not None)
        else:
            directions, plan_res, track_cmd = propose.plan_directions(
                ctx, n_cands, log, track_open=trk is not None)
        timing['plan_s'] = round(time.time() - t0, 1)
    vet_directions(state, directions, log)
    trk = apply_track_command(run, trk, track_cmd, k, events, log, dry_run)
    live = tracks_on and trk is not None and trk['status'] == 'open'
    for j, d in enumerate(directions, 1):
        log('  direction c%d: %s%s' % (j, d['focus'][:140],
                                       ('  [roadmap STEP %s]' % d['roadmap_step'])
                                       if d.get('roadmap_step') else ''))
    if live:
        st = trk['steps'][trk['current']]
        log('  track %s: step %d of %d (%s) takes the last slot; %d of %d iterations used'
            % (trk['id'], st['n'], len(trk['steps']) - 1, st['kind'],
               trk.get('iterations_used') or 0, int(CONFIG.get('track_max_iterations', 8))))
    if dry_run:
        log('dry run: stopping before any coding session')
        return None

    iter_dir = os.path.join(run.dir, 'iter-%d' % k)
    os.makedirs(iter_dir, exist_ok=True)
    write_json(os.path.join(iter_dir, 'plan.json'),
               {'directions': directions, 'call': call_record(plan_res),
                'track_command': track_cmd})
    start = git(['rev-parse', 'HEAD'], cwd=run.base_dir)
    assignments = []
    slot = None
    if resuming:
        log('continuing %d session(s) in the worktrees the usage limit '
            'interrupted' % len(resuming))
        assignments = [dict(s, resumed=True) for s in resuming]
        for asg in assignments:
            if asg.get('track_id'):
                st = trk['steps'][trk['current']]
                try:
                    spec = '' if st['kind'] == 'design' else read_track_spec(trk)
                except GitError:
                    spec = ''
                asg['direction'] = track_direction(trk, spec, prev_step_notes(trk))
    else:
        normal = list(directions)
        if live:
            slot = prepare_track_slot(run, trk, k, iter_dir, log)
            if slot is None:
                if (trk.get('suspended') or 0) >= 2:
                    abandon_track(trk, 'its slot could not be prepared %d times in a row'
                                  % trk['suspended'], k, events, log)
            else:
                # The track's step takes the place of the lowest-ranked one.
                normal = normal[:max(0, n_cands - 1)]
        try:
            for j, d in enumerate(normal, 1):
                assignments.append(new_assignment(state, k, 'c%d' % j, d, start, iter_dir))
        except GitError as exc:
            log('could not prepare a candidate worktree: %s' % exc)
            if slot:
                assignments.append(slot['asg'])
            for asg in assignments:
                try:
                    remove_worktree(asg['worktree'])
                except GitError:
                    pass
                git_ok(['branch', '-D', asg['branch']])
            return 'error'
        if slot and slot['mode'] == 'session':
            assignments.append(slot['asg'])
    remeasure = slot if slot and slot['mode'] == 'remeasure' else None
    record_dirs = [slim_direction(a['direction']) for a in assignments] or list(directions)

    def discard_all(spent_on=None):
        spent = round(sum((r['session'].get('cost_usd') or 0)
                          for r in (spent_on or [])), 2)
        if spent:
            state['discarded_usd'] = round((state.get('discarded_usd') or 0) + spent, 2)
            log('  discarding %d session(s) that produced nothing: $%.2f spent, '
                '$%.2f discarded so far this run'
                % (len(spent_on), spent, state['discarded_usd']))
        for asg in assignments + ([remeasure['asg']] if remeasure else []):
            try:
                remove_worktree(asg['worktree'])
            except GitError as exc:
                log('  (%s)' % exc)
            git_ok(['branch', '-D', asg['branch']])

    if not run.check_text:
        run.status.set(phase='check', detail='running the starting check once')
        t0 = time.time()
        run.check_text = starting_check(prepare_base(run, log), log)
        timing['check_s'] = round(time.time() - t0, 1)
        # Into this iteration's briefs too, not only the next one's.
        ctx['check_text'] = run.check_text
        if run.check_text:
            log('starting check passes; its output goes into every brief')
    results = []
    if assignments:
        run.status.set(phase='write', detail='%d coding sessions in parallel' % len(assignments))
        tracked = [a for a in assignments if a.get('track_id')]
        log('writing %d candidates in parallel (model %s, up to %d min each%s)...'
            % (len(assignments), CONFIG['model'], int(CONFIG['session_timeout_min']),
               ('; the track step up to %d min and $%.0f, the others $%.0f'
                % (int(CONFIG.get('track_session_timeout_min', 120)),
                   float(CONFIG.get('track_session_budget_usd', 40.0)),
                   float(CONFIG['session_budget_usd']))) if tracked else ''))
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
    if results and no_proposal and any(s == 'limit' for s in statuses):
        # The provider is refusing; nothing was produced. Do not spend an
        # iteration on it: wait for the reset and try this one again.
        msgs = ' '.join((r['session'].get('error') or '') for r in results)
        run.limit_message = msgs
        spent = round(sum((r['session'].get('cost_usd') or 0) for r in results), 2)
        state['discarded_usd'] = round((state.get('discarded_usd') or 0) + spent, 2)
        # The track as this attempt left it (opened, or a slot prepared)
        # goes with the slots: it is not recorded until the iteration is.
        state['pending'] = {'iteration': k, 'slots': [pending_slot(a) for a in assignments],
                            'track': copy.deepcopy(trk) if trk is not None else None}
        if remeasure:
            try:
                remove_worktree(remeasure['asg']['worktree'])
            except GitError:
                pass
            git_ok(['branch', '-D', remeasure['asg']['branch']])
        log('  keeping %d worktree(s) so the sessions can be continued after the '
            'wait ($%.2f spent so far on this attempt)' % (len(assignments), spent))
        return 'limit'
    if results and no_proposal and all(s not in ('ok', 'budget') for s in statuses):
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
    track_c = track_asg = None
    for c, asg in zip(cands, assignments):
        if asg.get('track_id'):
            track_c, track_asg = c, asg
    if remeasure:
        track_c, track_asg = remeasure['cand'], remeasure['asg']
        cands.append(track_c)
        assignments.append(track_asg)
        log('  %s: measuring track %s step %s again, with no session: it was never measured'
            % (track_c['label'], trk['id'], track_asg.get('step')))
    facts_before = list(state.get('design_facts') or [])
    keep_facts(state, cands)        # facts from track notes are kept too

    pre = None
    if track_c is not None:
        pre = track_pre_measure(trk, track_c, track_asg, track_model_fn(run, trk), log=log)

    # earlier rejected candidates with a big gain, re-merged onto the best;
    # not while the track measures a step, so no more designs simulate at
    # once than there are slots (the memory limit behind the rc-137 kills).
    if not fake and not (pre and pre.get('measure')):
        for cand, asg in retry_candidates(run, state, k, iter_dir, log):
            cands.append(cand)
            assignments.append(asg)

    # measure the ones that changed something
    to_measure = [(c, asg) for c, asg in zip(cands, assignments)
                  if c['commit'] and c['outcome'] != 'no_proposal'
                  and (c is not track_c or (pre or {}).get('measure'))]
    raw = {}
    guard_ev = None
    # A guard measures a best adopted in fast iterations alongside the
    # candidates; its verdict sets what they are scored against.
    guard_best = kind == 'guard' and not state['best'].get('guarded', True)
    run.status.set(phase='measure', detail='%s%d candidates: %s' % (
        '' if kind == 'full' else kind + ': ', len(to_measure),
        'simulate + synthesise' if synth_on else 'simulate %d draws' % len(it_draws)))
    ref = parent
    if kind in ('fast', 'clock'):
        ref = subset_view(parent, it_draws)
    if to_measure or guard_best:
        log('measuring %d candidate(s)%s...' % (
            len(to_measure), ' and the best design (guard)' if guard_best else ''))
        jobs = [(os.path.join(asg['worktree'], 'rtl'),
                 os.path.join(iter_dir, c['label'] + '-measure'), it_draws, 0,
                 synth_on and (pre['synth'] if c is track_c else True))
                for c, asg in to_measure]
        if guard_best:
            jobs.append((os.path.join(run.base_dir, 'rtl'),
                         os.path.join(iter_dir, 'guard-best-measure'), draws, 0, True))
        per = sim_jobs(len(jobs))
        jobs = [(j[0], j[1], j[2], per, j[4]) for j in jobs]
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
        for metrics in metrics_list:
            metrics['measured_on'] = kind
            if metrics.get('f_max_mhz') is not None:
                metrics['f_max_from'] = k
        guard_got = None
        if guard_best:
            guard_got = metrics_list.pop()
            guard_got['measured_on'] = 'guard'
            rmtree(os.path.join(iter_dir, 'guard-best-measure', 'sim'))
        if kind == 'guard':
            ref, guard_ev = settle_guard(run, guard_got, k, log)
        parent_ram = ram_latency(os.path.join(run.base_dir, 'rtl'))
        parent_model = ram_model_of(os.path.join(run.base_dir, 'rtl'))
        for (c, asg), metrics in zip(to_measure, metrics_list):
            if not synth_on:
                metrics = carry_clock(metrics, ref)
            raw[c['label']] = metrics
            sim_only = c is track_c and not pre['synth']
            # A step simulated without synthesis would read 'failed_synth'
            # here; its verdict comes from the track's own judging below.
            score_candidate(c, asg, metrics, ref, goal, parent_ram,
                            os.path.join(iter_dir, c['label'] + '-measure'),
                            (lambda m: None) if sim_only else log, parent_model)
            if sim_only:
                log('  %s: simulated only (a non-final track step is not synthesised): %s'
                    % (c['label'], ('byte-exact, %.3f B/cycle' % metrics['bytes_per_cycle'])
                       if metrics.get('oracle_pass') and metrics.get('bytes_per_cycle') is not None
                       else (metrics.get('first_problem') or metrics.get('error')
                             or metrics.get('measure_error') or 'no result')[:200]))
            if c is track_c:
                admit_track_candidate(trk, c)

    tverdict = None
    if track_c is not None:
        if 'judged' in pre:
            tverdict = (pre['judged'], pre['reason'])
        else:
            tverdict = track_after_measure(
                trk, track_c, raw.get(track_c['label']) or {},
                os.path.join(track_asg['worktree'], 'rtl'),
                ram_latency_at(trk['base_commit']), log)

    # select
    winner = select_winner(cands)
    if winner:
        asg = dict((c['label'], a) for c, a in to_measure)[winner['label']]
        run.status.set(phase='measure', detail='clean re-run of %s without the probe'
                       % winner['label'])
        differs = probe_clean_check(os.path.join(asg['worktree'], 'rtl'),
                                    os.path.join(iter_dir, winner['label'] + '-measure'),
                                    raw[winner['label']], it_draws,
                                    sim_jobs(1), log)
        if differs:
            probe_disagreed(run, cands, differs, log)
            winner = None
    advantages([c for c in cands if not c.get('track')])
    if winner:
        if winner is track_c:
            warn_replaced(state, trk, log)
        adopt(run, winner, raw[winner['label']], k, log)
    else:
        log('nothing adopted this iteration')
        # The coding sessions read the facts only through the guide, which
        # adopt() rebuilds. Without this, facts verified in an iteration that
        # adopted nothing reached the planner but not the next sessions --
        # and most iterations adopt nothing.
        if state.get('design_facts') != facts_before:
            run.write_guide(notes=state.get('guide_notes'), facts=state.get('design_facts'))
    if kind == 'guard':
        close_guard(state, k, log)
    elif kind == 'clock':
        state['clock_due'] = None

    # the track: the step's verdict, applied to the working copy
    if track_c is not None:
        outcome, reason = tverdict
        if (pre or {}).get('measure') and track_c.get('outcome') == 'measure_error' and (
                outcome not in TRACK_PASSED + TRACK_FAILED):
            outcome, reason = 'track_never_measured', track_c.get('reason') or reason
        if outcome == 'final_ok':
            outcome = 'adopted' if winner is track_c else 'track_final_pending'
        settle_track(trk, track_c, track_asg, outcome, reason, k, events, log,
                     used=pre.get('used', True), metrics=raw.get(track_c['label']),
                     spec_steps=pre.get('spec_steps'))
    if trk is not None:
        events.append({'kind': 'track_state', 'track': copy.deepcopy(trk)})

    # learn
    run.status.set(phase='learn', detail='updating the skill library')
    record_outcomes(skills_data, cands)
    t0 = time.time()
    changed, lessons = learn_step(ctx, cands, skills_data, iter_dir, log, fake)
    if not fake:
        timing['learn_s'] = round(time.time() - t0, 1)
    skills_mod.save(skills_data, run.skills_path)

    record = make_record(state, k, record_dirs, early + cands,
                         winner or (early[0] if early else None), changed, lessons, timing)
    if trk is not None:
        record['track'] = {'id': trk['id'], 'status': trk['status'],
                           'current': trk['current'],
                           'iterations_used': trk.get('iterations_used')}
    if kind != 'full':
        record['measure_kind'] = kind
        record['best_guarded'] = bool(state['best'].get('guarded', True))
    if guard_ev:
        record['guard'] = guard_ev
    append_record(run, record, log, events=roadmap_events(cands, k) + events)
    keep = (trk or {}).get('pending_slot') or {}
    for asg in assignments:
        if asg['worktree'] != keep.get('worktree'):
            remove_worktree(asg['worktree'])
    return 'adopted' if (winner or early) else 'none'


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
    # Sessions inherit the loop's environment.
    os.environ['AGENTIC_RUN'] = name
    log('agentic loop starting: run %s in %s' % (name, ROOT))
    problems = preflight(log, need_model=not args.dry_run)
    host_down = [p for p in problems if p.startswith('synth_backend is hacc but the host')]
    if problems and len(host_down) == len(problems) and not args.dry_run:
        # Only the synthesis host is missing (the VPN, usually): wait for it,
        # so a run started or reloaded while it is down begins when it is back.
        log('PROBLEM (waiting): %s' % host_down[0])
        run.status.set(phase='waiting', detail='synthesis host unreachable (VPN?)')
        while problems:
            time.sleep(HOST_POLL_S)
            problems = preflight(log, need_model=True)
        log('the synthesis host answers; starting')
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
        draws, problem = corpus_guard(state, draws, fresh, args.fake)
        if problem:
            log('PROBLEM: %s' % problem)
            state['stopped'] = 'corpus check failed'
            run.status.set(phase='failed', detail=problem[:200], finished=now_iso())
            return 1
        state['draw_names'] = sorted(d.name for d, _p, _c in draws)
        missing = [n for n in fast_names() if n not in state['draw_names']]
        if missing and int(CONFIG.get('guard_every') or 0) > 0:
            problem = ('fast_draws names %s, which the corpus does not have (it has %s)'
                       % (', '.join(missing), ', '.join(state['draw_names'])))
            log('PROBLEM: %s' % problem)
            state['stopped'] = 'corpus check failed'
            run.status.set(phase='failed', detail=problem[:200], finished=now_iso())
            return 1
        log('corpus: %d draws (%d scored, %d synthetic)%s' % (
            len(draws), sum(1 for d, _p, _c in draws if d.scored),
            sum(1 for d, _p, _c in draws if d.kind == 'synthetic'),
            ('; a --fake test cut (%s), every draw scored' % ', '.join(state['test_draws']))
            if state.get('test_draws') else ''))
        state_defaults(state)
        track_load_check(state, log)
        if not fresh:
            # State is the truth: each live track's branch and step refs go
            # back to what was saved (a crash between the save and the
            # branch move, or a branch moved by hand).
            reconcile_tracks(state, log)
        if state.get('probe_disabled'):
            measure.PROBE_OFF_REASON = 'disabled for this run: %s' % state['probe_disabled'][:200]
            log('the probe stays off for this run: %s' % state['probe_disabled'][:200])

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
                                   os.path.join(run.dir, 'baseline-measure'), draws,
                                   jobs=sim_jobs(1))
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
            base = best_view(base)
            state['baseline'] = base
            base['f_max_from'] = 0
            state['best'] = {'commit': state['base_commit'], 'metrics': base,
                             'iteration': 0, 'id': 'baseline', 'guarded': True}
            state['last_guarded'] = guard_snapshot(state, 0)
            run.set_progress()
            run.save()
            run.write_guide(facts=state.get('design_facts'))
            log('baseline: %s' % state_text(base, base).splitlines()[0])
            rmtree(os.path.join(run.dir, 'baseline-measure', 'sim'))
            calibrate_packmodel(run, log)
        elif not args.dry_run and not args.fake:
            # A resumed run: the best may predate the probe, and the packing
            # model's pointer may name an older best or none.
            probe_backfill(run, draws, log)
            calibrate_packmodel(run, log)
        else:
            calibrate_packmodel(run, log)

        if 'last_guarded' not in state and state['best'].get('guarded', True):
            state['last_guarded'] = guard_snapshot(state, len(state['iterations']))
        target = state['goal'].get('target_pct')
        since_winner = 0
        limit_waits = 0
        error_waits = 0
        t_start = time.time()
        k = len(state['iterations']) + 1
        while k <= state['max_iters']:
            gain = goal_gain(state['best']['metrics'], state['baseline'], state['goal'])
            if target and gain is not None and gain >= target:
                if state['best'].get('guarded', True):
                    state['stopped'] = 'target met: %+.2f%% on %s' % (gain, state['goal']['metric'])
                    break
                if not state.get('guard_due'):
                    # Met on a fast measurement only: confirm it on every
                    # draw, with synthesis, before stopping.
                    log('the goal is met on a fast measurement; iteration %d guards it' % k)
                    state['guard_due'] = True
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
            if (win.get('utilization') or 0) >= 0.9 and 0 < left < prewait_s(state) \
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
            if CONFIG['synth_backend'] == 'hacc' and not args.dry_run and not args.fake:
                wait_for_host(run, state, log)
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


def prewait_s(state):
    """How close to its reset a nearly spent usage window must be for the
    loop to wait instead of starting sessions: the longest session the next
    iteration could run (a track step's, while a track is open), capped at an
    hour. Not the full two hours of a track session: one cut off by the limit
    is kept pending and continued, so waiting longer costs more than it
    saves."""
    longest = int(CONFIG['session_timeout_min'])
    if CONFIG.get('tracks_enabled', True) and open_track(state) is not None:
        longest = max(longest, int(CONFIG.get('track_session_timeout_min', 120)))
    return min(60, longest) * 60


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
