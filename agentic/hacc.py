#!/usr/bin/env python3
"""
Synthesis on the HACC build host at ETH: Vivado, over ssh, for an FPGA part.

The Yosys backend (syn/synth.sh) measures the design on a 45nm ASIC library it
will never be built on, and it blackboxes the history RAM. campaign1's final
design clocks at 641.8 MHz there and at 263.0 MHz under Vivado on the U55C;
an older, larger revision missed 250 MHz under Vivado with half of its ten
worst paths starting at a URAM output -- paths the Yosys flow cannot see. So
f_max, and with it throughput, means something different on each; this is the
one that matches the hardware.

For one candidate:

    stage   its rtl/*.vhd (minus *.sim.vhd and *.syn.vhd, as synth.sh does),
            the kit's frozen syn/ram_xilinx.vhd and syn/vivado.tcl, as an
            in-memory tar sent as bytes
    run     Vivado in a scratch directory under /tmp on the host
    fetch   the three reports, into the candidate's synth folder here
    parse   LUTs, registers, BRAM, URAM, worst slack, the worst path's ends

Several candidates are measured at once and each gets its own scratch
directory, so they run side by side on the host.

WHY THE HOST HAS ITS OWN WATCHDOG
---------------------------------
Losing the connection (a laptop asleep, a VPN that drops) does not reliably
end a batch job on the far side, and one that survives holds cores and a
licence seat on a shared machine for as long as place-and-route cares to run.
So Vivado runs in a session of its own with a watchdog inside it that kills
the whole session at the deadline, whether or not anyone is still listening.
If this side gives up first, its cleanup kills that session instead. A first
version kept the watchdog outside the session; a lost connection orphaned its
sleep, which would have woken hours later and killed whatever session had
reused the number by then.
"""

import io
import os
import re
import shlex
import tarfile
import time

import tools
from tools import CONFIG, run as tools_run

KIT = os.path.dirname(os.path.abspath(__file__))
TCL_FILE = os.path.join(KIT, 'syn', 'vivado.tcl')
RAM_FILE = os.path.join(KIT, 'syn', 'ram_xilinx.vhd')

# The name the kit's RAM is staged under, chosen so no candidate file can
# plausibly collide with it (and one that does is refused, not overwritten).
RAM_STAGED = 'agentic_kit_ram_xilinx.vhd'

REPORTS = ('timing.log', 'utilization.log', 'critical_paths.log')

# BatchMode: a missing key fails at once instead of waiting on a password
# prompt nobody will answer. LogLevel=ERROR also silences the cluster's
# login banner. ServerAlive notices a dead connection in about five minutes.
SSH_OPTS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'LogLevel=ERROR',
            '-o', 'ServerAliveInterval=60', '-o', 'ServerAliveCountMax=5']

QUICK_TIMEOUT_S = 300      # anything that is not the synthesis itself
LOCAL_GRACE_S = 600        # past the host's deadline, only the link has hung
# Failures that are about the host, not the design, are retried with these
# pauses (the last repeating) until hacc_wait_min runs out. Three tries in two
# minutes once lost both candidates of an iteration to a dropped VPN.
RETRY_PAUSES_S = (30, 60, 120, 300)

# Substrings that make a Vivado failure environmental rather than about the
# design. Short on purpose: a wrong guess turns one real failure into three.
_TRANSIENT = ('license', 'licensing', 'cannot connect', 'temporary failure',
              'resource temporarily unavailable', 'no space left')

# Runs on the host. Arguments: directory, top, part, period (ns), limit (s).
#
# Vivado and its watchdog share one session, and that session is the only
# thing that outlives the connection, so one `pkill -s <leader>` removes all
# of it. setsid from a non-interactive shell's background job does not fork,
# so $! is the session leader and its pid is the session id.
_RUN = r'''
DIR="$1"; TOP="$2"; PART="$3"; PERIOD="$4"; LIMIT="$5"
cd "$DIR" || { echo "REMOTE_ERROR: no directory $DIR"; exit 0; }
if ! source "$6" >/dev/null 2>&1; then
  echo "REMOTE_ERROR: cannot source $6"; exit 0
fi
setsid bash -c '
  vivado -nolog -nojournal -mode batch -source vivado.tcl \
    -tclargs "$1" "$2" "$3" > vivado.out 2>&1 < /dev/null &
  V=$!
  ( sleep "$4" && echo "WATCHDOG: killed after $4 s" >> vivado.out \
      && pkill -KILL -s $$ ) &
  wait "$V"; echo "$?" > vivado.rc
  pkill -KILL -s $$
' _ "$TOP" "$PART" "$PERIOD" "$LIMIT" < /dev/null > /dev/null 2>&1 &
echo "$!" > vivado.pid
wait
echo "VIVADO_EXIT=$(cat vivado.rc 2>/dev/null || echo killed)"
tail -c 4000 vivado.out
'''


