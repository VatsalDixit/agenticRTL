#!/usr/bin/env python3
"""
Did the build tracks pay? A tracks-off twin of a tracks-on run, and the two
side by side.

  twin     set up the twin: a git worktree at the reference run's starting
           commit, with one commit on top that sets tracks_enabled false and
           nothing else, its own copy of agentic/data, and the command that
           starts it in tmux with the reference's goal, model, candidates,
           seed and iteration count. Prints the plan; --apply carries it out.
  compare  the two runs at equal iterations and at equal spend, on the
           guarded throughput only (a fast iteration's bytes/cycle is two
           tables, not the score), and what the reference's tracks cost and
           bought.

    python ab_tracks.py twin RUNS/fastguard200 --name fastguard200-notracks \\
        --dir /local/home/vdixit/ab/fastguard200-notracks [--apply]
    python ab_tracks.py compare RUNS/fastguard200 \\
        /local/home/vdixit/ab/fastguard200-notracks/.agentic/runs/fastguard200-notracks

RUNS is <checkout>/.agentic/runs. A run is named by its folder (or its
state.json). Standard library only, so it runs from anywhere.

Keep this file OUTSIDE a checkout a loop is running in: the loop restarts
itself whenever any agentic/*.py of its checkout changes.
"""

import argparse
import collections
import csv
import datetime
import json
import math
import os
import shlex
import shutil
import subprocess
import sys


def die(msg):
    raise SystemExit('ab_tracks: ' + msg)


def load_state(path):
    path = os.path.abspath(path)
    if os.path.isdir(path):
        path = os.path.join(path, 'state.json')
    if not os.path.exists(path):
        die('no state.json at %s' % path)
    with open(path, encoding='utf-8') as fil:
        return json.load(fil), os.path.dirname(path)


def repo_of(run_dir):
    """<checkout>/.agentic/runs/<run> -> <checkout>."""
    runs = os.path.dirname(run_dir)
    if os.path.basename(runs) != 'runs' or \
            os.path.basename(os.path.dirname(runs)) != '.agentic':
        die('%s is not inside <checkout>/.agentic/runs' % run_dir)
    return os.path.dirname(os.path.dirname(runs))


