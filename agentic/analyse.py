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
    copy slot       copy elements per cycle      vs  copy slots x cores
    literal slot    literal elements per cycle   vs  literal slots x cores
    core datapath   output bytes per cycle       vs  core line width x usable cores
    output port     output bytes per cycle       vs  de_data width

Usable cores is the core count, capped by the draw's chunk count and by its
bytes over its largest chunk (usable_cores below).

A transfer carries one copy and one literal, so the two element kinds have
SEPARATE ceilings. Pooling them hides a saturated copy slot behind an idle
literal slot: on the 32-byte design the copy slot ran at 82-93% of its ceiling
on five of eight scored draws while the pooled number read 63-74% and the loop
concluded "no stage saturated".

Also reads worst slack when synthesis numbers are given, and answers which
factor of throughput (bytes/cycle or f_max) is the one worth moving.

When the simulation ran with the internal probe (agentic/probe.py), the
handshakes between stages are read too, and they decide what output idle
means. The port counters alone cannot: on i35 the output was idle 40% of
cycles and the verdict said "internal latency", while the probe showed the
decoder moving on 99% of cycles and nothing after it ever waiting. Idle
there was the decoder's rate, not bubbles. Every probe path here is guarded:
a probe bug falls back to the port-counter verdict and never fails a
measurement.
"""

import os
import re
import sys

import oracle
import probe as probe_mod

SATURATED = 0.90
IMPOSSIBLE = 1.25
# What the decompression history holds; a longer chunk wraps it.
HISTORY_BYTES = 65536

# One element_stream transfer per cycle carries one copy slot and one literal
# slot. Read from the RTL when the record is recognisable, else these defaults.
DEFAULT_COPY_SLOTS = 1.0
DEFAULT_LITERAL_SLOTS = 1.0
DEFAULT_ELEMENTS_PER_TRANSFER = DEFAULT_COPY_SLOTS + DEFAULT_LITERAL_SLOTS


def _read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fil:
            return fil.read()
    except IOError:
        return ''


def _constants(texts):
    """Integer constants/generics declared in the given VHDL texts."""
    out = {}
    for text in texts:
        for m in re.finditer(r'\b(?:constant|generic)?\s*([A-Za-z_]\w*)\s*:\s*'
                             r'(?:natural|positive|integer)\s*:=\s*([^;]+);', text):
            expr = m.group(2).strip()
            try:
                out[m.group(1)] = int(_eval_int(expr, out))
            except Exception:
                pass
    return out


def _eval_int(expr, consts):
    """Evaluate a small integer expression like LINE_BYTES*8-1."""
    expr = expr.strip()
    if not re.match(r'^[\w\s*+\-/()]+$', expr):
        raise ValueError(expr)
    tokens = re.sub(r'\b([A-Za-z_]\w*)\b',
                    lambda m: str(consts[m.group(1)]) if m.group(1) in consts
                    else m.group(1), expr)
    if re.search(r'[A-Za-z_]', tokens):
        raise ValueError(expr)
    return eval(tokens, {'__builtins__': {}}, {})       # digits and operators only


def rtl_widths(rtl_dir):
    """Port widths, count-field widths, core count and core line width.

    Read from the RTL, never assumed. A width the reader cannot resolve is
    listed under 'unresolved' and the measurement refuses to guess.
    """
    top = _read(os.path.join(rtl_dir, 'vhsnunzip_unbuffered.vhd'))
    pkg = _read(os.path.join(rtl_dir, 'vhsnunzip_int_pkg.vhd'))
    others = [_read(os.path.join(rtl_dir, f)) for f in sorted(os.listdir(rtl_dir))
              if f.endswith('_pkg.vhd')] if os.path.isdir(rtl_dir) else []
    consts = _constants([top] + others)
    out = {'in_bytes': 8, 'in_cnt_bits': 3, 'out_bytes': 8, 'out_cnt_bits': 4,
           'cores': 1, 'core_line_bytes': 8.0,
           'copy_slots': DEFAULT_COPY_SLOTS,
           'literal_slots': DEFAULT_LITERAL_SLOTS,
           'elements_per_transfer': DEFAULT_ELEMENTS_PER_TRANSFER,
           'unresolved': []}
    # Which of those are still the defaults above because the RTL did not
    # say. A design that declares its widths elsewhere (DSW-4 keeps them in
    # its own package) would otherwise read as K2/L8 with nothing to tell a
    # caller that it was guessed; whoever needs a width it can trust (the
    # packing model's calibration) checks out['defaulted'].
    read = set()

    def width(port):
        found = re.search(
            port + r'\s*:\s*(?:in|out)\s+std_logic_vector\s*\(\s*(.+?)\s+downto\s+0\s*\)',
            top)
        if not found:
            return None
        try:
            return int(_eval_int(found.group(1), consts)) + 1
        except Exception:
            out['unresolved'].append(port)
            return None

    for port, key in (('co_data', 'in_bytes'), ('de_data', 'out_bytes')):
        bits = width(port)
        if bits:
            out[key] = bits // 8
            read.add(key)
    for port, key in (('co_cnt', 'in_cnt_bits'), ('de_cnt', 'out_cnt_bits')):
        bits = width(port)
        if bits:
            out[key] = bits
            read.add(key)

    found = re.search(r'NUM_CORES\s*:\s*(?:positive|natural|integer)\s*:=\s*(\d+)', top)
    if found:
        out['cores'] = int(found.group(1))
        read.add('cores')

    found = re.search(
        r'type\s+decompressed_stream\s+is\s+record.*?'
        r'data\s*:\s*byte_array\s*\(\s*0\s+to\s+(\d+)\s*\)', pkg, re.S | re.I)
    if found:
        out['core_line_bytes'] = float(int(found.group(1)) + 1)
        read.add('core_line_bytes')

    # The element stream: count cp_val/li_val style valid flags in the record,
    # by kind, because a copy and a literal do not compete for the same slot.
    found = re.search(r'type\s+element_stream\s+is\s+record(.*?)end\s+record',
                      pkg, re.S | re.I)
    if found:
        body = found.group(1)
        copies = len(re.findall(r'\bcp\d*_val\s*:', body))
        literals = len(re.findall(r'\bli\d*_val\s*:', body))
        if copies:
            out['copy_slots'] = float(copies)
            read.add('copy_slots')
        if literals:
            out['literal_slots'] = float(literals)
            read.add('literal_slots')
        if copies + literals >= 1:
            out['elements_per_transfer'] = float(copies + literals)
            read.add('elements_per_transfer')
    out['defaulted'] = [k for k in ('in_bytes', 'in_cnt_bits', 'out_bytes', 'out_cnt_bits',
                                    'cores', 'core_line_bytes', 'copy_slots',
                                    'literal_slots', 'elements_per_transfer')
                        if k not in read]
    return out


def count_elements(compressed):
    """Literal count, copy count and expanded bytes for one chunk."""
    i, total, shift = 0, 0, 0
    while True:
        byte = compressed[i]
        i += 1
        total |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            break
    literals = copies = 0
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
            literals += 1
        else:
            i += {1: 2, 2: 3, 3: 5}[kind]
            copies += 1
    return literals, copies, total


def stimulus_shape(cs_tv):
    chunks = oracle.read_stimulus(cs_tv)
    compressed = sum(len(c) for c in chunks)
    literals = copies = expanded = largest = long_chunks = 0
    for chunk in chunks:
        lits, cps, total = count_elements(chunk)
        literals += lits
        copies += cps
        expanded += total
        largest = max(largest, total)
        long_chunks += total > HISTORY_BYTES
    elements = literals + copies
    return {
        'chunks': len(chunks),
        'compressed_bytes': compressed,
        'expanded_bytes': expanded,
        'largest_chunk_bytes': largest,
        'long_chunks': long_chunks,
        'elements': elements,
        'literals': literals,
        'copies': copies,
        'compression_pct': round(100.0 * compressed / max(1, expanded), 1),
        'bytes_per_element': round(expanded / max(1, elements), 2),
    }


def usable_cores(cores, shape):
    """How many cores one draw can keep busy at once, at most.

    A chunk runs on one core from its first byte to its last, so the other
    cores can only take the bytes outside the largest chunk: the bound is
    total bytes / largest chunk, as well as the chunk count. On whole Parquet
    row groups the byte bound is the tight one. A 15.7 MB page in a 20 MB row
    group leaves a second core 1.3x at most, however many pages there are.
    """
    by_bytes = shape['expanded_bytes'] / float(max(1, shape.get('largest_chunk_bytes')
                                                   or shape['expanded_bytes']))
    return round(max(1.0, min(float(cores), float(max(1, shape['chunks'])), by_bytes)), 2)


def analyse(counters, cs_tv, widths, probe=None):
    """Rates at every stage and which one binds, for one draw.

    ``probe`` is probe.summarise()'s result for this draw when the simulation
    was probed; its verdict lands in report['probe'] (None without one).
    """
    shape = stimulus_shape(cs_tv)
    cycles = max(1, counters['cycles'])
    out_rate = counters['bytes_out'] / cycles
    in_rate = shape['compressed_bytes'] / cycles
    copy_rate = shape['copies'] / cycles
    literal_rate = shape['literals'] / cycles
    usable = usable_cores(widths['cores'], shape)

    stages = [
        {'name': 'input port', 'rate': in_rate,
         'ceiling': float(widths['in_bytes']), 'unit': 'B/cycle'},
        {'name': 'copy slot', 'rate': copy_rate,
         'ceiling': widths.get('copy_slots', DEFAULT_COPY_SLOTS) * usable,
         'unit': 'copies/cycle'},
        {'name': 'literal slot', 'rate': literal_rate,
         'ceiling': widths.get('literal_slots', DEFAULT_LITERAL_SLOTS) * usable,
         'unit': 'literals/cycle'},
        {'name': 'core datapath', 'rate': out_rate,
         'ceiling': widths['core_line_bytes'] * usable, 'unit': 'B/cycle'},
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
        'cores': widths['cores'],
        'usable_cores': usable,
        'bytes_per_cycle': round(out_rate, 4),
        'output_idle_pct': round(idle_pct, 1),
        'input_stall_pct': round(stall_pct, 1),
        'probe': _guarded(probe_verdict, probe),
    }


def _note(text):
    """Where a swallowed probe error goes: stderr, which the loop's log keeps."""
    try:
        sys.stderr.write('analyse: %s\n' % text)
    except Exception:
        pass


