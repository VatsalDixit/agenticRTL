#!/usr/bin/env python3
"""
A probe inside the design: how often each stage handshake moves, waits or
idles, counted in simulation.

The testbench only sees the ports, and the ports could not tell "the output
is idle because something inside stalls" from "the output is idle because
one stage makes all it can make every cycle". Read from the ports alone, the
second looks like the first.

This module makes that probe for any design, without knowing its names:

  find_handshakes(rtl_dir)    reads the RTL and finds, in every architecture,
                              the stream handshakes it can see: a record X
                              with a `valid` field and an X_ready (or X_pop),
                              or an X_valid with an X_ready (or X_pop). A
                              valid with no ready partner is still counted,
                              as occupancy (valid or not).
  instrument(rtl_dir, out)    copies rtl_dir byte for byte into `out` and
                              inserts one read-only process per architecture
                              that counts those handshakes per cycle. The
                              process sits inside translate_off/on, drives
                              nothing, and every name it declares starts with
                              agentic_probe_, so it cannot change what the
                              design does. The candidate's rtl/ is never
                              touched.
  read(draw_dir)              the counts the simulation left in a draw folder.
  summarise(probe)            fractions per handshake, in data-flow order,
                              with the producing and consuming stage of each.
  combine(probes)             cycle-weighted fractions over several draws.

What the inserted process writes, in the simulator's working folder (the
draw folder):

  agentic_probe.<path>.tot   totals, rewritten at cycle FLUSH_MIN and then
                             whenever the cycles since the last write reach
                             clamp(FLUSH_MIN, cycles/16, 65536): at most 1/16
                             of a run (or FLUSH_MIN cycles) is ever missing
                             from it, which is fine because fractions are
                             what is read; a 3.5M-cycle draw costs about 200
                             file writes. FLUSH_MIN is 32, not the 256 first
                             planned: a short self-test shape on a wide
                             design would never be written at all.
  agentic_probe.<path>.win   per 4096-cycle window counts, only when the file
                             agentic_probe.windows exists in the folder at
                             time 0. measure.py creates that marker in visible
                             draw folders only, so no window data is ever
                             written for a held-out draw.

Plain Python 3, standard library only: it is synced into session worktrees
and frozen with the rest of the instrument (agentic/freeze.py).
"""

import hashlib
import os
import re
import shutil

TOP = 'vhsnunzip_unbuffered'
WINDOW = 4096
PREFIX = 'agentic_probe'
MARKER = PREFIX + '.windows'
FLUSH_MIN = 32          # see the module doc: first totals write, smallest gap
FLUSH_MAX = 65536       # largest gap between two totals writes

# The limit rule (analyse.probe_verdict uses the same numbers): a stage whose
# output moves on nearly every cycle and never waits, while its input does
# wait, is running at its own rate. Nothing downstream holds it back and
# nothing upstream starves it, so only more work per cycle in THAT stage helps.
# "Never waits" is 5%, not 0: a stage running at its own rate is still
# refused now and then by the next one, and a 2% bar would miss the stage
# the probe exists to find and hand the verdict to the backpressure rule.
LIMIT_OUT_MOVED = 0.85
LIMIT_OUT_BLOCKED = 0.05
LIMIT_IN_BLOCKED = 0.10
# Handshakes whose waiting differs by less than this are one stall seen
# through pass-through stages (a FIFO, the pre-decoder), not separate ones.
SAME_STALL = 0.02

SCALAR = {'std_logic': 'sl', 'std_ulogic': 'sl', 'boolean': 'bool'}
CLOCKS = ('clk', 'clock')
RESETS = (('reset', '1'), ('rst', '1'), ('reset_n', '0'), ('rst_n', '0'),
          ('aresetn', '0'))
PARTNERS = (('ready', 'ready'), ('pop', 'pop'))
# Words after `end` that close something nested inside a subprogram body.
_NESTED_END = {'if', 'loop', 'case', 'block', 'process', 'generate', 'record',
               'component', 'units', 'protected', 'for'}
_UNIT_START = re.compile(r'^[ \t]*(library|entity|architecture|package|configuration|context)\b',
                         re.M)


# ---------------------------------------------------------------------------
# reading VHDL