class HaccError(RuntimeError):
    """The synthesis could not produce numbers. Not retried."""


class Transient(HaccError):
    """A failure about the host or the link, which a retry may not repeat."""


def _host():
    return CONFIG['hacc_host']


def _ssh(command, data=None, timeout=QUICK_TIMEOUT_S):
    """Run ``command`` on the host. Bytes in, bytes out.

    Text mode on Windows rewrites line endings in anything passing through,
    harmless for a log and fatal for a tar stream.
    """
    argv = [os.environ.get('HACC_SSH', 'ssh')] + SSH_OPTS + [_host(), command]
    res = tools_run(argv, timeout=timeout, stdin_bytes=data, binary=True)
    if res.timed_out:
        raise Transient('ssh to %s did not finish within %d s' % (_host(), timeout))
    if res.rc == 255:
        err = res.err.decode('utf-8', 'replace').strip().splitlines()
        reason = err[-1] if err else 'no message'
        if 'Permission denied' in reason:
            # The same on every attempt; not worth three.
            raise HaccError('ssh to %s refused the key: %s' % (_host(), reason))
        raise Transient('ssh to %s failed: %s' % (_host(), reason))
    return res


# --------------------------------------------------------------------------
# what is sent

def synth_files(rtl_dir):
    """The candidate's synthesisable VHDL, exactly as syn/synth.sh picks it."""
    return sorted(name for name in os.listdir(rtl_dir)
                  if name.endswith('.vhd')
                  and not name.endswith(('.sim.vhd', '.syn.vhd')))


_RECORD = r'type\s+%s\s+is\s+record(.*?)end\s+record'


def ram_interface_problem(rtl_dir):
    """Why the kit's fixed RAM cannot stand in for this candidate's, or None.

    The Xilinx RAM wires exactly 8 data bytes and 8 control bits through. A
    candidate that widened the ram_command/ram_response records would still
    compile against it -- VHDL is happy to leave the extra bytes unconnected
    -- and Vivado would then remove every register that only fed them. The
    candidate would measure smaller and faster for having broken its memory.
    The Yosys stub passes whatever the records hold, so this is the one
    backend where that change has to be caught explicitly.
    """
    names = synth_files(rtl_dir)
    for name in names:
        with open(os.path.join(rtl_dir, name), encoding='utf-8',
                  errors='replace') as fil:
            text = fil.read()
        if re.search(r'\bentity\s+vhsnunzip_ram\s+is\b', text, re.I):
            return ('%s declares entity vhsnunzip_ram, which would replace the '
                    'fixed memory synthesis measures every candidate with' % name)
    pkg = os.path.join(rtl_dir, 'vhsnunzip_int_pkg.vhd')
    if not os.path.exists(pkg):
        return 'rtl/vhsnunzip_int_pkg.vhd, which defines the RAM records, is missing'
    with open(pkg, encoding='utf-8', errors='replace') as fil:
        text = re.sub(r'--[^\n]*', '', fil.read())
    need = {'ram_command': (r'\bwdat\s*:\s*byte_array\s*\(\s*0\s+to\s+7\s*\)',
                            r'\bwctrl\s*:\s*std_logic_vector\s*\(\s*7\s+downto\s+0\s*\)'),
            'ram_response': (r'\brdat\s*:\s*byte_array\s*\(\s*0\s+to\s+7\s*\)',
                             r'\brctrl\s*:\s*std_logic_vector\s*\(\s*7\s+downto\s+0\s*\)')}
    for record, fields in need.items():
        body = re.search(_RECORD % record, text, re.S | re.I)
        if not body or not all(re.search(f, body.group(1), re.I) for f in fields):
            return ('the %s record no longer carries 8 data bytes and 8 control '
                    'bits; synthesis uses a fixed Xilinx RAM of that width, so '
                    'this change would not be measured' % record)
    return None


def _stage(rtl_dir):
    """The candidate plus the kit's RAM and script, as a tar archive.

    Line endings are normalised: a Windows checkout may carry CRLF, and a
    carriage return is not something to find out about on the host.
    """
    names = synth_files(rtl_dir)
    if RAM_STAGED in names:
        raise HaccError('a candidate file is named %s, which the kit reserves'
                        % RAM_STAGED)
    entries = [('rtl/' + n, os.path.join(rtl_dir, n)) for n in names]
    entries += [('rtl/' + RAM_STAGED, RAM_FILE), ('vivado.tcl', TCL_FILE)]

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for arcname, path in entries:
            with open(path, 'rb') as fil:
                data = fil.read().replace(b'\r\n', b'\n')
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --------------------------------------------------------------------------
# one run on the host

