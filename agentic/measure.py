#!/usr/bin/env python3
"""
Measure one design: correct or not, bytes per cycle, f_max, area, throughput.

    throughput (GB/s) = bytes_per_cycle x f_max (MHz) / 1000

  * bytes/cycle comes from the throughput testbench under GHDL, on every draw
    in the corpus. Correctness is decided on every draw by agentic/oracle.py
    against the frozen reference decompressor.
  * f_max and area come from the synthesis backend named in the config:
    GHDL -> Yosys -> ABC on the Nangate 45nm library (area in um2), or
    Vivado place-and-route for an FPGA part on the HACC host (agentic/hacc.py,
    area in LUTs).

The small draws run first, and a design that fails one of them gets neither
the long draws nor a synthesis run. Otherwise synthesis runs while the long
draws simulate: whole Parquet row groups take a quarter of an hour or more
to simulate, and neither half needs the other's result.

The loop always measures with ITS OWN copy of the testbench, the RAM stand-in
and the scripts (this folder), never with copies inside a candidate's
worktree. Only the candidate's rtl/ folder is taken from the worktree.

Usage:
    python agentic/measure.py [--rtl DIR] [--no-synth] [--json FILE]
"""

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

import analyse                                   # noqa: E402
import hacc                                      # noqa: E402
import oracle                                    # noqa: E402
import stim                                      # noqa: E402
from tools import (CONFIG, ROOT, eda_shell, geomean, read_json,  # noqa: E402
                   shell_path, write_json)

TB_FILE = os.path.join(KIT, 'tb', 'vhsnunzip_perf_tc.sim.08.vhd')
STUB_FILE = os.path.join(KIT, 'syn', 'ram_stub.vhd')
LIB_FILE = os.path.join(KIT, 'syn', 'lib', 'NangateOpenCellLibrary_typical.lib')
SIM_SCRIPT = os.path.join(KIT, 'syn', 'sim_draws.sh')
SYNTH_SCRIPT = os.path.join(KIT, 'syn', 'synth.sh')
TOP = 'vhsnunzip_unbuffered'
# Simulated time at which a draw is abandoned, at one cycle per nanosecond.
# The testbench's own watchdog ends a deadlock after 300,000 cycles without
# output, so this only stops a design that is alive but hopelessly slow: the
# longest real draw needs 3.5 million cycles on the original design.
STOP_TIME = '100ms'
# A draw whose cs.tv is at most this big simulates in seconds (the synthetic
# draws, nation, region); it runs before synthesis is started.
QUICK_DRAW_BYTES = 1 << 20

_DELAY = re.compile(r'Delay\s*=\s*([\d.]+)\s*ps')
_AREA = re.compile(r'Chip area for module .*?:\s*([\d.]+)')
_SEQ_AREA = re.compile(r'used for sequential elements:\s*([\d.]+)')
# A stat -liberty row with a real area number: "     3120 1.41E+04   DFF_X1".
# Rows for wires/ports ("38638 - wires"), internal $cells and the total row
# ("38473 9.02E+04 cells") are not cells and are skipped below.
_CELL = re.compile(r'^\s*(\d+)\s+(\d[\d.E+-]*)\s+([A-Za-z]\S*)\s*$', re.M)
# ABC names the ends of the critical path after the nets that carry it:
# "Start-point = pi19775 ($auto$dfflibmap...$238014). End-point = po18498 (...)"
_ABC_ENDS = re.compile(r'Start-point\s*=\s*\S+\s*\(([^)]*)\)\.?\s*'
                       r'End-point\s*=\s*\S+\s*\(([^)]*)\)')
_DFF_CELL = re.compile(r'^\s*cell\s+\\DFF\S*\s+(\S+)', re.M)


class MeasureError(RuntimeError):
    """The tools could not produce numbers that can be believed."""


class HostUnreachable(MeasureError):
    """The synthesis host could not be reached, even after waiting for it.
    Says nothing about the design."""


def corpus_dir(root=None):
    return os.path.join(root or ROOT, '.agentic', 'corpus')