def git(args, cwd, check=True):
    res = subprocess.run(['git'] + args, cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        die('git %s failed: %s' % (' '.join(args), (res.stderr or res.stdout).strip()))
    return res.stdout.strip() if res.returncode == 0 else None


def when(iso):
    """An ISO time from state.json or git, comparable across both."""
    try:
        return datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00')) \
            .astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def pct(new, old):
    return 100.0 * (new / old - 1.0) if new and old else None


# ---------------------------------------------------------------------------
# twin

def twin(args):
    ref, ref_dir = load_state(args.ref)
    root = repo_of(ref_dir)
    base = ref.get('base_commit') or die('the reference records no base_commit')
    kit = git(['rev-parse', '--verify', (args.kit_commit or base) + '^{commit}'], root)
    its = ref.get('iterations') or []
    warn = []

    cfg_text = git(['show', kit + ':agentic/config.json'], root)
    cfg = json.loads(cfg_text)
    if not cfg.get('tracks_enabled', True):
        die('tracks_enabled is already false at %s: the reference had no tracks to switch off'
            % kit[:10])
    if git(['diff', '--quiet', base, kit, '--', 'rtl'], root, check=False) is None:
        warn.append('rtl/ differs between %s and %s: the twin would not start from the '
                    "reference's design" % (base[:10], kit[:10]))

    tracks = ref.get('tracks') or []
    if not tracks:
        warn.append('the reference never opened a track: with tracks off the twin differs '
                    'only in one planner sentence, so the comparison measures run-to-run '
                    'noise, not tracks')

    # What the reference ran with. --model and --candidates are flags; effort
    # is config, and the tmux launcher passes no AGENTIC_* overrides through,
    # so a different effort goes into the twin's config commit.
    models = collections.Counter(it.get('model') or ref.get('model') for it in its)
    efforts = collections.Counter(it.get('effort') for it in its if it.get('effort'))
    model = (models.most_common(1)[0][0] if models else None) or ref.get('model') \
        or cfg.get('model')
    effort = efforts.most_common(1)[0][0] if efforts else cfg.get('effort')
    if len(models) > 1:
        warn.append('the reference changed model mid-run (%s); the twin uses %s throughout'
                    % (dict(models), model))
    if len(efforts) > 1:
        warn.append('the reference changed effort mid-run (%s); the twin uses %s throughout'
                    % (dict(efforts), effort))
    n_iters = args.iters or len(its)
    if not n_iters:
        die('the reference has no iterations recorded; give --iters')

    # The loop reloads its kit between iterations, so commits that landed in
    # the reference's checkout while it ran changed what it ran.
    started, last = when(ref.get('started')), when(its[-1].get('at')) if its else None
    landed = []
    log = git(['log', '--reverse', '--format=%H %cI %s', '%s..HEAD' % base, '--',
               'agentic/*.py', 'agentic/config.json', 'rtl'], root, check=False) or ''
    for line in log.splitlines():
        sha, at, subject = line.split(' ', 2)
        at_t = when(at)
        during = bool(started and last and at_t and started <= at_t <= last)
        landed.append((sha, at, subject, during))
    dirty = git(['status', '--porcelain', '--', 'agentic', 'rtl'], root, check=False)

    branch = 'ab/' + args.name
    for ref_name in ('refs/heads/' + branch, 'refs/heads/agentic/' + args.name):
        if git(['rev-parse', '--verify', '--quiet', ref_name], root, check=False):
            die('branch %s already exists; pick another --name' % ref_name[11:])
    target = os.path.abspath(args.dir)
    if os.path.exists(target):
        die('%s already exists' % target)

    twin_cfg = dict(cfg, tracks_enabled=False)
    if effort and effort != cfg.get('effort'):
        twin_cfg['effort'] = effort
    changed = sorted(k for k in twin_cfg if twin_cfg[k] != cfg.get(k))

    local = os.path.dirname(os.path.abspath(root))
    env = {'AGENTIC_TMUX_SESSION': 'ab-' + args.name,
           'AGENTIC_HOME': os.path.join(local, 'home-' + args.name)}
    cmd = ['%s=%s' % (k, shlex.quote(v)) for k, v in env.items()]
    cmd += [shlex.quote(os.path.join(target, 'agentic', 'hacc_tmux.sh')),
            '--run', shlex.quote(args.name), '--goal', shlex.quote(ref.get('goal_text') or ''),
            '--model', shlex.quote(model), '--candidates', str(ref.get('candidates')),
            '--seed', str(ref.get('seed', 0)), '--iters', str(n_iters)]
    if 'AGENTIC_HOME' not in (git(['show', kit + ':agentic/hacc_tmux.sh'], root,
                                  check=False) or ''):
        warn.append('hacc_tmux.sh at %s does not read AGENTIC_HOME: the twin shares '
                    "Claude Code's HOME with the reference" % kit[:10])

    print()
    print('reference   %s  (%d iterations, %d track(s), $%.2f)'
          % (ref.get('run'), len(its), len(tracks), ref.get('cost_usd') or 0))
    print('checkout    %s' % root)
    print('starts at   %s%s' % (kit[:12], '' if kit == base else
                                '  (the reference started at %s)' % base[:12]))
    print('goal        %s' % ref.get('goal_text'))
    print('model       %s, effort %s, %s candidates, seed %s, %d iterations'
          % (model, effort, ref.get('candidates'), ref.get('seed', 0), n_iters))
    print('config      %s' % ', '.join('%s: %s -> %s' % (k, json.dumps(cfg.get(k)),
                                                          json.dumps(twin_cfg[k]))
                                       for k in changed))
    if landed:
        print()
        print('kit commits after the start (%s marks one that landed while the reference '
              'ran;\nthe loop picks those up between iterations, so --kit-commit may be '
              'the closer match):' % '*')
        for sha, at, subject, during in landed:
            print('  %s %s %s %s' % ('*' if during else ' ', sha[:10], at[:16], subject[:70]))
    if dirty:
        warn.append('uncommitted changes under agentic/ or rtl/ in %s; if the reference ran '
                    'on them the twin cannot reproduce them:\n    %s'
                    % (root, dirty.replace('\n', '\n    ')))
    for w in warn:
        print()
        print('WARNING: ' + w)

    steps = [
        'git -C %s worktree add -b %s %s %s' % (shlex.quote(root), branch,
                                                shlex.quote(target), kit[:12]),
        'write %s/agentic/config.json with %s, and commit it on %s'
        % (target, ', '.join(changed), branch),
        'copy %s/agentic/data to %s/agentic/data' % (root, target),
    ]
    print()
    print('%s:' % ('doing' if args.apply else 'would do (add --apply)'))
    for s in steps:
        print('  ' + s)

    if args.apply:
        git(['worktree', 'add', '-b', branch, target, kit], root)
        with open(os.path.join(target, 'agentic', 'config.json'), 'w', encoding='utf-8') as fil:
            fil.write(json.dumps(twin_cfg, indent=2) + '\n')
        git(['commit', '-q', '-m', 'A/B twin of %s: build tracks off\n\n%s' % (
            ref.get('run'), '\n'.join('%s: %s -> %s' % (k, json.dumps(cfg.get(k)),
                                                       json.dumps(twin_cfg[k]))
                                     for k in changed)),
             '--', 'agentic/config.json'], target)
        data = os.path.join(root, 'agentic', 'data')
        if os.path.isdir(data):
            shutil.copytree(data, os.path.join(target, 'agentic', 'data'))
        else:
            print('  WARNING: %s does not exist; run agentic/make_data.py in the twin' % data)
        print('  done: %s on %s' % (git(['rev-parse', '--short', 'HEAD'], target), branch))

    print()
    print('start it with:')
    print('  ' + ' '.join(cmd))
    print()
    print('Both runs share the Claude subscription window and the host: run the twin')
    print('after the other loops finish, or the waits and the slower simulations make')
    print('it a different experiment. The @reboot crontab line restarts only the')
    print("default tmux session; add one with AGENTIC_TMUX_SESSION=%s for the twin."
          % env['AGENTIC_TMUX_SESSION'])
    return 0


# ---------------------------------------------------------------------------
# compare

def series(state):
    """One row per iteration: cumulative cost, the guarded throughput gain
    (carried through fast iterations), the adoptions and whether they held."""
    base_tp = (state.get('baseline') or {}).get('throughput_gbps')
    rows, gain, cost, pending = [], 0.0, 0.0, []
    for rec in state.get('iterations') or []:
        cost += rec.get('cost_usd') or 0
        guard = (rec.get('guard') or {}).get('kind')
        if guard == 'rollback':
            for a in pending:
                a['held'] = False
        if guard in ('rollback', 'guard_held'):
            pending = []
        guarded = rec.get('best_guarded', True)
        adopted = []
        for c in rec.get('candidates') or []:
            if c.get('adopted'):
                a = {'iteration': rec.get('iteration'), 'label': c.get('label'),
                     'track': (c.get('track') or {}).get('id'),
                     'gain_pct': (c.get('measured') or {}).get('gain_pct'),
                     'kind': rec.get('measure_kind', 'full'), 'held': True}
                adopted.append(a)
                if not guarded:
                    pending.append(a)
        if guarded:
            g = pct((rec.get('best_after') or {}).get('throughput_gbps'), base_tp)
            gain = g if g is not None else gain
        rows.append({'iteration': rec.get('iteration'), 'kind': rec.get('measure_kind', 'full'),
                     'cost': cost, 'gain': gain, 'guarded': guarded, 'adopted': adopted,
                     'rollback': guard == 'rollback'})
    return rows


def gain_at_cost(rows, spend):
    """The guarded gain once `spend` dollars are spent, and the mean gain over
    [0, spend] (the area under the best-so-far curve, per dollar)."""
    gain, area, prev_cost, prev_gain = 0.0, 0.0, 0.0, 0.0
    for r in rows:
        if r['cost'] > spend:
            break
        area += prev_gain * (r['cost'] - prev_cost)
        prev_cost, prev_gain = r['cost'], r['gain']
        gain = r['gain']
    area += prev_gain * max(0.0, spend - prev_cost)
    return gain, (area / spend if spend else 0.0)


def slot_ledger(state):
    """Dollars, sessions and held log-gain, split into track and normal slots."""
    out = {'track': [0.0, 0, 0.0], 'normal': [0.0, 0, 0.0]}
    for rec in state.get('iterations') or []:
        for c in rec.get('candidates') or []:
            if (c.get('session') or {}).get('status') == 'retry':
                continue
            side = out['track' if c.get('track') else 'normal']
            side[0] += (c.get('session') or {}).get('cost_usd') or 0
            side[1] += 1
    for r in series(state):
        for a in r['adopted']:
            if a['held'] and a['gain_pct'] is not None:
                out['track' if a['track'] else 'normal'][2] += \
                    100.0 * math.log1p(a['gain_pct'] / 100.0)
    return out


def track_table(state):
    cost = collections.defaultdict(float)
    sessions = collections.Counter()
    for rec in state.get('iterations') or []:
        for c in rec.get('candidates') or []:
            tid = (c.get('track') or {}).get('id')
            if tid:
                cost[tid] += (c.get('session') or {}).get('cost_usd') or 0
                sessions[tid] += 1
    adopted = {a['track']: a for r in series(state) for a in r['adopted'] if a['track']}
    lines = []
    for t in state.get('tracks') or []:
        steps = t.get('steps') or []
        a = adopted.get(t.get('id'))
        lines.append('  %-24s i%-4s %-13s %2s/%-2s steps passed  %2d sessions  $%7.2f  %s'
                     % (str(t.get('id'))[:24], t.get('opened_iteration'), t.get('status'),
                        sum(1 for s in steps if s.get('status') == 'passed'), len(steps),
                        sessions[t.get('id')], cost[t.get('id')],
                        ('adopted i%s %+.2f%%%s' % (a['iteration'], a['gain_pct'] or 0,
                                                    '' if a['held'] else ', rolled back'))
                        if a else (t.get('reason') or '')[:60]))
    return lines


def compare(args):
    (a, _), (b, _) = load_state(args.ref), load_state(args.twin)
    na, nb = a.get('run'), b.get('run')

    checks = []
    ba, bb = a.get('baseline') or {}, b.get('baseline') or {}
    if ba.get('bytes_per_cycle') != bb.get('bytes_per_cycle'):
        checks.append('baseline bytes/cycle differs (%s vs %s): not the same starting design '
                      'or corpus' % (ba.get('bytes_per_cycle'), bb.get('bytes_per_cycle')))
    if a.get('draw_names') != b.get('draw_names'):
        checks.append('the runs were scored on different draws')
    for key in ('goal_text', 'candidates', 'seed'):
        if a.get(key) != b.get(key):
            checks.append('%s differs: %r vs %r' % (key, a.get(key), b.get(key)))
    ma = sorted({it.get('model') for it in a.get('iterations') or [] if it.get('model')})
    mb = sorted({it.get('model') for it in b.get('iterations') or [] if it.get('model')})
    if ma != mb:
        checks.append('models differ: %s vs %s' % (ma, mb))
    if b.get('tracks'):
        checks.append('%s opened %d track(s): it is not a tracks-off run' % (nb, len(b['tracks'])))
    if not a.get('tracks'):
        checks.append('%s opened no track: this compares two runs without tracks' % na)

    ra, rb = series(a), series(b)
    n = min(len(ra), len(rb))
    spend = min(ra[-1]['cost'] if ra else 0, rb[-1]['cost'] if rb else 0)

    print()
    print('%-6s | %-34s | %-34s' % ('', na, nb))
    print('%-6s | %4s %9s %9s %-9s | %4s %9s %9s %-9s'
          % ('iter', 'kind', 'spent', 'guarded', 'adopted', 'kind', 'spent', 'guarded', 'adopted'))
    print('-' * 80)

    def cell(r):
        if r is None:
            return '%4s %9s %9s %-9s' % ('', '', '', '')
        mark = ''.join(('T' if x['track'] else 'N') + ('' if x['held'] else 'x')
                       for x in r['adopted']) + ('  R' if r['rollback'] else '')
        return '%4s %8.2f$ %+8.2f%% %-9s' % (r['kind'][:4], r['cost'], r['gain'], mark)

    for i in range(max(len(ra), len(rb))):
        print('%-6s | %s | %s' % ('i%d' % (i + 1), cell(ra[i] if i < len(ra) else None),
                                 cell(rb[i] if i < len(rb) else None)))
    print('adopted: T a track, N a normal candidate, x rolled back by the next guard; '
          'R a guard rolled back the best')

    def at_iter(rows):
        return rows[n - 1]['gain'] if n else 0.0

    ga, mean_a = gain_at_cost(ra, spend)
    gb, mean_b = gain_at_cost(rb, spend)
    print()
    print('%-44s %14s %14s' % ('', na[:14], nb[:14]))
    print('%-44s %+13.2f%% %+13.2f%%' % ('guarded gain at iteration %d (both ran it)' % n,
                                        at_iter(ra), at_iter(rb)))
    print('%-44s %+13.2f%% %+13.2f%%' % ('guarded gain at equal spend ($%.2f)' % spend, ga, gb))
    print('%-44s %+13.2f%% %+13.2f%%' % ('mean gain over that spend (area / $)', mean_a, mean_b))
    print('%-44s %13.2f$ %13.2f$' % ('total spent', ra[-1]['cost'] if ra else 0,
                                      rb[-1]['cost'] if rb else 0))
    fa, fb = (ra[-1]['gain'] if ra else 0), (rb[-1]['gain'] if rb else 0)
    print('%-44s %14s %14s' % ('dollars per percent (final guarded gain)',
                               '$%.2f' % (ra[-1]['cost'] / fa) if fa > 0 else 'n/a',
                               '$%.2f' % (rb[-1]['cost'] / fb) if fb > 0 else 'n/a'))

    for name, state in ((na, a), (nb, b)):
        led = slot_ledger(state)
        print()
        print('%s by slot (held gains, as log-%%, as each adoption was judged):' % name)
        for side in ('track', 'normal'):
            usd, cnt, lg = led[side]
            if not cnt:
                continue
            print('  %-7s %3d sessions  $%8.2f  %+7.2f%% gained  %s'
                  % (side, cnt, usd, lg, ('%.3f %%/$' % (lg / usd)) if usd else 'n/a'))
        rows = track_table(state)
        if rows:
            print('  tracks:')
            for line in rows:
                print('  ' + line)

    if checks:
        print()
        for c in checks:
            print('CHECK: ' + c)
    print()
    print('One pair of runs is one draw from each arm. Two runs of the same kit have')
    print('not been compared yet, so a difference smaller than that spread means')
    print('nothing; a second tracks-on run from the same start measures it.')

    if args.csv:
        with open(args.csv, 'w', newline='', encoding='utf-8') as fil:
            w = csv.writer(fil)
            w.writerow(['run', 'iteration', 'kind', 'cost_usd', 'guarded_gain_pct',
                        'adopted_track', 'adopted_normal', 'rollback'])
            for name, rows in ((na, ra), (nb, rb)):
                for r in rows:
                    w.writerow([name, r['iteration'], r['kind'], round(r['cost'], 2),
                                round(r['gain'], 3),
                                sum(1 for x in r['adopted'] if x['track']),
                                sum(1 for x in r['adopted'] if not x['track']),
                                int(r['rollback'])])
        print('wrote %s' % args.csv)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    sub = ap.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('twin', help='set up a tracks-off twin of a run')
    t.add_argument('ref', help='the tracks-on run: its folder or state.json')
    t.add_argument('--name', required=True, help='the twin run name')
    t.add_argument('--dir', required=True, help='where the twin worktree goes (must not exist)')
    t.add_argument('--kit-commit', default=None,
                   help='start from this commit instead of the reference\'s base_commit')
    t.add_argument('--iters', type=int, default=None,
                   help='iteration cap (default: as many as the reference ran)')
    t.add_argument('--apply', action='store_true', help='do it; without this, only print')
    c = sub.add_parser('compare', help='the reference and its twin side by side')
    c.add_argument('ref')
    c.add_argument('twin')
    c.add_argument('--csv', default=None, help='also write the per-iteration series here')
    args = ap.parse_args()
    return twin(args) if args.cmd == 'twin' else compare(args)


if __name__ == '__main__':
    sys.exit(main())
