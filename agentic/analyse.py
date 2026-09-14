#!/usr/bin/env python3
"""
Where does the design lose throughput?

Takes the counters from one throughput simulation and the stimulus it ran on,
computes a rate at each pipeline stage, and divides each by a ceiling that is
READ OUT OF THE RTL rather than assumed. The stage nearest its ceiling is the
binding one. A stage reading past its ceiling means the ceiling model is
stale, and the report says so instead of calling it a bottleneck.

Stages (for this Snappy decompressor):

    input port      compressed bytes per cycle   vs  co_data width
    decode engine   Snappy elements per cycle    vs  elements per transfer x cores
    core datapath   output bytes per cycle       vs  core line width x usable cores
    output port     output bytes per cycle       vs  de_data width

Also reads worst slack when synthesis numbers are given, and answers which
factor of throughput (bytes/cycle or f_max) is the one worth moving.
"""

import os
import re

import oracle

SATURATED = 0.90
IMPOSSIBLE = 1.25

# One element_stream transfer per cycle carries one copy slot and one literal
# slot, so a cycle can retire two Snappy elements. Read from the RTL when the
# record is recognisable, else this default.
DEFAULT_ELEMENTS_PER_TRANSFER = 2.0


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fil:
            return fil.read()
    except IOError:
        return ''


def rtl_widths(rtl_dir):
    """Port widths, count-field widths, core count and core line width."""
    top = _read(os.path.join(rtl_dir, 'vhsnunzip_unbuffered.vhd'))
    pkg = _read(os.path.join(rtl_dir, 'vhsnunzip_int_pkg.vhd'))
    out = {'in_bytes': 8, 'in_cnt_bits': 3, 'out_bytes': 8, 'out_cnt_bits': 4,
           'cores': 1, 'core_line_bytes': 8.0,
           'elements_per_transfer': DEFAULT_ELEMENTS_PER_TRANSFER}

    def width(port):
        found = re.search(
            port + r'\s*:\s*(?:in|out)\s+std_logic_vector\s*\(\s*(\d+)\s+downto\s+0\s*\)',
            top)
        return int(found.group(1)) + 1 if found else None

    for port, key, kbits in (('co_data', 'in_bytes', 'in_cnt_bits'),
                             ('de_data', 'out_bytes', 'out_cnt_bits')):
        bits = width(port)
        if bits:
            out[key] = bits // 8
    for port, key in (('co_cnt', 'in_cnt_bits'), ('de_cnt', 'out_cnt_bits')):
        bits = width(port)
        if bits:
            out[key] = bits

    found = re.search(r'NUM_CORES\s*:\s*(?:positive|natural|integer)\s*:=\s*(\d+)', top)
    if found:
        out['cores'] = int(found.group(1))

    found = re.search(
        r'type\s+decompressed_stream\s+is\s+record.*?'
        r'data\s*:\s*byte_array\s*\(\s*0\s+to\s+(\d+)\s*\)', pkg, re.S | re.I)
    if found:
        out['core_line_bytes'] = float(int(found.group(1)) + 1)

    # The element stream: count cp_val/li_val style valid flags in the record.
    found = re.search(r'type\s+element_stream\s+is\s+record(.*?)end\s+record',
                      pkg, re.S | re.I)
    if found:
        body = found.group(1)
        slots = len(re.findall(r'\b(?:cp|li)\d*_val\s*:', body))
        if slots >= 1:
            out['elements_per_transfer'] = float(slots)
    return out


def count_elements(compressed):
    """Snappy elements in a chunk, and the bytes they expand to."""
    i, total, shift = 0, 0, 0
    while True:
        byte = compressed[i]
        i += 1
        total |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            break
    count = 0
    while i < len(compressed):
        tag = compressed[i]
        kind = tag & 3
        if kind == 0:
            length = tag >> 2
            if length < 60:
                i += 1
            else:
                extra = length - 59
                length = int.from_bytes(compressed[i + 1:i + 1 + extra], 'little')
                i += 1 + extra
            i += length + 1
        elif kind == 1:
            i += 2
        elif kind == 2:
            i += 3
        else:
            i += 5
        count += 1
    return count, total


def stimulus_shape(cs_tv):
    chunks = oracle.read_stimulus(cs_tv)
    compressed = sum(len(c) for c in chunks)
    elements = expanded = 0
    for chunk in chunks:
        count, total = count_elements(chunk)
        elements += count
        expanded += total
    return {
        'chunks': len(chunks),
        'compressed_bytes': compressed,
        'expanded_bytes': expanded,
        'elements': elements,
        'compression_pct': round(100.0 * compressed / max(1, expanded), 1),
        'bytes_per_element': round(expanded / max(1, elements), 2),
    }