def _guarded(func, *args):
    """func(*args), or None when it raises: the probe is advice, never a crash."""
    try:
        return func(*args)
    except Exception as exc:
        _note('probe part skipped: %s: %s' % (type(exc).__name__, str(exc)[:200]))
        return None


def _pct(x):
    return 100.0 * (x or 0.0)


def stage_name(stage, stages):
    """A stage key with its entity: 'datapath_inst/main_dec_inst (vhsnunzip_decoder_long)'."""
    if not stage:
        return '(unknown)'
    ent = (stages or {}).get(stage)
    return '%s (%s)' % (stage, ent) if ent else stage


def probe_verdict(summary):
    """Which stage the probe names, from one draw's summary or a combined one.

    The rules, in data-flow order; the first match wins:
      rate          the first stage whose output moves on >= 85% of cycles
                    and waits on <= 5%, while its input waits on >= 10%. It
                    runs at its own rate: nothing after it holds it back.
      backpressure  else the handshake that waits most, if >= 25%: its
                    consumer holds the pipe back (a tie goes to the most
                    downstream inner stage, probe.stalled_by).
      starved       else the first stage whose input is empty on >= 50% of
                    cycles while nothing after it waits.
    -> {'limit', 'kind', 'text', 'handshakes', 'cycles', 'stages'} or None.
    """
    if not summary or not summary.get('handshakes'):
        return None
    hs = summary['handshakes']
    stages = summary.get('stages') or {}
    io, order = probe_mod.stage_io(hs)
    out = None
    for stage in order:
        if probe_mod.meets_limit(io[stage]):
            best = max(io[stage]['out'], key=lambda h: h['moved'])
            waits = max(io[stage]['in'], key=lambda h: h['blocked'])
            out = {'limit': stage, 'kind': 'rate',
                   'text': ('%s is the limit: its output %s moves on %.0f%% of cycles and '
                            'waits on %.1f%%, while its input %s waits on %.0f%%. Nothing '
                            'after it holds it back; it runs at its own rate, so only more '
                            'work per cycle in that stage raises bytes/cycle.'
                            % (stage_name(stage, stages), best['key'], _pct(best['moved']),
                               _pct(best['blocked']), waits['key'],
                               _pct(waits['blocked'])))}
            break
    pairs = [h for h in hs if h['kind'] == 'pair']
    if out is None and pairs:
        worst = probe_mod.stalled_by(hs)
        if worst['blocked'] >= 0.25:
            out = {'limit': worst['consumer'], 'kind': 'backpressure',
                   'text': ('%s holds the pipe back: %s waits on %.0f%% of cycles for it '
                            '(moves on %.0f%%).'
                            % (stage_name(worst['consumer'], stages), worst['key'],
                               _pct(worst['blocked']), _pct(worst['moved'])))}
    if out is None:
        for i, h in enumerate(pairs):
            if h['empty'] >= 0.50 and all(x['blocked'] < probe_mod.LIMIT_OUT_BLOCKED
                                          for x in pairs[i + 1:]):
                out = {'limit': h['consumer'], 'kind': 'starved',
                       'text': ('%s is starved: its input %s is empty on %.0f%% of cycles '
                                'and nothing after it waits; what feeds it (%s) is slower.'
                                % (stage_name(h['consumer'], stages), h['key'],
                                   _pct(h['empty']), stage_name(h['producer'], stages)))}
                break
    if out is None:
        out = {'limit': None, 'kind': 'none',
               'text': ('no stage reads as running at its own rate, holding the pipe '
                        'back, or starved.')}
    out.update(handshakes=hs, cycles=summary.get('cycles'), stages=stages)
    if summary.get('window_limit_share'):
        out['window_limit_share'] = summary['window_limit_share']
    return out