def prepare_corpus(root=None, seed=0):
    """Build every draw's cs.tv. Returns [(draw, dir, chunks), ...].

    ``seed`` is accepted for runs that recorded one; whole row groups are
    not sampled, so nothing depends on it.
    """
    return stim.build_all(corpus_dir(root))


def draw_bytes(entry):
    """Size of a draw's stimulus file, which is what its simulation costs."""
    try:
        return os.path.getsize(os.path.join(entry[1], 'cs.tv'))
    except OSError:
        return 0


def _parse_perf(path):
    out = {}
    with open(path, encoding='ascii', errors='replace') as fil:
        for line in fil:
            if '=' in line:
                key, val = line.strip().split('=', 1)
                try:
                    out[key] = int(val)
                except ValueError:
                    pass
    for key in ('cycles', 'bytes_out'):
        if key not in out:
            raise MeasureError('perf.txt has no %s' % key)
    return out


def os_killed(rc, deadlock):
    """Whether a draw's simulator was killed from outside (SIGKILL, rc 137)
    rather than finishing, failing or hanging on its own. A design bug makes
    GHDL exit with an error or run into the stop time, never this."""
    return rc == 137 and not deadlock


def _run_draws(rtl_dir, build_dir, generics, draws, timeout, jobs=None):
    """Compile once and simulate ``draws``: {draw dir as the shell sees it:
    (rc, deadlock)}. ``jobs`` overrides how many simulate at once."""
    # Longest first: the round ends when its last simulator does, and a long
    # draw started last would run on alone after the others had finished.
    ordered = sorted(draws, key=lambda e: -draw_bytes(e))
    specs = ' '.join('"%s:%d"' % (shell_path(d), c) for _draw, d, c in ordered)
    script = ('%sbash "%s" "%s" "%s" "%s" "%s" %s %s'
              % ('SIM_JOBS=%d ' % jobs if jobs else '',
                 shell_path(SIM_SCRIPT), shell_path(rtl_dir),
                 shell_path(TB_FILE), shell_path(build_dir), generics,
                 STOP_TIME, specs))
    res = eda_shell(script, timeout=timeout or CONFIG['sim_timeout_s'],
                    kill_token='sim-' + os.path.basename(os.path.dirname(build_dir)))
    text = res.text
    if res.timed_out:
        raise MeasureError('simulation did not finish within %d s'
                           % (timeout or CONFIG['sim_timeout_s']))
    if 'COMPILE_FAIL' in text or 'ELAB_FAIL' in text:
        tail = [l for l in text.splitlines() if l.strip()]
        raise MeasureError('the design does not compile:\n' + '\n'.join(tail[-25:]))
    if 'ELAB_OK' not in text:
        raise MeasureError('unexpected simulation output:\n' + text[-1500:])
    status = {}
    for line in text.splitlines():
        m = re.match(r'DRAW (\S+) rc=(-?\d+) deadlock=(\d)', line)
        if m:
            status[m.group(1)] = (int(m.group(2)), m.group(3) == '1')
    return status