def analyse(counters, cs_tv, widths):
    """Rates at every stage and which one binds, for one draw."""
    shape = stimulus_shape(cs_tv)
    cycles = max(1, counters['cycles'])
    out_rate = counters['bytes_out'] / cycles
    in_rate = shape['compressed_bytes'] / cycles
    elem_rate = shape['elements'] / cycles
    usable_cores = min(widths['cores'], max(1, shape['chunks']))

    stages = [
        {'name': 'input port', 'rate': in_rate,
         'ceiling': float(widths['in_bytes']), 'unit': 'B/cycle'},
        {'name': 'decode engine', 'rate': elem_rate,
         'ceiling': widths['elements_per_transfer'] * usable_cores,
         'unit': 'elem/cycle'},
        {'name': 'core datapath', 'rate': out_rate,
         'ceiling': widths['core_line_bytes'] * usable_cores, 'unit': 'B/cycle'},
        {'name': 'output port', 'rate': out_rate,
         'ceiling': float(widths['out_bytes']), 'unit': 'B/cycle'},
    ]
    for st in stages:
        st['utilisation'] = round(st['rate'] / st['ceiling'], 3)
        st['rate'] = round(st['rate'], 3)

    binding = max(stages, key=lambda s: s['utilisation'])
    stale = [s['name'] for s in stages if s['utilisation'] > IMPOSSIBLE]
    idle_pct = 100.0 * counters.get('de_bubble', 0) / cycles
    stall_pct = 100.0 * counters.get('co_stall', 0) / cycles

    if stale:
        verdict = 'unknown (stale ceiling)'
    elif binding['utilisation'] >= SATURATED:
        verdict = binding['name'] + ' saturated'
    elif idle_pct > 20.0:
        verdict = 'internal latency (output idle %.0f%% of cycles)' % idle_pct
    else:
        verdict = 'no stage saturated'

    return {
        'shape': shape,
        'stages': stages,
        'binding': binding['name'],
        'binding_utilisation': binding['utilisation'],
        'stale_ceilings': stale,
        'verdict': verdict,
        'usable_cores': usable_cores,
        'bytes_per_cycle': round(out_rate, 4),
        'output_idle_pct': round(idle_pct, 1),
        'input_stall_pct': round(stall_pct, 1),
    }


def combine(reports):
    """One verdict over several draws (keeps the disagreement visible)."""
    reports = [r for r in reports if r]
    if not reports:
        return None
    votes = {}
    for rep in reports:
        votes.setdefault(rep['binding'], []).append(rep)
    ranked = sorted(votes.items(), key=lambda kv: -len(kv[1]))
    worst = max(reports, key=lambda r: r['binding_utilisation'])
    stale = sorted({n for r in reports for n in r['stale_ceilings']})
    return {
        'binding': ranked[0][0],
        'split': {name: len(rs) for name, rs in ranked},
        'tightest': worst['binding'],
        'tightest_utilisation': worst['binding_utilisation'],
        'stale_ceilings': stale,
        'mean_output_idle_pct': round(sum(r['output_idle_pct'] for r in reports)
                                      / len(reports), 1),
    }


def lever(combined, wns_ns=None):
    """Which factor of throughput = bytes/cycle x f_max is worth moving."""
    if not combined:
        return {'lever': 'unknown', 'reason': 'no profile'}
    if combined['stale_ceilings']:
        return {'lever': 'unknown',
                'reason': ('%s reads past its ceiling; the stage model is '
                           'stale, so trust the raw counters, not the '
                           'utilisations.' % ', '.join(combined['stale_ceilings']))}
    tight = combined['tightest_utilisation']
    if tight >= SATURATED:
        return {'lever': 'width',
                'reason': ('%s is at %.0f%% of its ceiling. A faster clock cannot '
                           'move a saturated stage; more work per cycle can.'
                           % (combined['tightest'], 100.0 * tight))}
    if wns_ns is not None and wns_ns < 0:
        return {'lever': 'clock',
                'reason': ('no stage is saturated and worst slack is %.3f ns: '
                           'the clock is what limits throughput.' % wns_ns)}
    if combined['mean_output_idle_pct'] > 20.0:
        return {'lever': 'latency',
                'reason': ('no stage is saturated and the output is idle %.0f%% '
                           'of cycles: something inside is not keeping the '
                           'pipeline fed (bubbles, stalls between stages, '
                           'per-chunk overhead).' % combined['mean_output_idle_pct'])}
    return {'lever': 'width',
            'reason': 'no stage saturated and the clock is met; raise the '
                      'per-cycle work of the tightest stage (%s at %.0f%%).'
                      % (combined['tightest'], 100.0 * tight)}


def describe(report, name=None):
    """A short text block for a prompt or a log."""
    lines = []
    if name:
        lines.append('draw %s: %d chunk(s), %.1f bytes/element, compresses to %.0f%%'
                     % (name, report['shape']['chunks'],
                        report['shape']['bytes_per_element'],
                        report['shape']['compression_pct']))
    for st in report['stages']:
        lines.append('  %-14s %8.3f of %8.3f %-10s  %4.0f%%'
                     % (st['name'], st['rate'], st['ceiling'], st['unit'],
                        100.0 * st['utilisation']))
    lines.append('  output idle %.1f%% of cycles, input stalled %.1f%%; verdict: %s'
                 % (report['output_idle_pct'], report['input_stall_pct'],
                    report['verdict']))
    return '\n'.join(lines)
