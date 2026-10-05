#!/usr/bin/env python3
"""
Watch a run that lives on the HACC host from this machine.

    python agentic/mirror.py --run hacc-real200          keep a copy, every 10 s
    python agentic/mirror.py --run hacc-real200 --gui    ...and open the dashboard on it

When the loop runs in tmux on the host (agentic/hacc_tmux.sh), the dashboard
cannot: the host has no display and no tkinter. So this copies what the
dashboard reads, state.json and status.json (plus report.html), from the
host's run folder into .agentic/mirror/<run>/ here, over the same key-based
ssh the synthesis backend uses, and writes mirror.json saying whether the
loop process on the host is alive. `gui.py --runs-dir .agentic/mirror` shows
that copy as if the run were local, and trusts mirror.json over a pid check
(the pid in status.json is the host's).

status.json is copied every time; state.json (1-2 MB) only when the host's
copy is newer. It only reads on the host: stopping it, losing the VPN or
closing the laptop never touches the run.
"""

import argparse
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import time

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

from tools import CONFIG  # noqa: E402

MIRRORS = os.path.join(ROOT, '.agentic', 'mirror')
FILES = ('state.json', 'status.json', 'report.html')
SSH_OPTS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'LogLevel=ERROR',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=2']

# Runs on the host. Arguments: run folder, mtime of the state.json we hold.
# The tar goes to stdout; what this side needs to know goes to stderr.
_REMOTE = r'''
cd "$1" || { echo "MIRROR_ERROR: no run folder $1" >&2; exit 3; }
P=$(sed -n 's/^ *"pid": *\([0-9][0-9]*\).*/\1/p' status.json 2>/dev/null | head -1)
if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then echo "ALIVE=1" >&2; else echo "ALIVE=0" >&2; fi
M=$(stat -c %Y state.json 2>/dev/null || echo 0)
echo "STATE_MTIME=$M" >&2
F=status.json
if [ "$M" -gt "$2" ]; then F="$F state.json report.html"; fi
tar -cf - --ignore-failed-read $F 2>/dev/null
'''


def fetch(host, remote_dir, since):
    """(alive, state_mtime, {name: bytes}) from the host. Raises on failure."""
    proc = subprocess.run(['ssh'] + SSH_OPTS + [host, 'bash -s -- %s %d' % (shlex.quote(remote_dir), since)],
                          input=_REMOTE.encode(), capture_output=True, timeout=120)
    err = proc.stderr.decode('utf-8', 'replace')
    if 'MIRROR_ERROR:' in err or proc.returncode != 0:
        raise RuntimeError((err.strip().splitlines() or ['ssh rc %d' % proc.returncode])[-1])
    alive = 'ALIVE=1' in err
    mtime = int(err.split('STATE_MTIME=', 1)[1].split()[0]) if 'STATE_MTIME=' in err else 0
    files = {}
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        for member in tar.getmembers():
            # Only the expected names, never a path: nothing from the host
            # can write outside the mirror folder.
            if member.isfile() and member.name in FILES:
                files[member.name] = tar.extractfile(member).read()
    return alive, mtime, files


def write_atomic(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fil:
        fil.write(data)
    os.replace(tmp, path)


def main():
    user = CONFIG['hacc_host'].split('@')[0] if '@' in CONFIG['hacc_host'] else 'vdixit'
    host_default = CONFIG['hacc_host'] if CONFIG['hacc_host'] != 'local' else 'vdixit@hacc-build-02'
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--run', required=True)
    ap.add_argument('--host', default=host_default)
    ap.add_argument('--remote', default='/local/home/%s/agenticRTL' % user,
                    help='the repo on the host (default: %(default)s)')
    ap.add_argument('--every', type=float, default=10.0, help='seconds between copies')
    ap.add_argument('--gui', action='store_true', help='also open the dashboard on the copy')
    args = ap.parse_args()

    here = os.path.join(MIRRORS, args.run)
    os.makedirs(here, exist_ok=True)
    remote_dir = '%s/.agentic/runs/%s' % (args.remote.rstrip('/'), args.run)
    print('mirroring %s:%s into %s every %g s (Ctrl-C to stop)'
          % (args.host, remote_dir, here, args.every), flush=True)

    gui = None
    since = 0
    last_ok = None
    while True:
        try:
            alive, mtime, files = fetch(args.host, remote_dir, since)
            for name, data in files.items():
                write_atomic(os.path.join(here, name), data)
            if 'state.json' in files:
                since = mtime
            now = time.time()
            write_atomic(os.path.join(here, 'mirror.json'), json.dumps(
                {'host': args.host, 'remote': remote_dir, 'alive': alive,
                 'checked': now, 'state_mtime': since}, indent=1).encode())
            if last_ok is None or now - last_ok > 600:
                print('%s  host reached; loop %s' % (time.strftime('%H:%M:%S'),
                                                     'running' if alive else 'NOT running'), flush=True)
            last_ok = now
        except Exception as exc:     # the VPN dropped, the laptop woke up: try again
            print('%s  no copy: %s' % (time.strftime('%H:%M:%S'), str(exc)[:200]), flush=True)
        if args.gui and gui is None and os.path.exists(os.path.join(here, 'state.json')):
            gui = subprocess.Popen([sys.executable, os.path.join(KIT, 'gui.py'),
                                    '--runs-dir', MIRRORS, '--run', args.run])
        if gui is not None and gui.poll() is not None:
            print('dashboard closed; mirror stopped', flush=True)
            return 0
        try:
            time.sleep(args.every)
        except KeyboardInterrupt:
            break
    if gui is not None:
        gui.terminate()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
