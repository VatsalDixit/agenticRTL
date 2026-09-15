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
import sys
import time
import traceback

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

import analyse                         # noqa: E402
import freeze                          # noqa: E402
import learn as learn_mod              # noqa: E402
import measure                         # noqa: E402
import propose                         # noqa: E402
import report                          # noqa: E402
import skills as skills_mod            # noqa: E402
from tools import (CONFIG, ROOT, GitError, Logger, eda_available, git,  # noqa: E402
                   git_ok, now_iso, pct, read_json, rmtree, write_json)
from tools import kill_all as tools_kill_all                          # noqa: E402

# The scoring data (agentic/data, test_data) is not tracked by git, so a new
# worktree never contains it. These are removed only if someone committed
# them by hand. vectors/ (the upstream unit-test vectors from a built-in
# sample) stays: it is not the scoring data and deleting tracked files makes
# the worktree look damaged to the session working in it.
BLIND_DIRS = ('test_data', os.path.join('agentic', 'data'))
BLIND_SUFFIXES = ('.parquet',)
METRIC_KEYS = {'throughput': 'throughput_gbps', 'bytes_per_cycle': 'bytes_per_cycle',
               'fmax': 'f_max_mhz', 'area': 'area_um2'}


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
        with open(path, 'w', encoding='utf-8', newline='\n') as fil:
            fil.write(transform(text))
        write_json(os.path.join(asg['worktree'], 'PROPOSAL.json'), proposal)
        log('    %s | fake edit %s' % (asg['label'], name))
        out.append({'label': asg['label'], 'direction': asg['direction'],
                    'session': {'status': 'ok', 'cost_usd': 0.0, 'turns': 0,
                                'seconds': 0.0, 'error': '', 'subtype': 'fake'},
                    'proposal': propose.read_proposal(asg['worktree'])})
    return out


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
        'area_gain_pct': pct(metrics.get('area_um2'), parent.get('area_um2')),
        'gain_pct': goal_gain(metrics, parent, goal),
    }
    meas = {k: (round(v, 3) if v is not None else None) for k, v in meas.items()}
    out['measured'] = meas
    if metrics.get('synth_error') or meas['gain_pct'] is None:
        out['outcome'] = 'failed_synth'
        out['reason'] = metrics.get('synth_error') or 'no synthesis numbers'
        return out
    gain = meas['gain_pct']
    area = meas['area_gain_pct'] or 0.0
    score = -gain + float(CONFIG['area_weight']) * max(area, 0.0)
    if area > float(CONFIG['area_penalty_pct']):
        score += 0.5 * max(area - float(CONFIG['area_penalty_pct']), 0.0)
    out['score'] = round(score, 4)
    if gain < float(CONFIG['min_gain_pct']):
        out['outcome'] = 'no_gain' if gain > -float(CONFIG['min_gain_pct']) else 'regressed'
        out['reason'] = 'goal metric %+.2f%% (needs at least +%.2f%%)' % (
            gain, float(CONFIG['min_gain_pct']))
        return out
    if area > float(CONFIG['max_area_growth_pct']) and gain < area:
        out['outcome'] = 'too_expensive'
        out['reason'] = 'area +%.1f%% for %+.2f%% gain' % (area, gain)
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
    lines = ['bytes/cycle %.3f on real Parquet data (geomean), f_max %.1f MHz, area %.0f um2, '
             'throughput %.3f GB/s, worst slack %+.3f ns at a %d ps clock target'
             % (metrics.get('bytes_per_cycle') or 0, metrics.get('f_max_mhz') or 0,
                metrics.get('area_um2') or 0, metrics.get('throughput_gbps') or 0,
                metrics.get('wns_ns') or 0, int(CONFIG['clock_period_ps']))]
    lines.append('ports: co_data %d bytes (cnt %d bits), de_data %d bytes (cnt %d bits); '
                 'cores %d; core line %d bytes; registers %s'
                 % (w.get('in_bytes', 0), w.get('in_cnt_bits', 0), w.get('out_bytes', 0),
                    w.get('out_cnt_bits', 0), w.get('cores', 1),
                    int(w.get('core_line_bytes', 0)), metrics.get('regs', 'n/a')))
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
            lines.append(line)
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