def _combine_probe(reports):
    probes = [r.get('probe') for r in reports if r.get('probe')]
    if not probes:
        return None
    return probe_verdict(probe_mod.combine(probes))


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
    out = {
        'binding': ranked[0][0],
        'split': {name: len(rs) for name, rs in ranked},
        'tightest': worst['binding'],
        'tightest_utilisation': worst['binding_utilisation'],
        'stale_ceilings': stale,
        'mean_output_idle_pct': round(sum(r['output_idle_pct'] for r in reports)
                                      / len(reports), 1),
    }
    # Cycle-weighted over the draws, the way the probe counted them.
    verdict = _guarded(_combine_probe, reports)
    if verdict:
        out['probe'] = verdict
    return out


def _probe_lever(combined):
    """The lever when the output idles and the probe can say why, else None."""
    verdict = combined.get('probe')
    if not verdict:
        return None
    if verdict['kind'] == 'rate':
        name = stage_name(verdict['limit'], verdict.get('stages'))
        return {'lever': 'width',
                'reason': ("%s moves every cycle and nothing after it blocks; output idle "
                           "is that stage's rate, not bubbles. Raise the work it does per "
                           "cycle (more elements per transfer, a wider line or port). %s"
                           % (name, verdict['text'].split(': ', 1)[-1]))}
    for h in verdict.get('handshakes') or []:
        if h['kind'] == 'pair' and h['blocked'] >= 0.10 and h['empty'] >= 0.30:
            return {'lever': 'latency',
                    'reason': ('%s both waits (%.0f%% of cycles) and runs empty (%.0f%%): '
                               'the stages around it stall each other, which is latency '
                               '(bubbles, stalls between stages, per-chunk overhead). %s'
                               % (h['key'], _pct(h['blocked']), _pct(h['empty']),
                                  verdict['text']))}
    return {'lever': 'width',
            'reason': ('the output is idle %.0f%% of cycles, but no handshake inside both '
                       'waits and runs empty, so that idle is not bubbles. %s'
                       % (combined['mean_output_idle_pct'], verdict['text']))}


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
    if tight >= SATURATED and combined['tightest'] == 'copy slot':
        return {'lever': 'width',
                'reason': ('copy slot is at %.0f%% of its ceiling: the design issues a '
                           'fixed number of copy elements per cycle, and that caps '
                           'bytes/cycle at bytes / copies on each draw (see the cap '
                           'lines). Only MORE COPY ELEMENTS PER CYCLE (dual issue) or '
                           'a faster clock move it; a wider line, a wider port or a '
                           'larger copy cap cannot, since copies are shorter than '
                           'the line.' % (100.0 * tight))}
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
        # The probe, when there is one, says whether that idle is bubbles.
        found = _guarded(_probe_lever, combined)
        if found:
            return found
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
        shape = report['shape']
        lines.append('draw %s: %d chunk(s), %.1f bytes/element, compresses to %.0f%%'
                     ', %d copies and %d literals'
                     % (name, shape['chunks'], shape['bytes_per_element'],
                        shape['compression_pct'], shape.get('copies', 0),
                        shape.get('literals', 0)))
        if shape.get('largest_chunk_bytes'):
            lines.append('  largest chunk %.0f KiB, %.0f%% of the bytes, so more cores '
                         'can speed this draw up %.2fx at most; %d chunk(s) longer '
                         'than the 64 KiB history'
                         % (shape['largest_chunk_bytes'] / 1024.0,
                            100.0 * shape['largest_chunk_bytes']
                            / max(1, shape['expanded_bytes']),
                            usable_cores(shape['chunks'], shape),
                            shape.get('long_chunks', 0)))
    shape = report.get('shape') or {}
    copy = next((st for st in report['stages'] if st['name'] == 'copy slot'), None)
    if copy and shape.get('copies'):
        cap = shape['expanded_bytes'] / float(shape['copies']) * copy['ceiling']
        lines.append('  copy-issue cap: %g copy element(s) per cycle caps this draw at '
                     '%.2f B/cycle (bytes / copies); measured %.2f, %.0f%% of it'
                     % (copy['ceiling'], cap, report['bytes_per_cycle'],
                        100.0 * report['bytes_per_cycle'] / cap))
    if report.get('cores', 1) > (report.get('usable_cores') or 1):
        lines.append('  usable cores %.2f of %d: a chunk runs on one core, so the '
                     'others only get the bytes outside the largest one'
                     % (report['usable_cores'], report['cores']))
    for st in report['stages']:
        lines.append('  %-14s %8.3f of %8.3f %-15s %4.0f%%'
                     % (st['name'], st['rate'], st['ceiling'], st['unit'],
                        100.0 * st['utilisation']))
    lines.append('  output idle %.1f%% of cycles, input stalled %.1f%%; verdict: %s'
                 % (report['output_idle_pct'], report['input_stall_pct'],
                    report['verdict']))
    extra = _guarded(describe_probe, report.get('probe'))
    if extra:
        lines.append(extra)
    return '\n'.join(lines)