def simulate(rtl_dir, build_dir, draws, widths, timeout=None, jobs=None):
    """Run every draw against one rtl/ folder. Returns per-draw results.

    ``draws`` is [(draw, dir, chunks), ...]. The result for each draw is a
    dict with oracle_pass, bytes_per_cycle, counters, and a problem string
    when something went wrong. Raises MeasureError when the design does not
    even compile. ``jobs`` is how many draws simulate at once (default: the
    script's own, 3).
    """
    if widths.get('unresolved'):
        raise MeasureError('cannot read the width of %s from the RTL: declare '
                           'top-level ports with a literal range, e.g. '
                           'std_logic_vector(127 downto 0)'
                           % ', '.join(widths['unresolved']))
    generics = ('-gCO_BYTES=%d -gCO_CNT_BITS=%d -gDE_BYTES=%d -gDE_CNT_BITS=%d'
                % (widths['in_bytes'], widths['in_cnt_bits'],
                   widths['out_bytes'], widths['out_cnt_bits']))
    if widths['in_bytes'] % 8 != 0:
        raise MeasureError('co_data is %d bytes wide; the harness needs a '
                           'multiple of 8' % widths['in_bytes'])

    # Every measurement simulates in its own copies of the draw folders.
    # Several candidates are measured at once, and the testbench writes
    # perf.txt/out.hex into the folder it runs in, so a shared folder would
    # let one candidate's results overwrite another's.
    private = []
    for draw, ddir, chunks in draws:
        mine = os.path.join(build_dir, 'draws', draw.name)
        os.makedirs(mine, exist_ok=True)
        shutil.copyfile(os.path.join(ddir, 'cs.tv'), os.path.join(mine, 'cs.tv'))
        private.append((draw, mine, chunks))
    draws = private

    status = _run_draws(rtl_dir, build_dir, generics, draws, timeout, jobs=jobs)
    # A simulator the operating system killed has said nothing about the
    # design, so those draws are run again, one at a time, before anything is
    # read into them. It is not hypothetical: with two cores each simulator
    # needs about a gigabyte, and ten sound candidates were once recorded as
    # failing correctness because the out-of-memory killer took theirs.
    killed = [d for d in draws if os_killed(*status.get(shell_path(d[1]), (99, False)))]
    if killed:
        status.update(_run_draws(rtl_dir, build_dir, generics, killed, timeout, jobs=1))

    results = []
    for draw, ddir, chunks in draws:
        rc, deadlock = status.get(shell_path(ddir), (99, False))
        rec = {'name': draw.name, 'kind': draw.kind, 'scored': draw.scored,
               'visible': draw.visible, 'chunks': chunks,
               'oracle_pass': False, 'bytes_per_cycle': None, 'problem': None}
        perf = os.path.join(ddir, 'perf.txt')
        out_hex = os.path.join(ddir, 'out.hex')
        cs_tv = os.path.join(ddir, 'cs.tv')
        if os_killed(rc, deadlock):
            rec['problem'] = ('the simulator was killed by the operating system (rc 137, '
                              'most likely out of memory), also when run on its own; '
                              'this is not a verdict on the design')
            rec['os_killed'] = True
        elif deadlock:
            rec['problem'] = 'deadlock: the simulation did not finish (hung waiting for output)'
        elif rc != 0 or not os.path.exists(perf):
            log = os.path.join(ddir, 'sim.log')
            tail = ''
            if os.path.exists(log):
                with open(log, encoding='utf-8', errors='replace') as fil:
                    tail = ' | '.join(l.strip() for l in fil.read().splitlines()
                                      if l.strip())[-400:]
            rec['problem'] = 'simulation failed (rc %d): %s' % (rc, tail)
        else:
            problems = oracle.check(cs_tv, out_hex)
            counters = _parse_perf(perf)
            rec['counters'] = counters
            if problems:
                rec['problem'] = 'wrong output: ' + problems[0]
            else:
                rec['oracle_pass'] = True
                rec['bytes_per_cycle'] = round(counters['bytes_out']
                                               / max(1, counters['cycles']), 4)
                rec['output_idle_pct'] = round(100.0 * counters.get('de_bubble', 0)
                                               / max(1, counters['cycles']), 1)
                rec['input_stall_pct'] = round(100.0 * counters.get('co_stall', 0)
                                               / max(1, counters['cycles']), 1)
                try:
                    rec['analysis'] = analyse.analyse(counters, cs_tv, widths)
                except Exception as exc:       # the profile is advisory only
                    rec['analysis'] = None
                    rec['analysis_error'] = str(exc)[:200]
        results.append(rec)
        # A row group leaves about 100 MB of text per candidate, and the
        # counters and the verdict are all that is kept. The stimulus is a
        # copy; a wrong output stays for whoever wants to look at it.
        for name in ('cs.tv',) + (('out.hex',) if rec['oracle_pass'] else ()):
            try:
                os.remove(os.path.join(ddir, name))
            except OSError:
                pass
    return results