def mask(text):
    """The text with comments and string/character literals blanked out.

    Same length and the same line breaks, so an offset into the mask is an
    offset into the file. Lower case, because VHDL is case-insensitive.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '-' and text.startswith('--', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            for k in range(i, j):
                if out[k] != '\r':
                    out[k] = ' '
            i = j
        elif ch == '/' and text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] not in '\r\n':
                    out[k] = ' '
            i = j
        elif ch == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2
                        continue
                    break
                if text[j] == '\n':
                    break
                j += 1
            for k in range(i, min(j + 1, n)):
                if out[k] not in '\r\n':
                    out[k] = ' '
            i = j + 1
        elif (ch == "'" and i + 2 < n and text[i + 2] == "'"
              and not (i > 0 and (text[i - 1].isalnum() or text[i - 1] in '_)'))):
            out[i] = out[i + 1] = out[i + 2] = ' '
            i += 3
        else:
            i += 1
    return ''.join(out).lower()


def _close_paren(text, start):
    """Index just past the parenthesis that closes the one at ``start``."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _split_top(text, sep):
    """Split on ``sep`` where no parenthesis is open."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append(''.join(cur))
    return parts


_TOKEN = re.compile(r'[a-z_][a-z0-9_]*|;|\(|\)')


def _tokens(text, start, end):
    return [(m.group(0), m.start()) for m in _TOKEN.finditer(text, start, end)]


def _skip_subprogram(toks, i):
    """``toks[i]`` is the `is` of a subprogram body; index past its end."""
    n = len(toks)
    while i < n and toks[i][0] != 'begin':
        word = toks[i][0]
        if word in ('function', 'procedure'):
            j = i
            while j < n and toks[j][0] not in (';', 'is'):
                if toks[j][0] == '(':
                    depth = 1
                    j += 1
                    while j < n and depth:
                        depth += {'(': 1, ')': -1}.get(toks[j][0], 0)
                        j += 1
                    continue
                j += 1
            if j < n and toks[j][0] == 'is' and not (j + 1 < n and toks[j + 1][0] == 'new'):
                i = _skip_subprogram(toks, j + 1)
                continue
            i = j
        i += 1
    i += 1
    while i < n:
        if toks[i][0] == 'end':
            nxt = toks[i + 1][0] if i + 1 < n else ';'
            if nxt not in _NESTED_END:
                while i < n and toks[i][0] != ';':
                    i += 1
                return i + 1
        i += 1
    return n


def _decl_statements(text, start, end):
    """Top-level declarations of a declarative region, and where `begin` is.

    Returns ([statement text], offset of `begin` or None). Component
    declarations, record and protected types and subprogram bodies are
    stepped over whole, so their `end` and `begin` words are not mistaken
    for the region's.
    """
    toks = _tokens(text, start, end)
    stmts, i, n = [], 0, len(toks)
    while i < n:
        word, pos = toks[i]
        if word == 'begin':
            return stmts, pos
        if word == ';':
            i += 1
            continue
        j, depth, body = i, 0, False
        if word == 'component':
            while j < n and not (toks[j][0] == 'end' and j + 1 < n
                                 and toks[j + 1][0] == 'component'):
                j += 1
            while j < n and toks[j][0] != ';':
                j += 1
        elif word in ('function', 'procedure', 'pure', 'impure'):
            while j < n:
                if toks[j][0] == '(':
                    depth += 1
                elif toks[j][0] == ')':
                    depth -= 1
                elif depth == 0 and toks[j][0] == ';':
                    break
                elif depth == 0 and toks[j][0] == 'is':
                    if not (j + 1 < n and toks[j + 1][0] == 'new'):
                        body = True
                    break
                j += 1
            if body:
                j = _skip_subprogram(toks, j + 1)
                i = j
                continue
        else:
            while j < n:
                if toks[j][0] == '(':
                    depth += 1
                elif toks[j][0] == ')':
                    depth -= 1
                elif depth == 0 and toks[j][0] == ';':
                    break
                elif (toks[j][0] in ('record', 'protected') and depth == 0
                      and j > i and toks[j - 1][0] == 'is'):
                    kind = toks[j][0]
                    while j < n and not (toks[j][0] == 'end' and j + 1 < n
                                         and toks[j + 1][0] == kind):
                        j += 1
                    while j < n and toks[j][0] != ';':
                        j += 1
                    break
                j += 1
        stop = toks[j][1] if j < n else end
        stmts.append(text[pos:stop].strip())
        i = j + 1
    return stmts, None


_TYPE_MARK = re.compile(r'^\s*(?:(in|out|inout|buffer|linkage)\s+)?([a-z_][a-z0-9_.]*)\s*(\()?')


def _parse_object(stmt):
    """'a, b : [mode] type ...' -> ([names], mode, type mark, constrained)."""
    if ':' not in stmt:
        return None
    names, rest = stmt.split(':', 1)
    rest = rest.split(':=', 1)[0]
    m = _TYPE_MARK.match(rest)
    if not m:
        return None
    names = [x.strip() for x in names.split(',') if re.match(r'^[a-z_][a-z0-9_]*$', x.strip())]
    tmark = m.group(2).split('.')[-1]
    return names, (m.group(1) or 'in'), tmark, bool(m.group(3))


def _entity_ports(mtext, start, end):
    """{port: (mode, type mark, constrained)} of the entity header in [start, end)."""
    found = re.compile(r'\bport\s*\(').search(mtext, start, end)
    if not found:
        return {}
    close = _close_paren(mtext, found.end() - 1)
    out = {}
    for item in _split_top(mtext[found.end():close - 1], ';'):
        item = re.sub(r'^\s*signal\s+', '', item)
        parsed = _parse_object(item)
        if parsed:
            names, mode, tmark, constrained = parsed
            for name in names:
                out[name] = (mode, tmark, constrained)
    return out


def _types(stmts, records, arrays, subtypes):
    """Record, array-of and subtype declarations among ``stmts``."""
    for stmt in stmts:
        m = re.match(r'^type\s+([a-z_]\w*)\s+is\s+record\b(.*?)\bend\s+record\b', stmt, re.S)
        if m:
            valid = None
            for field in _split_top(m.group(2), ';'):
                parsed = _parse_object(field)
                if parsed and 'valid' in parsed[0] and not parsed[3]:
                    valid = parsed[2]
            if valid:
                records[m.group(1)] = valid
            continue
        m = re.match(r'^type\s+([a-z_]\w*)\s+is\s+array\s*\(.*\)\s*of\s+([a-z_][\w.]*)\s*$',
                     stmt, re.S)
        if m:
            arrays[m.group(1)] = m.group(2).split('.')[-1]
            continue
        m = re.match(r'^subtype\s+([a-z_]\w*)\s+is\s+([a-z_][\w.]*)\s*(\()?', stmt)
        if m:
            subtypes[m.group(1)] = (m.group(2).split('.')[-1], bool(m.group(3)))


def _resolve(tmark, subtypes):
    """A subtype mark resolved to its base type mark, as far as it goes."""
    seen = set()
    while tmark in subtypes and tmark not in seen:
        seen.add(tmark)
        tmark = subtypes[tmark][0]
    return tmark


_INST = re.compile(r'\b([a-z_]\w*)\s*:\s*(?:entity\s+[a-z_]\w*\.([a-z_]\w*)'
                   r'(?:\s*\(\s*[a-z_]\w*\s*\))?|component\s+([a-z_]\w*)|([a-z_]\w*))'
                   r'\s+(?=generic\s+map\b|port\s+map\b)')


def _instances(mtext, start, end):
    """[(label, entity, {formal: actual base name})] in an architecture body."""
    out = []
    pos = start
    while True:
        m = _INST.search(mtext, pos, end)
        if not m:
            break
        label = m.group(1)
        ent = m.group(2) or m.group(3) or m.group(4)
        j = m.end()
        gm = re.compile(r'generic\s+map\s*\(').match(mtext, j)
        if gm:
            j = _close_paren(mtext, gm.end() - 1)
            j = len(mtext[:j]) + (len(mtext[j:end]) - len(mtext[j:end].lstrip()))
        pm = re.compile(r'\s*port\s+map\s*\(').match(mtext, j)
        assoc = {}
        if pm:
            close = _close_paren(mtext, pm.end() - 1)
            for item in _split_top(mtext[pm.end():close - 1], ','):
                if '=>' not in item:
                    continue
                formal, actual = item.split('=>', 1)
                fm = re.match(r'\s*([a-z_]\w*)', formal)
                am = re.match(r'\s*([a-z_]\w*)', actual)
                if fm and am and am.group(1) != 'open':
                    assoc.setdefault(fm.group(1), []).append(am.group(1))
            pos = close
        else:
            pos = m.end()
        out.append((label, ent, assoc))
    return out


def _arch_end(mtext, body_start):
    """Offset of the `end` that closes the architecture whose body starts here."""
    nxt = _UNIT_START.search(mtext, body_start)
    stop = nxt.start() if nxt else len(mtext)
    last = None
    for m in re.finditer(r'\bend\b[^;]*;', mtext[body_start:stop]):
        last = m
    return body_start + last.start() if last else None


def _read_text(path):
    with open(path, 'rb') as fil:
        return fil.read().decode('latin-1')


def _vhd_files(rtl_dir):
    return [f for f in sorted(os.listdir(rtl_dir))
            if f.lower().endswith('.vhd') and not f.lower().endswith('.syn.vhd')]


def find_handshakes(rtl_dir, top=TOP):
    """Every stream handshake the RTL lets this reader see. See the module doc.

    -> {'records': {type_name: valid_field_type},
        'arches': [{'file', 'entity', 'arch', 'key', 'clk', 'reset',
                    'reset_active', 'end_offset',
                    'pairs': [{'name', 'valid', 'ready', 'style', 'src',
                               'producer', 'consumer', 'vexpr', 'rexpr'}],
                    'occupancy': [{'name', 'valid', 'producer', 'consumer', 'vexpr'}],
                    'instances': [(label, entity)], 'order': [...],
                    'skipped': [(name, why)]}],
        'skipped': [(entity(arch), why)]}
    Only architectures with at least one handshake are listed.
    """
    files = {}
    for name in _vhd_files(rtl_dir):
        text = _read_text(os.path.join(rtl_dir, name))
        files[name] = (text, mask(text))

    records, arrays, subtypes, entities = {}, {}, {}, {}
    arch_list = []
    for name, (_text, mtext) in files.items():
        for m in re.finditer(r'\bpackage\s+([a-z_]\w*)\s+is\b', mtext):
            stop = re.compile(r'\bend\s+(?:package\s+)?' + m.group(1) + r'\s*;').search(
                mtext, m.end())
            stmts, _b = _decl_statements(mtext, m.end(), stop.start() if stop else len(mtext))
            _types([s for s in stmts if s], records, arrays, subtypes)
        for m in re.finditer(r'\bentity\s+([a-z_]\w*)\s+is\b', mtext):
            stop = re.compile(r'\bend\s+(?:entity\s+)?(?:' + m.group(1) + r'\s*)?;').search(
                mtext, m.end())
            entities[m.group(1)] = _entity_ports(mtext, m.end(), stop.start() if stop else len(mtext))
        for m in re.finditer(r'\barchitecture\s+([a-z_]\w*)\s+of\s+([a-z_]\w*)\s+is\b', mtext):
            arch_list.append((name, m))

    result = {'records': dict(records), 'arches': [], 'skipped': []}
    for fname, m in arch_list:
        text, mtext = files[fname]
        key = '%s(%s)' % (m.group(2), m.group(1))
        try:
            entry = _arch_handshakes(fname, mtext, m, records, arrays, subtypes,
                                     entities, top)
        except Exception as exc:          # one odd architecture never stops the rest
            result['skipped'].append((key, 'could not be read: %s' % str(exc)[:120]))
            continue
        if entry is None:
            continue
        if isinstance(entry, str):
            result['skipped'].append((key, entry))
            continue
        result['arches'].append(entry)
    return result


def _arch_handshakes(fname, mtext, m, records, arrays, subtypes, entities, top):
    arch, ent = m.group(1), m.group(2)
    key = '%s(%s)' % (ent, arch)
    stmts, begin = _decl_statements(mtext, m.end(), len(mtext))
    if begin is None:
        return 'no `begin` found'
    end_off = _arch_end(mtext, begin)
    if end_off is None:
        return 'no closing `end` found'
    loc_records, loc_arrays, loc_subtypes = dict(records), dict(arrays), dict(subtypes)
    _types(stmts, loc_records, loc_arrays, loc_subtypes)

    # name -> (type mark, constrained, port mode or None for a local signal)
    names, order = {}, []
    ports = entities.get(ent, {})
    for pname, (mode, tmark, constrained) in ports.items():
        names[pname] = (tmark, constrained, mode)
        order.append(pname)
    for stmt in stmts:
        if not stmt.startswith('signal'):
            continue
        parsed = _parse_object(stmt[len('signal'):])
        if not parsed:
            continue
        snames, _mode, tmark, constrained = parsed
        for sname in snames:
            names[sname] = (tmark, constrained, None)
            order.append(sname)
    skipped = []
    for stmt in stmts:
        if stmt.startswith('alias'):
            am = re.match(r'^alias\s+([a-z_]\w*)', stmt)
            if am and re.search(r'(_valid|_ready|_pop)$', am.group(1)):
                skipped.append((am.group(1), 'an alias; aliases are not followed'))
    # Signals declared after `begin` live in a generate or block: not visible
    # from a process at architecture level.
    for sm in re.finditer(r'\bsignal\s+([^:;]+):', mtext[begin:end_off]):
        for sname in sm.group(1).split(','):
            sname = sname.strip()
            if re.search(r'(_valid|_ready|_pop)$', sname) or sname in ('valid',):
                skipped.append((sname, 'local to a generate or block'))

    def kind(name):
        """'sl' / 'bool' for a scalar, ('rec', vtype, is_array) for a record."""
        tmark, constrained, _mode = names[name]
        base = _resolve(tmark, loc_subtypes)
        if base in SCALAR and not constrained and not (
                tmark in loc_subtypes and loc_subtypes[tmark][1]):
            return SCALAR[base]
        if base in loc_records:
            return ('rec', loc_records[base], False)
        if base in loc_arrays and _resolve(loc_arrays[base], loc_subtypes) in loc_records:
            return ('rec', loc_records[_resolve(loc_arrays[base], loc_subtypes)], True)
        return None

    def scalar_expr(name, k):
        return name if k == 'bool' else "%s = '1'" % name

    clk = next((c for c in CLOCKS if c in names and kind(c) == 'sl'), None)
    reset, reset_active = None, None
    for rname, active in RESETS:
        if rname in names and kind(rname) in ('sl', 'bool'):
            reset, reset_active = rname, active
            break

    instances = _instances(mtext, begin, end_off)

    def side(name):
        """(producer, consumer) instance labels for a net, '^' for the port side."""
        prod = cons = None
        for label, iname, assoc in instances:
            iports = entities.get(iname, {})
            for formal, actuals in assoc.items():
                if name in actuals and formal in iports:
                    mode = iports[formal][0]
                    if mode in ('out', 'buffer'):
                        prod = prod or label
                    elif mode == 'in':
                        cons = cons or label
        if name in ports:
            if ports[name][0] == 'in':
                prod = prod or '^'
            elif ports[name][0] in ('out', 'buffer'):
                cons = cons or '^'
        return prod, cons

    labels = {}
    for label, iname, _assoc in instances:
        if label in labels and iname not in labels[label].split('/'):
            labels[label] += '/' + iname
        else:
            labels.setdefault(label, iname)

    def stage(label):
        if label in (None, '^'):
            return label
        return '%s:%s' % (label, labels.get(label, '?'))

    is_top = ent == top
    pairs, occupancy, taken = [], [], set()

    def is_local(name):
        return names[name][2] is None

    def partner(base):
        for suffix, style in PARTNERS:
            cand = '%s_%s' % (base, suffix)
            if cand in names:
                return cand, style
        return None, None

    def add(name, valid, vexpr, ready, style, rexpr, src, vnet):
        if ready is not None:
            if not (is_local(vnet) or is_local(ready) or is_top):
                skipped.append((name, 'valid and %s are both ports; counted in the parent'
                                % style))
                return
            vprod, vcons = side(vnet)
            rprod, rcons = side(ready)
            # The ready flows backwards: whoever drives it is the consumer.
            # It is the better witness of the two, because a record's other
            # fields (data) often fan out to more instances than the one that
            # takes the transfer.
            prod = vprod or rcons
            cons = rprod or vcons
            pairs.append({'name': name, 'valid': valid, 'ready': ready, 'style': style,
                          'src': src, 'vexpr': vexpr, 'rexpr': rexpr,
                          'producer': stage(prod), 'consumer': stage(cons),
                          'ports': (not is_local(vnet), not is_local(ready))})
        else:
            if not (is_local(vnet) or is_top):
                return
            prod, cons = side(vnet)
            occupancy.append({'name': name, 'valid': valid, 'vexpr': vexpr,
                              'producer': stage(prod), 'consumer': stage(cons)})
        taken.add(name)

    # Records first: a record X with X_ready says more than a loose X_valid.
    for name in order:
        k = kind(name)
        if not (isinstance(k, tuple) and k[0] == 'rec'):
            continue
        vtype = _resolve(k[1], loc_subtypes)
        if vtype not in SCALAR:
            skipped.append((name, 'its valid field is %s, not std_logic or boolean' % k[1]))
            continue
        ref = '%s(%s\'low)' % (name, name) if k[2] else name
        label = '%s(0)' % name if k[2] else name
        valid = ref + '.valid'
        vexpr = scalar_expr(valid, SCALAR[vtype])
        ready, style = partner(name)
        if ready is not None:
            rk = kind(ready)
            if rk not in ('sl', 'bool'):
                skipped.append((name, '%s is not a scalar std_logic or boolean' % ready))
                ready = None
        add(label, valid, vexpr, ready, style,
            scalar_expr(ready, kind(ready)) if ready else None, 'record', name)
    for name in order:
        if not name.endswith('_valid'):
            continue
        base = name[:-len('_valid')]
        if not base or base in taken or ('%s(0)' % base) in taken:
            continue
        k = kind(name)
        if k not in ('sl', 'bool'):
            skipped.append((base, '%s is not a scalar std_logic or boolean' % name))
            continue
        ready, style = partner(base)
        if ready is not None and kind(ready) not in ('sl', 'bool'):
            skipped.append((base, '%s is not a scalar std_logic or boolean' % ready))
            continue
        add(base, name, scalar_expr(name, k), ready, style,
            scalar_expr(ready, kind(ready)) if ready else None, 'flat', name)

    if not pairs and not occupancy:
        return None
    if clk is None:
        return 'handshakes found but no clk or clock signal to count them on'
    seq = _local_order(pairs + occupancy, [l for l, _e, _a in instances])
    return {'file': fname, 'entity': ent, 'arch': arch, 'key': key,
            'clk': clk, 'reset': reset, 'reset_active': reset_active,
            'reset_kind': kind(reset) if reset else None,
            'end_offset': end_off, 'pairs': pairs, 'occupancy': occupancy,
            'instances': [(l, labels.get(l, e)) for l, e, _a in instances],
            'order': seq, 'skipped': skipped, 'top': is_top}


def _local_order(handshakes, inst_labels):
    """Handshakes and child instances of one architecture in data-flow order.

    A handshake comes before the instance that consumes it and after the one
    that produces it; ties keep declaration order. Feedback loops are broken
    at the earliest declared node. Returns [('h', name) | ('i', label)].
    """
    nodes = [('h', h['name']) for h in handshakes]
    where = {}
    for label in inst_labels:
        if label not in where:
            where[label] = len(where)
            nodes.append(('i', label))
    edges = dict((n, set()) for n in nodes)

    def label_of(stage):
        return stage.split(':', 1)[0] if stage and stage != '^' else None

    # Ties are broken by where in the body the instance a handshake touches
    # sits: a handshake just after its producer, just before its consumer,
    # inputs from the parent first, and one that touches no instance (a
    # register between two processes) at the end in declaration order.
    rank = {}
    for i, h in enumerate(handshakes):
        node = ('h', h['name'])
        prod, cons = label_of(h['producer']), label_of(h['consumer'])
        if prod in where:
            edges[('i', prod)].add(node)
        if cons in where:
            edges[node].add(('i', cons))
        if h['producer'] == '^':
            rank[node] = (-1.0, i)
        elif prod in where:
            rank[node] = (where[prod] + 0.5, i)
        elif cons in where:
            rank[node] = (where[cons] - 0.5, i)
        else:
            rank[node] = (1e9, i)
    for label, idx in where.items():
        rank[('i', label)] = (float(idx), -1)
    indeg = dict((n, 0) for n in nodes)
    for n in nodes:
        for m in edges[n]:
            indeg[m] += 1
    out, left = [], set(nodes)
    while left:
        ready = [n for n in left if indeg[n] == 0]
        pick = min(ready or left, key=lambda n: rank[n])
        out.append(pick)
        left.discard(pick)
        for m in edges[pick]:
            if m in left:
                indeg[m] -= 1
    return out


# ---------------------------------------------------------------------------
# writing the probe

def _vhdl_str(text):
    return '"%s"' % text.replace('"', '""')


def probe_process(entry, window=WINDOW, newline='\n'):
    """The VHDL process inserted before the architecture's final `end`."""
    hs = []
    by_name = dict((h['name'], h) for h in entry['pairs'] + entry['occupancy'])
    pair_names = set(h['name'] for h in entry['pairs'])
    for kind, name in entry['order']:
        if kind == 'h':
            hs.append(by_name[name])
    n = len(hs)
    win = int(window) if window else 0
    win_len = win if win > 0 else 65536
    clk = entry['clk']
    if entry['reset']:
        if entry['reset_kind'] == 'bool':
            gate = entry['reset'] if entry['reset_active'] == '0' else 'not %s' % entry['reset']
        else:
            gate = "%s = '%s'" % (entry['reset'], '0' if entry['reset_active'] == '1' else '1')
    else:
        gate = 'true'
    P = PREFIX
    L = []
    a = L.append
    a('-- pragma translate_off')
    a('-- agentic probe: inserted by agentic/probe.py into a simulation-only copy of')
    a('-- rtl/. Reads handshakes and drives nothing; see probe.py.')
    a('%s_p: process (%s) is' % (P, clk))
    a('  type %s_cnt_t is array (0 to %d) of natural;' % (P, 3 * n - 1))
    a('  variable %s_tot : %s_cnt_t := (others => 0);' % (P, P))
    a('  variable %s_win : %s_cnt_t := (others => 0);' % (P, P))
    for var in ('cyc', 'wcyc', 'widx', 'gap', 'wbn'):
        a('  variable %s_%s : natural := 0;' % (P, var))
    a('  variable %s_next : natural := %d;' % (P, FLUSH_MIN))
    for var in ('init', 'dead', 'wins', 'wopen'):
        a('  variable %s_%s : boolean := false;' % (P, var))
    a('  variable %s_st : std.standard.file_open_status;' % P)
    a('  file %s_f : std.textio.text;' % P)
    a('  variable %s_l : std.textio.line;' % P)
    a('  variable %s_wb : std.textio.line;' % P)
    # The file name: 'path_name with every character outside [A-Za-z0-9_]
    # turned into one '.', because ':' cannot be in a Windows file name;
    # past 150 characters, the first 100 plus a hash of the whole.
    fn = [
        'function {P}_fname ({P}_s : string) return string is',
        '  variable {P}_r : string(1 to {P}_s\'length + 1);',
        '  variable {P}_k : natural := 0;',
        '  variable {P}_h : natural := 0;',
        '  variable {P}_c : character;',
        'begin',
        '  for {P}_i in {P}_s\'range loop',
        '    {P}_c := {P}_s({P}_i);',
        '    {P}_h := ({P}_h * 31 + character\'pos({P}_c)) mod 1000003;',
        "    if ({P}_c >= 'a' and {P}_c <= 'z') or ({P}_c >= 'A' and {P}_c <= 'Z') or",
        "       ({P}_c >= '0' and {P}_c <= '9') or {P}_c = '_' then",
        '      {P}_k := {P}_k + 1;',
        '      {P}_r({P}_k) := {P}_c;',
        "    elsif {P}_k > 0 and {P}_r({P}_k) /= '.' then",
        '      {P}_k := {P}_k + 1;',
        "      {P}_r({P}_k) := '.';",
        '    end if;',
        '  end loop;',
        "  if {P}_k > 0 and {P}_r({P}_k) = '.' then",
        '    {P}_k := {P}_k - 1;',
        '  end if;',
        '  if {P}_k > 150 then',
        '    return {P}_r(1 to 100) & ".h" & integer\'image({P}_h);',
        '  end if;',
        '  return {P}_r(1 to {P}_k);',
        'end function;',
    ]
    for line in fn:
        a('  ' + line.replace('{P}', P))
    a('  constant %s_path : string := %s\'path_name;' % (P, entry['entity']))
    a('  constant %s_file : string := "%s." & %s_fname(%s_path);' % (P, P, P, P))
    a('begin')
    a("  if %s'event and %s = '1' then" % (clk, clk))
    a('    if not %s_init then' % P)
    a('      %s_init := true;' % P)
    if win > 0:
        a('      std.textio.file_open(%s_st, %s_f, %s, std.standard.read_mode);'
          % (P, P, _vhdl_str(MARKER)))
        a('      if %s_st = std.standard.open_ok then' % P)
        a('        %s_wins := true;' % P)
        a('        std.textio.file_close(%s_f);' % P)
        a('      end if;')
    a('    end if;')
    a('    if (%s) and not %s_dead then' % (gate, P))
    a('      %s_cyc := %s_cyc + 1;' % (P, P))
    a('      %s_wcyc := %s_wcyc + 1;' % (P, P))
    for i, h in enumerate(hs):
        a('      if %s then' % h['vexpr'])
        if h['name'] in pair_names:
            a('        if %s then' % h['rexpr'])
            a('          %s_win(%d) := %s_win(%d) + 1;' % (P, 3 * i, P, 3 * i))
            a('        else')
            a('          %s_win(%d) := %s_win(%d) + 1;' % (P, 3 * i + 1, P, 3 * i + 1))
            a('        end if;')
        else:
            a('        %s_win(%d) := %s_win(%d) + 1;' % (P, 3 * i, P, 3 * i))
        a('      else')
        a('        %s_win(%d) := %s_win(%d) + 1;' % (P, 3 * i + 2, P, 3 * i + 2))
        a('      end if;')
    a('      if %s_wcyc = %d then' % (P, win_len))
    if win > 0:
        # Window lines are kept in a buffer and written with the totals: one
        # file open per flush, not one per window. From WSL a file on /mnt/c
        # costs about 12 ms to open, append and close, and per-window writes
        # made the probed taxi simulation 34% slower.
        a('        if %s_wins then' % P)
        a('          if %s_wbn > 0 then' % P)
        a('            std.textio.write(%s_wb, character\'val(10));' % P)
        a('          end if;')
        a('          std.textio.write(%s_wb, string\'("W "));' % P)
        a('          std.textio.write(%s_wb, %s_widx);' % (P, P))
        a('          for %s_i in 0 to %d loop' % (P, 3 * n - 1))
        a('            std.textio.write(%s_wb, string\'(" "));' % P)
        a('            std.textio.write(%s_wb, %s_win(%s_i));' % (P, P, P))
        a('          end loop;')
        a('          %s_wbn := %s_wbn + 1;' % (P, P))
        a('        end if;')
    a('        for %s_i in 0 to %d loop' % (P, 3 * n - 1))
    a('          %s_tot(%s_i) := %s_tot(%s_i) + %s_win(%s_i);' % (P, P, P, P, P, P))
    a('          %s_win(%s_i) := 0;' % (P, P))
    a('        end loop;')
    a('        %s_wcyc := 0;' % P)
    a('        %s_widx := %s_widx + 1;' % (P, P))
    a('      end if;')
    # Adaptive totals: at FLUSH_MIN, then every clamp(FLUSH_MIN, cycles/16,
    # FLUSH_MAX); the next flush cycle is worked out once per flush, not
    # divided out on every cycle.
    a('      if %s_cyc = %s_next then' % (P, P))
    a('        %s_gap := %s_cyc / 16;' % (P, P))
    a('        if %s_gap < %d then' % (P, FLUSH_MIN))
    a('          %s_gap := %d;' % (P, FLUSH_MIN))
    a('        elsif %s_gap > %d then' % (P, FLUSH_MAX))
    a('          %s_gap := %d;' % (P, FLUSH_MAX))
    a('        end if;')
    a('        %s_next := %s_cyc + %s_gap;' % (P, P, P))
    if win > 0:
        # (A counter, not `wb /= null`: "/=" on a line needs textio visible.)
        a('        if %s_wins then' % P)
        a('          if %s_wbn > 0 then' % P)
        a('            %s_wbn := 0;' % P)
        a('            if %s_wopen then' % P)
        a('              std.textio.file_open(%s_st, %s_f, %s_file & ".win", '
          'std.standard.append_mode);' % (P, P, P))
        a('            else')
        a('              std.textio.file_open(%s_st, %s_f, %s_file & ".win", '
          'std.standard.write_mode);' % (P, P, P))
        a('            end if;')
        a('            if %s_st = std.standard.open_ok then' % P)
        a('              if not %s_wopen then' % P)
        a('                %s_wopen := true;' % P)
        a('                std.textio.write(%s_l, string\'("N%s"));'
          % (P, ''.join(' ' + h['name'] for h in hs)))
        a('                std.textio.writeline(%s_f, %s_l);' % (P, P))
        a('              end if;')
        a('              std.textio.writeline(%s_f, %s_wb);' % (P, P))
        a('              std.textio.file_close(%s_f);' % P)
        a('            else')
        a('              %s_dead := true;' % P)
        a('            end if;')
        a('          end if;')
        a('        end if;')
    a('        std.textio.file_open(%s_st, %s_f, %s_file & ".tot", std.standard.write_mode);'
      % (P, P, P))
    a('        if %s_st = std.standard.open_ok then' % P)
    a('          std.textio.write(%s_l, string\'("agentic_probe 1"));' % P)
    a('          std.textio.writeline(%s_f, %s_l);' % (P, P))
    a('          std.textio.write(%s_l, string\'("path "));' % P)
    a('          std.textio.write(%s_l, %s_path);' % (P, P))
    a('          std.textio.writeline(%s_f, %s_l);' % (P, P))
    a('          std.textio.write(%s_l, string\'("arch %s %s %d"));'
      % (P, entry['entity'], entry['arch'], 1 if entry.get('top') else 0))
    a('          std.textio.writeline(%s_f, %s_l);' % (P, P))
    a('          std.textio.write(%s_l, string\'("cycles "));' % P)
    a('          std.textio.write(%s_l, %s_cyc);' % (P, P))
    a('          std.textio.writeline(%s_f, %s_l);' % (P, P))
    a('          std.textio.write(%s_l, string\'("window %d"));' % (P, win))
    a('          std.textio.writeline(%s_f, %s_l);' % (P, P))
    idx = dict((h['name'], i) for i, h in enumerate(hs))
    inst_ent = dict(entry['instances'])
    for kind, name in entry['order']:
        if kind == 'i':
            a('          std.textio.write(%s_l, string\'("I %s %s"));'
              % (P, name, inst_ent.get(name, '?')))
            a('          std.textio.writeline(%s_f, %s_l);' % (P, P))
            continue
        h = by_name[name]
        i = idx[name]
        head = 'H %s %s %s %s %s ' % ('P' if name in pair_names else 'O',
                                      h.get('style', '-'), name,
                                      h['producer'] or '-', h['consumer'] or '-')
        a('          std.textio.write(%s_l, string\'("%s"));' % (P, head))
        for j in range(3):
            if j:
                a('          std.textio.write(%s_l, string\'(" "));' % P)
            a('          std.textio.write(%s_l, %s_tot(%d) + %s_win(%d));'
              % (P, P, 3 * i + j, P, 3 * i + j))
        a('          std.textio.writeline(%s_f, %s_l);' % (P, P))
    a('          std.textio.file_close(%s_f);' % P)
    a('        else')
    a('          %s_dead := true;' % P)
    a('        end if;')
    a('      end if;')
    a('    end if;')
    a('  end if;')
    a('end process;')
    a('-- pragma translate_on')
    return newline.join(L) + newline