# Handshake lines shown at most; DSW-4 has about forty, most of them registers
# without a ready, and every pair is always shown.
PROBE_LINES = 24


def describe_probe(verdict, max_lines=PROBE_LINES):
    """The probe's lines for a prompt or a log: one per handshake, then the verdict."""
    if not verdict or not verdict.get('handshakes'):
        return ''
    stages = verdict.get('stages') or {}
    hs = verdict['handshakes']
    pairs = [h for h in hs if h['kind'] == 'pair']
    occ = [h for h in hs if h['kind'] != 'pair']
    shown = set(id(h) for h in pairs + occ[:max(0, max_lines - len(pairs))])
    lines = ['  probe inside the design (% of cycles: moved / waiting / empty):']
    for h in hs:
        if id(h) not in shown:
            continue
        flow = '%s -> %s' % (h['producer'] or '?', h['consumer'] or '?')
        if h['kind'] == 'pair':
            lines.append('    %-30s %5.1f / %5.1f / %5.1f   %s'
                         % (h['key'], _pct(h['moved']), _pct(h['blocked']),
                            _pct(h['empty']), flow))
        else:
            lines.append('    %-30s valid %5.1f, empty %5.1f (no ready)   %s'
                         % (h['key'], _pct(h['moved']), _pct(h['empty']), flow))
    if len(shown) < len(hs):
        lines.append('    (%d more without a ready not shown)' % (len(hs) - len(shown)))
    lines.append('  probe verdict: %s' % verdict['text'])
    share = verdict.get('window_limit_share')
    if share:
        lines.append('  share of 4096-cycle windows in which a stage is the limit: %s'
                     % ', '.join('%s %.0f%%' % (stage_name(s, stages), 100.0 * v)
                                 for s, v in sorted(share.items(), key=lambda kv: -kv[1])))
    return '\n'.join(lines)