def parse_synth(text):
    delays = _DELAY.findall(text)
    if not delays:
        raise MeasureError('no ABC timing line in the synthesis output')
    delay_ps = float(delays[-1])
    if delay_ps <= 0:
        raise MeasureError('ABC reported a delay of %s ps' % delay_ps)
    area = _AREA.search(text)
    if not area:
        raise MeasureError('no chip area in the synthesis output')
    period = float(CONFIG['clock_period_ps'])
    out = {
        'delay_ps': round(delay_ps, 2),
        'wns_ns': round((period - delay_ps) / 1000.0, 3),
        'f_max_mhz': round(1000000.0 / delay_ps, 1),
        'area_um2': round(float(area.group(1)), 1),
    }
    seq = _SEQ_AREA.search(text)
    if seq:
        out['seq_area_um2'] = round(float(seq.group(1)), 1)
    return out


def _pretty_net(name):
    """An RTL signal name out of a yosys net name, or '' if it has none."""
    name = (name or '').strip()
    name = re.sub(r'^\$flatten\\?', '', name)
    name = name.replace('\\', '')
    if not name or name.startswith('$'):
        return ''
    return name.strip()


def critical_path(out_dir, text):
    """The worst path's two ends, named as the RTL names them.

    ABC reports the path by net name, and those names are meaningless on their
    own ($auto$dfflibmap$238014). The register dump taken just before ABC ran
    says which register each of those nets belongs to, which turns the report
    into "from cmd_gen_2 c1h(0) to cmd_gen_2 li_off(5)" -- something a session
    can act on instead of guessing which logic is slow.
    """
    found = _ABC_ENDS.search(text or '')
    if not found:
        # synth.sh only echoes the summary lines; the path is in the full log.
        try:
            with open(os.path.join(out_dir, 'yosys.log'),
                      encoding='utf-8', errors='replace') as fil:
                found = _ABC_ENDS.search(fil.read())
        except OSError:
            found = None
    if not found:
        return None
    start_net, end_net = found.group(1).strip(), found.group(2).strip()
    path = os.path.join(out_dir, 'dffs.txt')
    by_qn, by_d = {}, {}
    try:
        with open(path, encoding='utf-8', errors='replace') as fil:
            conn = {}
            for line in fil:
                line = line.strip()
                if line.startswith('cell '):
                    conn = {}
                elif line.startswith('connect '):
                    parts = line.split(None, 2)
                    if len(parts) == 3:
                        conn[parts[1].lstrip('\\')] = parts[2].strip()
                elif line == 'end' and conn.get('Q'):
                    qname = conn['Q']
                    if conn.get('QN'):
                        by_qn[conn['QN']] = qname
                    if conn.get('D'):
                        by_d[conn['D']] = qname
    except OSError:
        return None
    start = _pretty_net(by_qn.get(start_net) or start_net)
    end = _pretty_net(by_d.get(end_net) or end_net)
    if not start and not end:
        return None
    return {'from': start or '(an input or unnamed net)',
            'to': end or '(an output or unnamed net)'}


def synthesize(rtl_dir, out_dir, timeout=None):
    """Area and timing for one rtl/ folder, from the configured backend.

    Raises MeasureError. Every result carries `area`, `area_unit` and
    `synth_backend`, so a comparison across backends can be refused rather
    than made.
    """
    backend = CONFIG['synth_backend']
    if backend == 'hacc':
        try:
            return hacc.synthesize(rtl_dir, out_dir, TOP)
        except hacc.Transient as exc:
            raise HostUnreachable(str(exc))
        except hacc.HaccError as exc:
            raise MeasureError(str(exc))
    if backend != 'yosys':
        raise MeasureError('unknown synth_backend %r in the config (yosys or hacc)'
                           % backend)
    return _synthesize_yosys(rtl_dir, out_dir, timeout)