def instrument(rtl_dir, out_dir, window=WINDOW, exclude=(), top=TOP):
    """Copy rtl_dir to out_dir and insert the probe into the copy.

    Returns {'arches': [key], 'pairs': n, 'occupancy': n, 'skipped': [...],
    'excluded': [key], 'ranges': {key: (file, first line, last line)}}, or
    None when there is nothing to probe (the caller then simulates rtl_dir
    itself). Never writes into rtl_dir.
    """
    rtl_dir = os.path.abspath(rtl_dir)
    out_dir = os.path.abspath(out_dir)
    if out_dir == rtl_dir or out_dir.startswith(rtl_dir + os.sep):
        raise ValueError('the probe copy must live outside rtl/')
    found = find_handshakes(rtl_dir, top)
    arches = [a for a in found['arches'] if a['key'] not in set(exclude)]
    if not arches:
        return None
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    shutil.copytree(rtl_dir, out_dir)
    by_file = {}
    for entry in arches:
        by_file.setdefault(entry['file'], []).append(entry)
    ranges = {}
    for fname, entries in by_file.items():
        path = os.path.join(out_dir, fname)
        text = _read_text(path)
        newline = '\r\n' if '\r\n' in text else '\n'
        # Last first, so earlier offsets stay valid.
        for entry in sorted(entries, key=lambda e: -e['end_offset']):
            off = entry['end_offset']
            block = probe_process(entry, window, newline)
            line_start = text.rfind('\n', 0, off) + 1
            if text[line_start:off].strip():
                block = newline + block
            text = text[:off] + block + text[off:]
        with open(path, 'wb') as fil:
            fil.write(text.encode('latin-1'))
        # Line ranges of the inserted blocks, for reading a compiler error.
        for entry in entries:
            ranges[entry['key']] = (fname, None, None)
        _mark_ranges(text, fname, entries, ranges)
    return {'arches': [a['key'] for a in arches],
            'pairs': sum(len(a['pairs']) for a in arches),
            'occupancy': sum(len(a['occupancy']) for a in arches),
            'skipped': [(a['key'], n, w) for a in arches for n, w in a['skipped']]
            + [(k, '', w) for k, w in found['skipped']],
            'excluded': [k for k in exclude],
            'ranges': ranges}