def build_ctx(state, skills_data, iteration):
    best = state['best']['metrics']
    return {
        'goal_text': state['goal_text'],
        'iteration': iteration,
        'max_iters': state['max_iters'],
        'progress_text': progress_text(state),
        'state_text': state_text(best, state['baseline']),
        'profile_text': profile_text(best),
        'lever': best.get('lever') or {'lever': 'unknown', 'reason': 'no profile'},
        'skills_text': skills_mod.format_for_prompt(skills_data, max_entries=30),
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
        self.log = Logger(os.path.join(self.dir, 'loop.log'))
        self.status = report.Status(os.path.join(self.dir, 'status.json'))
        self.base_dir = os.path.join(self.dir, 'base')
        self.state = read_json(self.state_path, None)
        self.limit_message = ''

    def save(self):
        self.state['updated'] = now_iso()
        write_json(self.state_path, self.state)
        report.write_report(self.state, os.path.join(self.dir, 'report.html'))

    def set_progress(self):
        base = self.state['baseline']
        best = self.state['best']['metrics']
        self.state['progress'] = {
            'throughput_gain_pct': pct(best.get('throughput_gbps'), base.get('throughput_gbps')),
            'bpc_gain_pct': pct(best.get('bytes_per_cycle'), base.get('bytes_per_cycle')),
            'fmax_gain_pct': pct(best.get('f_max_mhz'), base.get('f_max_mhz')),
            'area_gain_pct': pct(best.get('area_um2'), base.get('area_um2')),
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
    if not os.path.exists(measure.LIB_FILE):
        problems.append('missing liberty file %s (run bash agentic/syn/get_lib.sh)' % measure.LIB_FILE)
    fz = freeze.check()
    if fz:
        problems.extend(fz)
    if need_model:
        ok, why = propose.availability()
        if not ok:
            problems.append(why)
    return problems


def measure_one(args):
    rtl_dir, work_dir, draws = args
    try:
        return measure.measure(rtl_dir, work_dir, draws, synth=True)
    except Exception as exc:                # never let one candidate kill the run
        return {'oracle_pass': False, 'error': 'measurement crashed: %s' % exc,
                'draws': []}


def run_iteration(run, draws, skills_data, k, n_cands, dry_run=False, fake=False):
    state = run.state
    log = run.log
    goal = state['goal']
    parent = state['best']['metrics']
    run.status.set(phase='plan', iteration=k, detail='choosing %d directions' % n_cands)
    log('== iteration %d of %d ==' % (k, state['max_iters']))
    log('best so far: %s' % progress_text(state))
    log('lever: %s -- %s' % (parent.get('lever', {}).get('lever'),
                             parent.get('lever', {}).get('reason')))

    ctx = build_ctx(state, skills_data, k)
    directions, plan_res = propose.plan_directions(ctx, n_cands, log)
    for j, d in enumerate(directions, 1):
        log('  direction c%d: %s' % (j, d['focus'][:140]))
    if dry_run:
        log('dry run: stopping before any coding session')
        return None

    iter_dir = os.path.join(run.dir, 'iter-%d' % k)
    os.makedirs(iter_dir, exist_ok=True)
    start = git(['rev-parse', 'HEAD'], cwd=run.base_dir)
    assignments = []
    try:
        for j, d in enumerate(directions, 1):
            label = 'c%d' % j
            # Not under the run branch's name: git cannot have both a branch
            # 'agentic/run' and a branch 'agentic/run/i1-c1'.
            branch = 'agentic-cand/%s/i%d-%s' % (state['run'], k, label)
            path = os.path.join(iter_dir, label)
            add_worktree(path, branch, start)
            blind(path)
            assignments.append({'label': label, 'worktree': path, 'direction': d,
                                'branch': branch})
    except GitError as exc:
        log('could not prepare a candidate worktree: %s' % exc)
        return 'error'

    def discard_all():
        for asg in assignments:
            try:
                remove_worktree(asg['worktree'])
            except GitError as exc:
                log('  (%s)' % exc)
            git_ok(['branch', '-D', asg['branch']])

    run.status.set(phase='write', detail='%d coding sessions in parallel' % n_cands)
    log('writing %d candidates in parallel (model %s, up to %d min each)...'
        % (n_cands, CONFIG['model'], int(CONFIG['session_timeout_min'])))
    t0 = time.time()
    if fake:
        results = fake_write_candidates(assignments, log)
    else:
        results = propose.write_candidates(ctx, assignments, log)
    log('sessions finished in %.0f min' % ((time.time() - t0) / 60.0))

    statuses = [r['session'].get('status') for r in results]
    no_proposal = not any(r['proposal'] and r['proposal'].get('id') not in (None, 'none')
                          for r in results)
    if no_proposal and any(s == 'limit' for s in statuses):
        # The provider is refusing; nothing was produced. Do not spend an
        # iteration on it: wait for the reset and try this one again.
        msgs = ' '.join((r['session'].get('error') or '') for r in results)
        run.limit_message = msgs
        discard_all()
        return 'limit'
    if no_proposal and all(s not in ('ok', 'budget') for s in statuses):
        errs = '; '.join((r['session'].get('error') or '')[:120] for r in results)
        log('every session failed: %s' % errs)
        discard_all()
        return 'error'

    # commit what each session produced
    cands = []
    for asg, res in zip(assignments, results):
        sess = res['session']
        prop = res['proposal'] or {}
        cand = {'label': asg['label'], 'branch': asg['branch'],
                'direction': asg['direction'], 'id': prop.get('id'),
                'rationale': prop.get('rationale', ''),
                'expected_gain_pct': prop.get('expected_gain_pct'),
                'expected_effect': prop.get('expected_effect', ''),
                'risk': prop.get('risk', ''), 'skills_used': prop.get('skills_used') or [],
                'session': sess, 'commit': None, 'files_changed': [],
                'outcome': '', 'reason': '', 'measured': {}, 'score': None,
                'advantage': None, 'adopted': False}
        log('  %s: session %s, %d turns, $%.2f, %.0f s%s' % (
            asg['label'], sess.get('status'), sess.get('turns') or 0,
            sess.get('cost_usd') or 0, sess.get('seconds') or 0,
            (' (' + sess.get('error', '')[:100] + ')') if sess.get('error') else ''))
        try:
            sha, files = commit_candidate(asg['worktree'], 'agentic i%d %s: %s'
                                          % (k, asg['label'], prop.get('id') or 'no proposal'))
        except GitError as exc:
            sha, files = None, []
            log('  %s: could not commit: %s' % (asg['label'], exc))
        cand['commit'] = sha
        cand['files_changed'] = files
        if not sha or (prop.get('id') in (None, 'none')):
            cand['outcome'] = 'no_proposal'
            cand['reason'] = (prop.get('rationale') or sess.get('error') or
                              'the session made no change to rtl/')[:300]
            log('  %s: no proposal (%s)' % (asg['label'], cand['reason'][:120]))
        else:
            log('  %s: proposal %s touching %s; predicted %s%%' % (
                asg['label'], cand['id'], ', '.join(files)[:120],
                cand['expected_gain_pct']))
        cands.append(cand)

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
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs))
        try:
            metrics_list = list(pool.map(measure_one, jobs))
        except KeyboardInterrupt:
            tools_kill_all()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        parent_ram = ram_latency(os.path.join(run.base_dir, 'rtl'))
        for (c, asg), metrics in zip(to_measure, metrics_list):
            ev = evaluate(metrics, parent, goal)
            raw[c['label']] = metrics
            c.update({'measured': ev['measured'], 'score': ev['score'],
                      'outcome': ev['outcome'], 'reason': ev['reason'],
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
            rmtree(os.path.join(iter_dir, c['label'] + '-measure', 'sim'))
            for junk in ('vhsnunzip_unbuffered', 'work-obj08.cf', 'netlist.v'):
                try:
                    os.remove(os.path.join(iter_dir, c['label'] + '-measure', 'synth', junk))
                except OSError:
                    pass
    advantages(cands)

    # select
    winner = None
    adoptable = [c for c in cands if c.get('adoptable')]
    if adoptable:
        winner = min(adoptable, key=lambda c: c['score'])
        run.status.set(phase='adopt', detail=winner['id'])
        if not git_ok(['merge', '--ff-only', winner['commit']], cwd=run.base_dir):
            git(['reset', '--hard', winner['commit']], cwd=run.base_dir)
        winner['adopted'] = True
        winner['outcome'] = 'adopted'
        # The best design keeps the FULL measurement (per-draw stage
        # analyses included) because the next iteration's brief is built
        # from it; the iteration record below keeps the stripped copy.
        best_metrics = dict(raw[winner['label']])
        state['best'] = {'commit': winner['commit'], 'metrics': best_metrics,
                         'iteration': k, 'id': winner['id']}
        run.set_progress()
        log('ADOPTED %s: %s' % (winner['id'], progress_text(state)))
    else:
        log('nothing adopted this iteration')

    # learn
    run.status.set(phase='learn', detail='updating the skill library')
    group = []
    for c in cands:
        group.append({'label': c['label'], 'id': c['id'], 'focus': c['direction'].get('focus'),
                      'rationale': c['rationale'], 'outcome': c['outcome'],
                      'problem': c['reason'], 'measured': c['measured'],
                      'expected_gain_pct': c['expected_gain_pct'],
                      'advantage': c['advantage'], 'adopted': c['adopted'],
                      'skills_used': c['skills_used'], 'files_changed': c['files_changed']})
        # Counters only move for candidates that were actually measured; a
        # session that timed out or hit a limit says nothing about a skill.
        if c['commit'] and c['outcome'] != 'no_proposal':
            used = list(c['skills_used']) + list(c['direction'].get('skill_ids') or [])
            skills_mod.record_outcome(skills_data, sorted(set(used)),
                                      passed=c['outcome'] in ('candidate', 'adopted', 'no_gain',
                                                              'regressed', 'too_expensive',
                                                              'ram_latency_changed'),
                                      adopted=c['adopted'], advantage=c['advantage'])
    changed, lessons = [], []
    if not fake:
        try:
            changed, lessons, _res = learn_mod.learn(ctx, group, skills_data, log)
        except Exception as exc:              # learning is optional; never fatal
            log('  learning step failed: %s' % str(exc)[:200])
    for ch in changed:
        log('  skill %s' % ch)
    for les in lessons:
        log('  lesson: %s' % les)
    skills_mod.save(skills_data, run.skills_path)

    # record
    record = {'iteration': k, 'at': now_iso(), 'directions': directions,
              'candidates': [{kk: vv for kk, vv in c.items() if kk != 'metrics'}
                             for c in cands],
              'winner': ({'id': winner['id'], 'label': winner['label'],
                          'commit': winner['commit'], 'measured': winner['measured']}
                         if winner else None),
              'best_after': {kk: state['best']['metrics'].get(kk) for kk in
                             ('throughput_gbps', 'bytes_per_cycle', 'f_max_mhz', 'area_um2')},
              'skills_changed': changed, 'lessons': lessons,
              'cost_usd': round(sum((c['session'].get('cost_usd') or 0) for c in cands), 2)}
    state['iterations'].append(record)
    state['cost_usd'] = round((state.get('cost_usd') or 0) + record['cost_usd'], 2)
    run.save()

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
            'started': now_iso(), 'base_commit': base_commit,
            'branch': branch, 'baseline': None,
            'best': None, 'iterations': [], 'stopped': None, 'cost_usd': 0.0,
            'model': CONFIG['model'], 'seed': args.seed,
        }
        log('goal: %s  (metric %s, target %s)' % (
            goal['text'], goal['metric'],
            '%+.0f%%' % goal['target_pct'] if goal['target_pct'] else 'as far as possible'))
    else:
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
        else:
            skills_data = skills_mod.load()
            skills_mod.save(skills_data, run.skills_path)

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
                wait = propose.reset_wait_seconds(run.limit_message)
                if wait is None:
                    wait = int(CONFIG['limit_wait_min']) * 60
                wait = min(max(wait + 120, 300), 6 * 3600)
                log('the model provider reports a usage limit; waiting %d min (wait %d)'
                    % (wait // 60, limit_waits))
                run.status.set(phase='waiting',
                               detail='usage limit; retrying in %d min' % (wait // 60))
                time.sleep(wait)
                continue
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
    log('total model cost this run: $%.2f' % (state.get('cost_usd') or 0))
    log('report: %s' % os.path.join(run.dir, 'report.html'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