def _synthesize_yosys(rtl_dir, out_dir, timeout=None):
    if not os.path.exists(LIB_FILE):
        raise MeasureError('no liberty file at %s (run agentic/syn/get_lib.sh)'
                           % LIB_FILE)
    script = ('bash "%s" "%s" "%s" "%s" "%s" %s %d'
              % (shell_path(SYNTH_SCRIPT), shell_path(rtl_dir),
                 shell_path(STUB_FILE), shell_path(LIB_FILE),
                 shell_path(out_dir), TOP, int(CONFIG['clock_period_ps'])))
    res = eda_shell(script, timeout=timeout or CONFIG['synth_timeout_s'],
                    kill_token='synth-' + os.path.basename(os.path.dirname(out_dir)))
    if res.timed_out:
        raise MeasureError('synthesis did not finish within %d s'
                           % (timeout or CONFIG['synth_timeout_s']))
    text = res.text
    if 'SYNTH_OK' not in text:
        tail = [l for l in text.splitlines() if l.strip()]
        raise MeasureError('synthesis failed:\n' + '\n'.join(tail[-25:]))
    metrics = parse_synth(text)
    worst = critical_path(out_dir, text)
    if worst:
        metrics['critical_path'] = worst
    try:                                   # several MB, and only needed once
        os.remove(os.path.join(out_dir, 'dffs.txt'))
    except OSError:
        pass
    stat = os.path.join(out_dir, 'stat.log')
    if os.path.exists(stat):
        with open(stat, encoding='utf-8', errors='replace') as fil:
            block = fil.read()
        cells = {name: int(n) for n, _area, name in _CELL.findall(block)
                 if name != 'cells'}
        regs = sum(n for name, n in cells.items()
                   if name.startswith(('DFF', 'SDFF')))
        if regs:
            metrics['regs'] = regs
        if cells:
            metrics['cells'] = sum(cells.values())
    metrics.update(area=metrics['area_um2'], area_unit='um2', synth_backend='yosys')
    return metrics


def summarize(sim_results, synth_metrics):
    """Fold per-draw results and synthesis into one metrics dict."""
    out = {'oracle_pass': all(r['oracle_pass'] for r in sim_results),
           'draws': sim_results}
    failed = [r for r in sim_results if not r['oracle_pass']]
    if failed:
        out['first_problem'] = '%s: %s' % (failed[0]['name'], failed[0]['problem'])
    scored = [r['bytes_per_cycle'] for r in sim_results
              if r['scored'] and r['bytes_per_cycle']]
    visible = [r['bytes_per_cycle'] for r in sim_results
               if r['visible'] and r['bytes_per_cycle']]
    out['bytes_per_cycle'] = round(geomean(scored), 4) if scored else None
    out['bytes_per_cycle_visible'] = round(geomean(visible), 4) if visible else None
    reports = [r.get('analysis') for r in sim_results if r['scored']]
    out['profile'] = analyse.combine(reports)
    if synth_metrics:
        out.update(synth_metrics)
        if out['bytes_per_cycle']:
            out['throughput_gbps'] = round(out['bytes_per_cycle']
                                           * synth_metrics['f_max_mhz'] / 1000.0, 4)
        out['lever'] = analyse.lever(out['profile'], synth_metrics.get('wns_ns'))
    else:
        out['lever'] = analyse.lever(out['profile'])
    return out


def _synthesize_into(rtl_dir, out_dir, box):
    """synthesize() for a thread: the result or the error lands in ``box``."""
    t0 = time.time()
    try:
        box['metrics'] = synthesize(rtl_dir, out_dir)
    except HostUnreachable as exc:
        box['unreachable'] = str(exc)
    except MeasureError as exc:
        box['error'] = str(exc)
    except Exception as exc:               # a thread must not die unheard
        box['error'] = 'synthesis crashed: %s' % exc
    # Wall time including the transfer to and from the host, which is what
    # the loop waits for; hacc's own synth_seconds is the same span.
    box['seconds'] = round(time.time() - t0, 1)