def _mark_ranges(text, fname, entries, ranges):
    """Which lines of the instrumented file each probe block occupies."""
    lines = text.split('\n')
    blocks = []
    for no, line in enumerate(lines, 1):
        if line.strip().startswith('%s_p: process' % PREFIX):
            first = no - 3
            last = no
            while last < len(lines) and lines[last - 1].strip() != '-- pragma translate_on':
                last += 1
            blocks.append((first, last))
    # Blocks appear in file order; entries sorted by offset match them.
    for entry, block in zip(sorted(entries, key=lambda e: e['end_offset']), blocks):
        ranges[entry['key']] = (fname, block[0], block[1])


def culprits(log_text, info):
    """Architectures whose probe a compiler log points at.

    GHDL names errors as file:line:col. An error inside an inserted block
    names that architecture; an error elsewhere in an instrumented file names
    every probed architecture of that file (the insertion may have broken
    it); a log that names no probed file names them all.
    """
    if not info:
        return []
    hits = set()
    named_files = False
    for m in re.finditer(r'([\w.\-]+\.vhd):(\d+):', log_text or ''):
        fname, line = m.group(1), int(m.group(2))
        for key, (rfile, first, last) in info['ranges'].items():
            if rfile != fname:
                continue
            named_files = True
            if first is not None and first <= line <= last:
                hits.add(key)
        if not hits:
            for key, (rfile, _f, _l) in info['ranges'].items():
                if rfile == fname:
                    hits.add(key)
    if not named_files:
        return list(info['arches'])
    return sorted(hits)