def run_remote(rtl_dir, out_dir, top, limit_s):
    """Synthesise on the host and leave the reports in ``out_dir``."""
    part = CONFIG['hacc_part']
    period_ns = '%g' % (float(CONFIG['clock_period_ps']) / 1000.0)
    for name in (top, part):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            raise HaccError('refusing to pass %r to a remote shell' % name)

    os.makedirs(out_dir, exist_ok=True)
    for name in REPORTS + ('vivado.log',):
        # A failure below must not leave the last run's reports to be parsed
        # as this one's.
        stale = os.path.join(out_dir, name)
        if os.path.exists(stale):
            os.remove(stale)

    remote = _ssh('mktemp -d /tmp/agentic-synth.XXXXXX').out.decode().strip()
    if not remote.startswith('/tmp/agentic-synth.'):
        raise Transient('could not create a working directory on %s' % _host())

    finished = False
    try:
        _ssh('tar -xf - -C %s' % shlex.quote(remote), data=_stage(rtl_dir))
        args = (remote, top, part, period_ns, int(limit_s),
                CONFIG['hacc_vivado_settings'])
        res = _ssh('bash -s -- ' + ' '.join(shlex.quote(str(a)) for a in args),
                   data=_RUN.encode(), timeout=int(limit_s) + LOCAL_GRACE_S)
        out = res.out.decode('utf-8', 'replace')

        if 'REMOTE_ERROR:' in out:
            raise HaccError(out.split('REMOTE_ERROR:', 1)[1].strip().splitlines()[0])
        code = re.search(r'VIVADO_EXIT=(\S+)', out)
        if not code:
            raise Transient('the run on %s ended without an exit status; the '
                            'connection was probably lost' % _host())
        tail = out[code.end():]
        if 'WATCHDOG: killed' in tail:
            # Deterministic enough: the same design would take as long again.
            raise HaccError('vivado did not finish within %d s' % int(limit_s))
        if code.group(1) != '0':
            errors = [l for l in tail.splitlines() if l.startswith(('ERROR', 'CRITICAL'))]
            msg = 'vivado failed on %s:\n%s' % (
                _host(), '\n'.join(errors[-12:]) or tail[-1500:])
            if code.group(1) == 'killed' or any(t in tail.lower() for t in _TRANSIENT):
                raise Transient(msg)
            raise HaccError(msg)

        fetched = _ssh('cd %s && tar -cf - --ignore-failed-read reports'
                       % shlex.quote(remote))
        with tarfile.open(fileobj=io.BytesIO(fetched.out)) as tar:
            for member in tar.getmembers():
                # Checked against a fixed list, never trusted: nothing from
                # the far side can write outside out_dir.
                name = member.name.rsplit('/', 1)[-1]
                if member.isfile() and name in REPORTS:
                    with open(os.path.join(out_dir, name), 'wb') as dst:
                        dst.write(tar.extractfile(member).read())
        with open(os.path.join(out_dir, 'vivado.log'), 'w', encoding='utf-8') as fil:
            fil.write(tail)
        finished = True
    finally:
        # On any failure, nothing may stay running before the directory goes:
        # a retry must not share the host with its own ghost.
        cleanup = 'rm -rf %s' % shlex.quote(remote)
        if not finished:
            cleanup = ('P=$(cat %s/vivado.pid 2>/dev/null) && [ -n "$P" ] '
                       '&& pkill -KILL -s "$P"; %s' % (shlex.quote(remote), cleanup))
        try:
            _ssh(cleanup, timeout=120)
        except HaccError:
            pass


# --------------------------------------------------------------------------
# reading the reports

_UTIL_ROWS = (('CLB LUTs', 'luts'), ('CLB Registers', 'regs'),
              ('Block RAM Tile', 'bram'), ('URAM', 'uram'), ('DSPs', 'dsp'))

# Worst slack has been printed in more than one shape across releases.
_WNS_PATTERNS = (
    (r'WNS\(ns\).*?\n\s*-[-\s]*\n\s*(-?\d+\.\d+)', re.S),
    (r'WNS\(ns\)\s*:?\s*(-?\d+\.\d+)', 0),
    (r'Slack \(M?ET\)\s*:\s*(-?\d+\.\d+)\s*ns', 0),
)
# The summary's data row: WNS, TNS, failing endpoints, total endpoints.
_SUMMARY_ROW = re.compile(r'WNS\(ns\)\s+TNS\(ns\).*?\n\s*-[-\s]*\n\s*'
                          r'(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)', re.S)


