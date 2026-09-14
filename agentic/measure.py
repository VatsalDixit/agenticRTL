#!/usr/bin/env python3
"""
Measure one design: correct or not, bytes per cycle, f_max, area, throughput.

    throughput (GB/s) = bytes_per_cycle x f_max (MHz) / 1000

  * bytes/cycle comes from the throughput testbench under GHDL, on every draw
    in the corpus, all at once. Correctness is decided on every draw by
    agentic/oracle.py against the frozen reference decompressor.
  * f_max and area come from GHDL -> Yosys -> ABC on the Nangate 45nm library.

The loop always measures with ITS OWN copy of the testbench, the stub and the
scripts (this folder), never with copies inside a candidate's worktree. Only
the candidate's rtl/ folder is taken from the worktree.

Usage:
    python agentic/measure.py [--rtl DIR] [--no-synth] [--json FILE]
"""

import argparse
import json
import os
import re
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

import analyse                                   # noqa: E402
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

_DELAY = re.compile(r'Delay\s*=\s*([\d.]+)\s*ps')
_AREA = re.compile(r'Chip area for module .*?:\s*([\d.]+)')
_SEQ_AREA = re.compile(r'used for sequential elements:\s*([\d.]+)')
_CELL = re.compile(r'^\s*(\d+)\s+[\d.E+-]+\s+(\S+)\s*$', re.M)


class MeasureError(RuntimeError):
    """The tools could not produce numbers that can be believed."""


def corpus_dir(root=None):
    return os.path.join(root or ROOT, '.agentic', 'corpus')


def prepare_corpus(root=None, pages=None, seed=0):
    """Build every draw's cs.tv. Returns [(draw, dir, chunks), ...]."""
    return stim.build_all(corpus_dir(root), pages=pages or CONFIG['train_pages'],
                          seed=seed)


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


def simulate(rtl_dir, build_dir, draws, widths, timeout=None):
    """Run every draw against one rtl/ folder. Returns per-draw results.

    ``draws`` is [(draw, dir, chunks), ...]. The result for each draw is a
    dict with oracle_pass, bytes_per_cycle, counters, and a problem string
    when something went wrong. Raises MeasureError when the design does not
    even compile.
    """
    generics = ('-gCO_BYTES=%d -gCO_CNT_BITS=%d -gDE_BYTES=%d -gDE_CNT_BITS=%d'
                % (widths['in_bytes'], widths['in_cnt_bits'],
                   widths['out_bytes'], widths['out_cnt_bits']))
    if widths['in_bytes'] % 8 != 0:
        raise MeasureError('co_data is %d bytes wide; the harness needs a '
                           'multiple of 8' % widths['in_bytes'])
    specs = ' '.join('"%s:%d"' % (shell_path(d), c) for _draw, d, c in draws)
    script = ('bash "%s" "%s" "%s" "%s" "%s" 50ms %s'
              % (shell_path(SIM_SCRIPT), shell_path(rtl_dir),
                 shell_path(TB_FILE), shell_path(build_dir), generics, specs))
    res = eda_shell(script, timeout=timeout or CONFIG['sim_timeout_s'])
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

    results = []
    for draw, ddir, chunks in draws:
        rc, deadlock = status.get(shell_path(ddir), (99, False))
        rec = {'name': draw.name, 'kind': draw.kind, 'scored': draw.scored,
               'visible': draw.visible, 'chunks': chunks,
               'oracle_pass': False, 'bytes_per_cycle': None, 'problem': None}
        perf = os.path.join(ddir, 'perf.txt')
        out_hex = os.path.join(ddir, 'out.hex')
        cs_tv = os.path.join(ddir, 'cs.tv')
        if deadlock:
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


def synthesize(rtl_dir, out_dir, timeout=None):
    """Area and timing for one rtl/ folder. Raises MeasureError."""
    if not os.path.exists(LIB_FILE):
        raise MeasureError('no liberty file at %s (run agentic/syn/get_lib.sh)'
                           % LIB_FILE)
    script = ('bash "%s" "%s" "%s" "%s" "%s" %s %d'
              % (shell_path(SYNTH_SCRIPT), shell_path(rtl_dir),
                 shell_path(STUB_FILE), shell_path(LIB_FILE),
                 shell_path(out_dir), TOP, int(CONFIG['clock_period_ps'])))
    res = eda_shell(script, timeout=timeout or CONFIG['synth_timeout_s'])
    if res.timed_out:
        raise MeasureError('synthesis did not finish within %d s'
                           % (timeout or CONFIG['synth_timeout_s']))
    text = res.text
    if 'SYNTH_OK' not in text:
        tail = [l for l in text.splitlines() if l.strip()]
        raise MeasureError('synthesis failed:\n' + '\n'.join(tail[-25:]))
    metrics = parse_synth(text)
    stat = os.path.join(out_dir, 'stat.log')
    if os.path.exists(stat):
        with open(stat, encoding='utf-8', errors='replace') as fil:
            block = fil.read()
        cells = {name: int(n) for n, name in _CELL.findall(block)}
        regs = sum(n for name, n in cells.items()
                   if name.startswith(('DFF', 'SDFF')))
        if regs:
            metrics['regs'] = regs
        if cells:
            metrics['cells'] = sum(cells.values())
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


def measure(rtl_dir, work_dir, draws, synth=True, log=None):
    """The whole measurement for one rtl/ folder. Never raises for a bad
    design; returns a dict with 'error' set when the tools could not run."""
    os.makedirs(work_dir, exist_ok=True)
    widths = analyse.rtl_widths(rtl_dir)
    try:
        sims = simulate(rtl_dir, os.path.join(work_dir, 'sim'), draws, widths)
    except MeasureError as exc:
        return {'oracle_pass': False, 'error': str(exc), 'widths': widths,
                'draws': []}
    synth_metrics = None
    synth_error = None
    if synth and all(r['oracle_pass'] for r in sims):
        try:
            synth_metrics = synthesize(rtl_dir, os.path.join(work_dir, 'synth'))
        except MeasureError as exc:
            synth_error = str(exc)
    out = summarize(sims, synth_metrics)
    out['widths'] = widths
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
