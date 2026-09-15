#!/usr/bin/env python3
"""
Small helpers every part of the loop uses.

  * where things are (ROOT = the repo, KIT = this folder)
  * the config (agentic/config.json, overridable with AGENTIC_* env vars)
  * running a command with a hard timeout that also kills child processes
  * running a bash script "where the EDA tools live" (WSL on Windows,
    plain bash on Linux/macOS)
  * git helpers that never touch the user's own checkout
  * a logger that writes to the screen and to the run's log file
  * atomic JSON writes, geometric mean, percent change
"""

import datetime
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)

DEFAULT_CONFIG = {
    # Where GHDL and Yosys live. On Windows the loop runs them inside WSL.
    "wsl_distro": "Ubuntu-22.04",
    # Folder of the OSS CAD Suite inside that shell ($HOME is expanded there).
    "eda_suite": "$HOME/eda/oss-cad-suite",
    # Which Claude model writes the RTL candidates, and how hard it thinks.
    # Sonnet by default: on a subscription the 5-hour usage window is the
    # real budget, and Opus spends it about five times faster. Pass
    # --model opus for higher quality per session and fewer iterations per day.
    "model": "sonnet",
    "effort": "high",
    # Cheaper model for the planning and skill-learning calls (no tools).
    "helper_model": "sonnet",
    # How many RTL candidates are written in parallel each iteration.
    "candidates": 3,
    # Limits for one candidate-writing session.
    "session_budget_usd": 10.0,
    "session_max_turns": 300,
    "session_timeout_min": 45,
    # Stimulus: pages per real-data draw.
    "train_pages": 12,
    # Synthesis clock target in picoseconds (4000 ps = 250 MHz).
    "clock_period_ps": 4000,
    # Tool timeouts.
    "sim_timeout_s": 1200,
    "synth_timeout_s": 2400,
    # Scoring.
    "area_weight": 0.15,
    "area_penalty_pct": 10.0,
    "min_gain_pct": 0.2,
    "max_area_growth_pct": 60.0,
    "heldout_share_min": 0.15,
    "overfit_train_pct": 5.0,
    # How long to wait when the model provider says "limit reached".
    "limit_wait_min": 20,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(KIT, 'config.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fil:
            cfg.update(json.load(fil))
    for key in list(cfg):
        val = os.environ.get('AGENTIC_' + key.upper())
        if val is None:
            continue
        cur = cfg[key]
        if isinstance(cur, bool):
            cfg[key] = val.lower() in ('1', 'true', 'yes', 'on')
        elif isinstance(cur, int):
            cfg[key] = int(val)
        elif isinstance(cur, float):
            cfg[key] = float(val)
        else:
            cfg[key] = val
    return cfg


CONFIG = load_config()


def is_windows():
    return sys.platform == 'win32'


def shell_path(path):
    """A path as the EDA shell sees it. On Windows that is /mnt/c/... ."""
    path = os.path.abspath(path).replace('\\', '/')
    if is_windows() and len(path) > 1 and path[1] == ':':
        return '/mnt/' + path[0].lower() + path[2:]
    return path


class Result(object):
    def __init__(self, rc, out, err, timed_out=False, seconds=0.0):
        self.rc = rc
        self.out = out
        self.err = err
        self.timed_out = timed_out
        self.seconds = seconds

    @property
    def ok(self):
        return self.rc == 0 and not self.timed_out

    @property
    def text(self):
        return (self.out or '') + (self.err or '')


def _kill_tree(proc):
    try:
        if is_windows():
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


# Every process this module starts, so a Ctrl-C can kill them all.
_LIVE = set()


def kill_all():
    for proc in list(_LIVE):
        _kill_tree(proc)


def run(cmd, timeout, cwd=None, env=None, stdin_bytes=None):
    """Run a command. Bytes in, bytes out, hard timeout, children killed too."""
    start = time.time()
    kwargs = {}
    if not is_windows():
        kwargs['start_new_session'] = True
    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdin=subprocess.PIPE if stdin_bytes is not None
                            else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **kwargs)
    _LIVE.add(proc)
    try:
        out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = b'', b''
        _LIVE.discard(proc)
        return Result(-1, _dec(out), _dec(err), timed_out=True,
                      seconds=time.time() - start)
    finally:
        _LIVE.discard(proc)
    return Result(proc.returncode, _dec(out), _dec(err),
                  seconds=time.time() - start)


def _dec(data):
    if data is None:
        return ''
    if isinstance(data, bytes):
        return data.decode('utf-8', 'replace')
    return data


def _eda_cmd(full):
    if is_windows():
        return ['wsl.exe', '-d', CONFIG['wsl_distro'], '-e', 'bash', '-c', full]
    return ['bash', '-c', full]


def eda_shell(script, timeout, cwd=None, kill_token=None):
    """Run a bash script where ghdl and yosys live.

    On Windows that is the WSL distro named in the config. Elsewhere it is
    plain bash. The OSS CAD Suite bin folder is put on PATH first.

    ``kill_token`` names this job inside the Linux side. On a timeout the
    Windows side can only kill wsl.exe; ghdl and yosys would keep running.
    With a token in their environment they can be found and killed too.
    """
    token = 'AGENTIC_JOB=%s-%d-%d' % (kill_token or 'job', os.getpid(),
                                      int(time.time() * 1000) % 100000)
    prefix = 'export %s; export PATH="%s/bin:$PATH"; ' % (token, CONFIG['eda_suite'])
    if cwd:
        prefix += 'cd "%s" || exit 97; ' % shell_path(cwd)
    res = run(_eda_cmd(prefix + script), timeout=timeout)
    if res.timed_out:
        killer = ("for p in /proc/[0-9]*; do grep -qz '%s' $p/environ 2>/dev/null "
                  "&& kill -9 ${p##*/} 2>/dev/null; done; true" % token)
        try:
            run(_eda_cmd(killer), timeout=60)
        except Exception:
            pass
    return res


def eda_available():
    """(ok, message): can the EDA shell run ghdl and yosys?"""
    res = eda_shell('which ghdl yosys && ghdl --version | head -1 && yosys -V',
                    timeout=120)
    if res.timed_out:
        return False, 'the EDA shell did not answer in 120 s'
    if not res.ok:
        return False, ('ghdl/yosys not found in the EDA shell: %s'
                       % res.text.strip()[-300:])
    return True, res.out.strip().replace('\n', ' | ')


# --------------------------------------------------------------------------
# git

class GitError(RuntimeError):
    pass


def git(args, cwd=None, check=True, timeout=600):
    res = run(['git'] + list(args), timeout=timeout, cwd=cwd or ROOT)
    if check and not res.ok:
        raise GitError('git %s failed (rc %s): %s'
                       % (' '.join(args), res.rc, res.text.strip()[-500:]))
    return res.out.strip()


def git_ok(args, cwd=None):
    try:
        git(args, cwd=cwd)
        return True
    except GitError:
        return False


def rev_parse(ref, cwd=None):
    return git(['rev-parse', '--verify', ref + '^{commit}'], cwd=cwd)


# --------------------------------------------------------------------------
# logging and files

class Logger(object):
    """Print to the screen and append to a file, with timestamps."""

    def __init__(self, path=None):
        self.path = path
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, msg):
        line = '[%s] %s' % (datetime.datetime.now().strftime('%H:%M:%S'), msg)
        try:
            sys.stdout.write(line + '\n')
            sys.stdout.flush()
        except Exception:
            pass
        if self.path:
            try:
                with open(self.path, 'a', encoding='utf-8') as fil:
                    fil.write(line + '\n')
            except Exception:
                pass


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def write_json(path, data):
    """Write JSON atomically so a reader never sees half a file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=os.path.dirname(
        os.path.abspath(path)))
    with os.fdopen(fd, 'w', encoding='utf-8') as fil:
        json.dump(data, fil, indent=1, sort_keys=True)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2)
    os.replace(tmp, path)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding='utf-8') as fil:
        return json.load(fil)


def sha256_file(path):
    with open(path, 'rb') as fil:
        data = fil.read().replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()


def geomean(values):
    vals = [v for v in values if v is not None and v > 0]
    if not vals:
        return None
    return math.exp(sum(math.log(v) for v in vals) / len(vals))


def pct(new, old):
    """Percent change from old to new, or None."""
    if new is None or old is None or old == 0:
        return None
    return 100.0 * (new - old) / old


def rmtree(path):
    """Remove a directory, retrying for Windows file locks."""
    for attempt in range(6):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            return True
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return not os.path.isdir(path)