def _read(out_dir, name):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        raise HaccError('%s was not produced' % name)
    with open(path, encoding='utf-8', errors='replace') as fil:
        return fil.read()


def pretty_pin(pin):
    """A Vivado cell pin as the RTL names the thing behind it.

    core_inst/datapath_inst/proc.off_reg[7]/D  ->  core_inst.datapath_inst.proc.off[7]
    """
    name = re.sub(r'/[A-Za-z0-9_]+$', '', pin.strip())     # the pin itself
    name = re.sub(r'_reg(?=\[|$)', '', name)                 # flop naming
    return name.replace('/', '.')


def parse_reports(out_dir):
    """Metrics out of the three reports. Raises HaccError when unreadable."""
    out = {'synth_backend': 'hacc'}
    util = _read(out_dir, 'utilization.log')
    for label, key in _UTIL_ROWS:
        row = re.search(r'^\|\s*' + re.escape(label) + r'\s*\|\s*(\d+)', util, re.M)
        if row:
            out[key] = int(row.group(1))
    if 'luts' not in out:
        raise HaccError('no CLB LUTs row in utilization.log; the report format '
                        'has changed and hacc._UTIL_ROWS needs updating')
    device = re.search(r'^\|\s*Device\s*:\s*(\S+)', util, re.M)
    out['part'] = device.group(1) if device else CONFIG['hacc_part']
    out['area'] = out['luts']
    out['area_unit'] = 'LUTs'

    timing = _read(out_dir, 'timing.log')
    for pattern, flags in _WNS_PATTERNS:
        found = re.search(pattern, timing, flags)
        if found:
            wns = float(found.group(1))
            break
    else:
        raise HaccError('no worst slack in timing.log matched any known shape; '
                        'without it there is no f_max and so no throughput')
    period_ns = float(CONFIG['clock_period_ps']) / 1000.0
    out['wns_ns'] = round(wns, 3)
    out['f_max_mhz'] = round(1000.0 / (period_ns - wns), 1)
    row = _SUMMARY_ROW.search(timing)
    if row:
        out['failing_endpoints'] = int(row.group(3))
        out['total_endpoints'] = int(row.group(4))

    try:
        paths = _read(out_dir, 'critical_paths.log')
    except HaccError:
        return out                         # advisory only
    src = re.search(r'^\s*Source:\s+(\S+)', paths, re.M)
    dst = re.search(r'^\s*Destination:\s+(\S+)', paths, re.M)
    if src and dst:
        out['critical_path'] = {'from': pretty_pin(src.group(1)),
                                'to': pretty_pin(dst.group(1))}
    return out


# --------------------------------------------------------------------------

def synthesize(rtl_dir, out_dir, top):
    """Area and timing for one rtl/ folder, from Vivado on the host.

    A failure about the host rather than the design (ssh cannot connect, the
    VPN is down, the link drops mid-run) is retried until hacc_wait_min has
    passed, so a laptop that loses the VPN for a while pauses a measurement
    instead of failing it. Raises Transient if the host never came back.
    """
    problem = ram_interface_problem(rtl_dir)
    if problem:
        raise HaccError(problem)
    deadline = time.time() + 60.0 * float(CONFIG.get('hacc_wait_min') or 0)
    attempt = 0
    while True:
        attempt += 1
        if tools.STOPPING:
            raise HaccError('the loop is stopping; synthesis not started')
        try:
            run_remote(rtl_dir, out_dir, top, int(CONFIG['vivado_timeout_s']))
            break
        except Transient:
            pause = RETRY_PAUSES_S[min(attempt, len(RETRY_PAUSES_S)) - 1]
            if tools.STOPPING or time.time() + pause > deadline:
                raise
            time.sleep(pause)
    metrics = parse_reports(out_dir)
    metrics['synth_attempts'] = attempt
    return metrics


def available():
    """(ok, message): does the host answer, with the pinned Vivado on it?"""
    try:
        res = _ssh('test -f %s && echo ok'
                   % shlex.quote(CONFIG['hacc_vivado_settings']), timeout=60)
    except HaccError as exc:
        return False, str(exc)
    if b'ok' not in res.out:
        return False, 'no Vivado at %s on %s' % (CONFIG['hacc_vivado_settings'], _host())
    return True, 'Vivado on %s for %s' % (_host(), CONFIG['hacc_part'])