# ---------------------------------------------------------------------------
# reading the counts back

def clear(draw_dir, keep_totals=False, keep_marker=False):
    """Delete the probe files in a draw folder: all of them (the window
    marker included) unless told to keep the totals or the marker."""
    try:
        names = os.listdir(draw_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith(PREFIX + '.'):
            continue
        if keep_totals and name.endswith('.tot'):
            continue
        if keep_marker and name == MARKER:
            continue
        try:
            os.remove(os.path.join(draw_dir, name))
        except OSError:
            pass


def clear_counts(draw_dir):
    """Delete the counts a run left (before running the folder again), keeping
    the window marker, which says what the folder may hold, not what it holds."""
    clear(draw_dir, keep_marker=True)


def read(draw_dir):
    """{instance path: {'cycles', 'entity', 'arch', 'top', 'window', 'hs': {name: [m, b, e]},
    'meta': {name: (kind, style, producer, consumer)}, 'order': [...],
    'windows': [[idx, counts...]], 'names': [...]}} from one draw folder."""
    out = {}
    try:
        names = sorted(os.listdir(draw_dir))
    except OSError:
        return out
    for name in names:
        if not (name.startswith(PREFIX + '.') and name.endswith('.tot')):
            continue
        try:
            with open(os.path.join(draw_dir, name), encoding='latin-1') as fil:
                lines = fil.read().splitlines()
        except OSError:
            continue
        rec = {'cycles': 0, 'hs': {}, 'meta': {}, 'order': [], 'windows': [],
               'names': [], 'file': name}
        path = None
        for line in lines:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == 'path' and len(parts) >= 2:
                path = line[5:].strip()
            elif parts[0] == 'arch' and len(parts) >= 4:
                rec['entity'], rec['arch'], rec['top'] = parts[1], parts[2], parts[3] == '1'
            elif parts[0] == 'cycles' and len(parts) == 2:
                rec['cycles'] = int(parts[1])
            elif parts[0] == 'window' and len(parts) == 2:
                rec['window'] = int(parts[1])
            elif parts[0] == 'I' and len(parts) >= 3:
                rec['order'].append(('i', parts[1], parts[2]))
            elif parts[0] == 'H' and len(parts) == 9:
                _h, kind, style, hname, prod, cons, mv, bl, em = parts
                rec['hs'][hname] = [int(mv), int(bl), int(em)]
                rec['meta'][hname] = (kind, style, None if prod == '-' else prod,
                                      None if cons == '-' else cons)
                rec['order'].append(('h', hname))
        if path is None or not rec['hs']:
            continue
        win = os.path.join(draw_dir, name[:-4] + '.win')
        if os.path.exists(win):
            try:
                with open(win, encoding='latin-1') as fil:
                    for line in fil:
                        parts = line.split()
                        if parts and parts[0] == 'N':
                            rec['names'] = parts[1:]
                        elif parts and parts[0] == 'W':
                            rec['windows'].append([int(x) for x in parts[1:]])
            except (OSError, ValueError):
                rec['windows'] = []
        out[path] = rec
    return out


def _split_path(path):
    return [p for p in path.split(':') if p]


def summarise(probe, keep_windows=False):
    """Fractions per handshake, in data-flow order, from read()'s result.

    -> {'cycles', 'handshakes': [{'key', 'name', 'kind', 'style', 'moved',
        'blocked', 'empty', 'producer', 'consumer'}], 'stages': {key: entity},
        'window_limit_share': {stage: share} (only with window data),
        'windows': [...] (only with keep_windows)}
    or None when there is nothing. Keys are relative to the top instance:
    'co' at the top, 'datapath_inst/el' one level down.
    """
    if not probe:
        return None
    paths = sorted(probe, key=lambda p: len(_split_path(p)))
    top = next((p for p in paths if probe[p].get('top')), paths[0])
    top_parts = _split_path(top)

    def rel(path):
        parts = _split_path(path)
        if parts[:len(top_parts)] == top_parts:
            parts = parts[len(top_parts):]
        return '/'.join(parts)

    def stage_key(path, label_ent):
        if label_ent is None:
            return (rel(path) + '/' if rel(path) else '') + '(local)'
        if label_ent == '^':
            parent = rel(path)
            return parent.rsplit('/', 1)[0] if '/' in parent else '(outside)'
        label = label_ent.split(':', 1)[0]
        base = rel(path)
        # A child under a generate keeps the generate in its path.
        for other in paths:
            r = rel(other)
            if r.startswith(base + '/' if base else '') and r.split('/')[-1] == label:
                return r
        return (base + '/' if base else '') + label

    stages = {}
    for path in paths:
        for kind, *rest in probe[path]['order']:
            if kind == 'i':
                stages[stage_key(path, rest[0] + ':' + rest[1])] = rest[1]
        r = rel(path)
        stages.setdefault(r or 'top', probe[path].get('entity', '?'))

    def children(path):
        """Probe paths directly below ``path`` for an instance label."""
        out = {}
        plen = len(_split_path(path))
        for other in paths:
            parts = _split_path(other)
            if len(parts) > plen and _split_path(path) == parts[:plen]:
                inner = [p for p in paths if p != other and
                         _split_path(p)[:plen] == _split_path(path) and
                         len(_split_path(p)) > plen and
                         _split_path(other)[:len(_split_path(p))] == _split_path(p)]
                if not inner:
                    out.setdefault(parts[-1], []).append(other)
        return out

    ordered = []
    done = set()

    def walk(path):
        if path in done:
            return
        done.add(path)
        rec = probe[path]
        kids = children(path)
        used = set()
        for item in rec['order']:
            if item[0] == 'i':
                for kid in kids.get(item[1], []):
                    used.add(kid)
                    walk(kid)
            else:
                ordered.append((path, item[1]))
        for label in sorted(kids):
            for kid in kids[label]:
                if kid not in used:
                    walk(kid)

    walk(top)
    for path in paths:
        walk(path)

    cycles = max(probe[p]['cycles'] for p in paths) or 1
    hs = []
    for path, name in ordered:
        rec = probe[path]
        mv, bl, em = rec['hs'][name]
        cyc = max(1, rec['cycles'])
        kind, style, prod, cons = rec['meta'][name]
        key = (rel(path) + '/' if rel(path) else '') + name
        hs.append({'key': key, 'name': name,
                   'kind': 'pair' if kind == 'P' else 'occupancy',
                   'style': style if kind == 'P' else None,
                   'moved': round(mv / float(cyc), 4),
                   'blocked': round(bl / float(cyc), 4),
                   'empty': round(em / float(cyc), 4),
                   'producer': stage_key(path, prod),
                   'consumer': stage_key(path, cons)})
    out = {'cycles': cycles, 'handshakes': hs, 'stages': stages}
    wins = _windows(probe, ordered, rel)
    if wins:
        out['window_limit_share'] = _window_share(hs, wins)
        if keep_windows:
            out['windows'] = wins
    return out


def _windows(probe, ordered, rel):
    """[{key: (m, b, e)}] per window index, over every instance with windows."""
    per = {}
    for path, name in ordered:
        rec = probe[path]
        if not rec['windows'] or name not in rec['names']:
            continue
        pos = rec['names'].index(name)
        key = (rel(path) + '/' if rel(path) else '') + name
        for row in rec['windows']:
            idx, counts = row[0], row[1:]
            if 3 * pos + 2 < len(counts):
                per.setdefault(idx, {})[key] = tuple(counts[3 * pos:3 * pos + 3])
    return [per[i] for i in sorted(per)]


def stage_io(handshakes):
    """{stage: {'in': [handshake], 'out': [handshake]}} in data-flow order."""
    out = {}
    order = []
    for h in handshakes:
        if h['kind'] != 'pair':
            continue
        for role, stage in (('out', h['producer']), ('in', h['consumer'])):
            if stage is None:
                continue
            if stage not in out:
                out[stage] = {'in': [], 'out': []}
            out[stage][role].append(h)
    for h in handshakes:
        if h['kind'] == 'pair' and h['consumer'] and h['consumer'] not in order:
            order.append(h['consumer'])
    for stage in out:
        if stage not in order:
            order.append(stage)
    # A stage that holds probed stages of its own (datapath_inst around the
    # decoder) is judged only after them: its ports would otherwise name the
    # whole pipeline when one stage inside it is the one at its rate.
    keys = [h['key'] for h in handshakes]
    inner = [s for s in order if not any(k.startswith(s + '/') for k in keys)]
    outer = [s for s in order if s not in inner]
    outer.sort(key=lambda s: -s.count('/'))
    return out, inner + outer


def stalled_by(handshakes):
    """The handshake whose consumer holds the pipe back, or None: the one that
    waits most. On a tie (within SAME_STALL) the most downstream one of the
    inner stages wins: one stall deep inside backs up through every stage
    before it, so they all wait about as much, and the outermost consumer is
    only the wrapper around all of them."""
    pairs = [h for h in handshakes if h['kind'] == 'pair']
    if not pairs:
        return None
    top = max(h['blocked'] for h in pairs)
    tied = [h for h in pairs if h['blocked'] >= top - SAME_STALL]
    _io, order = stage_io(handshakes)
    keys = [h['key'] for h in handshakes]
    inner = set(s for s in order if not any(k.startswith(s + '/') for k in keys))
    pick = [h for h in tied if h['consumer'] in inner] or tied
    return pick[-1]                 # handshakes are in data-flow order


def meets_limit(io):
    """Whether one stage is running at its own rate (the limit rule)."""
    if not io['in'] or not io['out']:
        return False
    best = max(io['out'], key=lambda h: h['moved'])
    return (best['moved'] >= LIMIT_OUT_MOVED and best['blocked'] <= LIMIT_OUT_BLOCKED
            and max(h['blocked'] for h in io['in']) >= LIMIT_IN_BLOCKED)


def _window_share(hs, wins):
    """Share of windows in which each stage is the limit: the first stage, in
    the verdict's own order (inner stages before the ones around them), that
    meets the limit rule in that window."""
    counts = {}
    for win in wins:
        frac = []
        for h in hs:
            got = win.get(h['key'])
            if not got:
                continue
            tot = float(max(1, sum(got)))
            frac.append(dict(h, moved=got[0] / tot,
                             blocked=(got[1] / tot) if h['kind'] == 'pair' else 0.0,
                             empty=got[2] / tot))
        io, order = stage_io(frac)
        for stage in order:
            if meets_limit(io[stage]):
                counts[stage] = counts.get(stage, 0) + 1
                break
    return dict((s, round(c / float(len(wins)), 3)) for s, c in counts.items())


def combine(probes):
    """Cycle-weighted fractions over several draws' summaries (no windows)."""
    probes = [p for p in probes if p and p.get('handshakes')]
    if not probes:
        return None
    first = probes[0]
    total = {}
    weight = {}
    for p in probes:
        cyc = float(p.get('cycles') or 1)
        for h in p['handshakes']:
            acc = total.setdefault(h['key'], [0.0, 0.0, 0.0])
            acc[0] += h['moved'] * cyc
            acc[1] += h['blocked'] * cyc
            acc[2] += h['empty'] * cyc
            weight[h['key']] = weight.get(h['key'], 0.0) + cyc
    hs = []
    seen = set()
    for p in probes:
        for h in p['handshakes']:
            if h['key'] in seen:
                continue
            seen.add(h['key'])
            w = weight[h['key']] or 1.0
            acc = total[h['key']]
            hs.append(dict(h, moved=round(acc[0] / w, 4), blocked=round(acc[1] / w, 4),
                           empty=round(acc[2] / w, 4)))
    stages = {}
    for p in probes:
        stages.update(p.get('stages') or {})
    return {'cycles': int(sum(p.get('cycles') or 0 for p in probes)),
            'handshakes': hs, 'stages': stages or first.get('stages', {}),
            'draws': len(probes)}


def digest(rtl_dir):
    """A hash of the probe-relevant RTL (for callers that cache instrumentation)."""
    h = hashlib.sha256()
    for name in _vhd_files(rtl_dir):
        h.update(name.encode())
        with open(os.path.join(rtl_dir, name), 'rb') as fil:
            h.update(fil.read())
    return h.hexdigest()[:16]


def main():
    import argparse
    ap = argparse.ArgumentParser(description='List the handshakes the probe would count.')
    ap.add_argument('rtl')
    args = ap.parse_args()
    found = find_handshakes(args.rtl)
    for arch in found['arches']:
        print('%s  (%s, clk=%s, reset=%s)' % (arch['key'], arch['file'], arch['clk'],
                                              arch['reset']))
        for p in arch['pairs']:
            print('  pair  %-14s %-6s %-7s %s -> %s' % (p['name'], p['style'], p['src'],
                                                       p['producer'], p['consumer']))
        for o in arch['occupancy']:
            print('  occ   %-14s %s -> %s' % (o['name'], o['producer'], o['consumer']))
        for name, why in arch['skipped']:
            print('  skip  %-14s %s' % (name, why))
    for key, why in found['skipped']:
        print('skipped %s: %s' % (key, why))


if __name__ == '__main__':
    main()