def measure(rtl_dir, work_dir, draws, synth=True, log=None, jobs=None):
    """The whole measurement for one rtl/ folder. Never raises for a bad
    design; returns a dict with 'error' set when the tools could not run.

    The small draws go first. If they all pass, synthesis starts in a thread
    and the long draws simulate meanwhile; if one fails, the design is
    rejected without either. sim_seconds and synth_seconds overlap, so they
    add up to more than the measurement took. ``jobs`` is how many draws
    simulate at once.
    """
    os.makedirs(work_dir, exist_ok=True)
    widths = analyse.rtl_widths(rtl_dir)
    sim_dir = os.path.join(work_dir, 'sim')
    quick = [d for d in draws if draw_bytes(d) <= QUICK_DRAW_BYTES]
    slow = [d for d in draws if draw_bytes(d) > QUICK_DRAW_BYTES]
    t_sim = time.time()
    box, worker = {}, None
    try:
        sims = simulate(rtl_dir, sim_dir, quick, widths, jobs=jobs) if quick else []
        if all(r['oracle_pass'] for r in sims):
            if synth:
                worker = threading.Thread(
                    target=_synthesize_into, daemon=True,
                    args=(rtl_dir, os.path.join(work_dir, 'synth'), box))
                worker.start()
            if slow:
                sims += simulate(rtl_dir, sim_dir, slow, widths, jobs=jobs)
    except MeasureError as exc:
        # A synthesis already started is left to finish on its own; the
        # design is rejected whatever it reports.
        return {'oracle_pass': False, 'error': str(exc), 'widths': widths,
                'draws': [], 'sim_seconds': round(time.time() - t_sim, 1)}
    sim_seconds = round(time.time() - t_sim, 1)
    rank = dict((d.name, i) for i, (d, _p, _c) in enumerate(draws))
    sims.sort(key=lambda r: rank.get(r['name'], len(rank)))
    synth_metrics = None
    synth_error = None
    if worker is not None:
        worker.join()
        if all(r['oracle_pass'] for r in sims):
            synth_metrics, synth_error = box.get('metrics'), box.get('error')
    unreachable = box.get('unreachable') if all(r['oracle_pass'] for r in sims) else None
    out = summarize(sims, synth_metrics)
    out['widths'] = widths
    out['sim_seconds'] = sim_seconds
    killed = [r['name'] for r in sims if r.get('os_killed')]
    if killed:
        # Not a correctness failure: the measurement itself did not happen.
        out['measure_error'] = ('the simulator was killed by the operating system '
                                '(most likely out of memory) on %s, even when run on '
                                'its own; nothing was learned about the design'
                                % ', '.join(killed))
    elif unreachable:
        # Nor is a host that stayed out of reach: the design was never synthesised.
        out['measure_error'] = ('the synthesis host could not be reached for %s min '
                                '(%s); the design was never synthesised and nothing '
                                'was learned about it'
                                % (CONFIG.get('hacc_wait_min'), unreachable[:200]))
    if synth_metrics is not None or synth_error or unreachable:
        out['synth_seconds'] = box.get('seconds')
    if synth_error:
        out['synth_error'] = synth_error
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--rtl', default=os.path.join(ROOT, 'rtl'))
    ap.add_argument('--work', default=os.path.join(ROOT, '.agentic', 'measure'))
    ap.add_argument('--no-synth', action='store_true')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    draws = prepare_corpus()
    result = measure(os.path.abspath(args.rtl), os.path.abspath(args.work),
                     draws, synth=not args.no_synth)
    if args.json:
        write_json(args.json, result)
    print(json.dumps({k: v for k, v in result.items() if k != 'draws'},
                     indent=1, sort_keys=True))
    for rec in result.get('draws', []):
        print('  %-20s %-5s %s' % (rec['name'], 'ok' if rec['oracle_pass'] else 'FAIL',
                                    rec['bytes_per_cycle'] if rec['oracle_pass']
                                    else rec['problem']))
    return 0 if result.get('oracle_pass') else 1


if __name__ == '__main__':
    sys.exit(main())
