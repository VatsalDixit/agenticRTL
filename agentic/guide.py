#!/usr/bin/env python3
"""
A guide to the design as it is right now, generated from the RTL.

Every candidate session used to spend the first half of its budget working
out the same things: which file holds what, how wide the ports are, what the
records passed between stages contain, which module feeds which. Measured
over twelve sessions, the time before the first edit was six to fourteen
minutes, about 60% of the session, and all of it was paid again by the next
session because the worktree (and everything the session had understood) was
deleted at the end of the iteration.

This module writes that knowledge down once per adopted design. It is
generated from the RTL itself, so it cannot drift: when the design changes,
the guide is rebuilt. It describes the design and nothing else -- no advice,
no directions, no bottleneck claims -- so it cannot narrow what a session
proposes.

    python agentic/guide.py                 print the guide for rtl/
    python agentic/guide.py --rtl DIR --out FILE

The loop writes it to <run>/guide.md after the baseline and after every
adoption, and pastes it into each session's brief.
"""

import argparse
import os
import re
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import analyse                                        # noqa: E402

TOP = 'vhsnunzip_unbuffered'
PKG = 'vhsnunzip_int_pkg.vhd'

# Caps, so one huge file cannot push the guide past a few thousand tokens.
MAX_BLOCKS_PER_FILE = 16
MAX_FIELDS_PER_RECORD = 28
MAX_CONSTANTS = 30

RE_ENTITY = re.compile(r'^\s*entity\s+(\w+)\s+is\b', re.I)
RE_ARCH = re.compile(r'^\s*architecture\s+(\w+)\s+of\s+(\w+)', re.I)
RE_PROCESS = re.compile(r'^\s*(\w+)\s*:\s*process\b', re.I)
RE_GENERATE = re.compile(r'^\s*(\w+)\s*:\s*(?:for|if)\b.*\bgenerate\b', re.I)
RE_INST = re.compile(r'^\s*(\w+)\s*:\s*(?:entity\s+work\.)?(vhsnunzip_\w+)\s*$', re.I)
RE_INST_GEN = re.compile(r'^\s*(\w+)\s*:\s*(?:entity\s+work\.)?(vhsnunzip_\w+)\s+'
                         r'(?:generic|port)\s+map', re.I)
RE_FIELD = re.compile(r'^\s*(\w+)\s*:\s*([^;]+);')


def read(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as fil:
            return fil.read()
    except IOError:
        return ''


def rtl_files(rtl_dir):
    if not os.path.isdir(rtl_dir):
        return []
    return sorted(f for f in os.listdir(rtl_dir) if f.endswith('.vhd'))


# ---------------------------------------------------------------------------
# what is in each file

def file_map(rtl_dir, name):
    """Entities, processes, generate blocks and instances, with line numbers."""
    lines = read(os.path.join(rtl_dir, name)).splitlines()
    out = {'lines': len(lines), 'entities': [], 'blocks': [], 'instances': []}
    open_stack = []
    for num, line in enumerate(lines, 1):
        bare = line.split('--')[0]
        if not bare.strip():
            continue
        found = RE_ENTITY.match(bare)
        if found:
            out['entities'].append(found.group(1))
            continue
        found = RE_ARCH.match(bare)
        if found and found.group(2) not in out['entities']:
            out['entities'].append(found.group(2))
        found = RE_PROCESS.match(bare)
        if found:
            open_stack.append(('process', found.group(1), num))
            continue
        found = RE_GENERATE.match(bare)
        if found:
            open_stack.append(('generate', found.group(1), num))
            continue
        found = RE_INST.match(bare) or RE_INST_GEN.match(bare)
        if found:
            out['instances'].append((found.group(1), found.group(2), num))
            continue
        low = bare.strip().lower()
        if low.startswith('end process') or low.startswith('end generate'):
            kind = 'process' if low.startswith('end process') else 'generate'
            for idx in range(len(open_stack) - 1, -1, -1):
                if open_stack[idx][0] == kind:
                    _kind, label, start = open_stack.pop(idx)
                    out['blocks'].append((kind, label, start, num))
                    break
    for kind, label, start in open_stack:          # unterminated, still useful
        out['blocks'].append((kind, label, start, len(lines)))
    out['blocks'].sort(key=lambda b: b[2])
    return out


# ---------------------------------------------------------------------------
# the records that move between stages

def records(rtl_dir):
    """Every record type in the internal package, with its fields."""
    text = read(os.path.join(rtl_dir, PKG))
    lines = text.splitlines()
    out = []
    current = None
    for num, line in enumerate(lines, 1):
        bare = line.split('--')[0]
        found = re.match(r'^\s*type\s+(\w+)\s+is\s+record\b', bare, re.I)
        if found:
            current = {'name': found.group(1), 'line': num, 'fields': []}
            continue
        if current is None:
            continue
        if re.match(r'^\s*end\s+record\b', bare, re.I):
            out.append(current)
            current = None
            continue
        field = RE_FIELD.match(bare)
        if field:
            current['fields'].append((field.group(1),
                                      ' '.join(field.group(2).split())))
    return out


def size_of(decl, consts):
    """Bytes or bits a field declaration covers, when that can be read off."""
    found = re.search(r'byte_array\s*\(\s*(.+?)\s+to\s+(.+?)\s*\)', decl, re.I)
    if found:
        try:
            lo = analyse._eval_int(found.group(1), consts)
            hi = analyse._eval_int(found.group(2), consts)
            return '%d bytes' % (hi - lo + 1)
        except Exception:
            return ''
    found = re.search(r'(?:std_logic_vector|unsigned|signed)\s*\(\s*(.+?)'
                      r'\s+downto\s+(.+?)\s*\)', decl, re.I)
    if found:
        try:
            hi = analyse._eval_int(found.group(1), consts)
            lo = analyse._eval_int(found.group(2), consts)
            return '%d bits' % (hi - lo + 1)
        except Exception:
            return ''
    if re.match(r'^\s*std_logic\s*$', decl, re.I):
        return '1 bit'
    return ''


# ---------------------------------------------------------------------------
# which module feeds which

def module_tree(rtl_dir):
    """Instances per entity, as a tree rooted at the top entity."""
    children = {}
    for name in rtl_files(rtl_dir):
        info = file_map(rtl_dir, name)
        for ent in info['entities']:
            children.setdefault(ent, [])
        for label, target, _line in info['instances']:
            owner = info['entities'][0] if info['entities'] else name
            children.setdefault(owner, []).append((label, target))
    lines = []

    def walk(ent, depth, seen):
        for label, target in children.get(ent, []):
            lines.append('%s%s : %s' % ('  ' * depth, label, target))
            if target not in seen:
                walk(target, depth + 1, seen | {target})
    lines.append(TOP)
    walk(TOP, 1, {TOP})
    return lines


# ---------------------------------------------------------------------------

def build_guide(rtl_dir, widths=None, notes=None, facts=None):
    """The whole guide, as markdown text."""
    widths = widths or analyse.rtl_widths(rtl_dir)
    names = rtl_files(rtl_dir)
    texts = [read(os.path.join(rtl_dir, f)) for f in names if f.endswith('_pkg.vhd')]
    texts.append(read(os.path.join(rtl_dir, TOP + '.vhd')))
    consts = analyse._constants(texts)

    out = []
    out.append('# The design as it is now')
    out.append('')
    out.append('Generated from rtl/ by agentic/guide.py. It is a map, not advice: '
               'it says what exists and where, not what to change. If it disagrees '
               'with the code, the code is right and the guide is stale. The line '
               'ranges below are meant to be read with Read offset and limit, so '
               'you pull in one process rather than a whole file.')
    out.append('')

    out.append('## Sizes, read from the RTL')
    out.append('')
    out.append('| what | value |')
    out.append('|---|---|')
    out.append('| co_data (compressed in) | %d bytes, cnt %d bits |'
               % (widths['in_bytes'], widths['in_cnt_bits']))
    out.append('| de_data (decompressed out) | %d bytes, cnt %d bits |'
               % (widths['out_bytes'], widths['out_cnt_bits']))
    out.append('| core line | %d bytes |' % int(widths['core_line_bytes']))
    out.append('| cores | %d |' % widths['cores'])
    out.append('| element slots per transfer | %d copy, %d literal |'
               % (widths.get('copy_slots', 1), widths.get('literal_slots', 1)))
    out.append('')

    # A zero is almost always a constant this reader could not evaluate
    # (a function call or a generic it cannot see), not a real zero.
    picked = [(k, v) for k, v in sorted(consts.items())
              if v and not k.lower().startswith(('dbg', 'test'))][:MAX_CONSTANTS]
    if picked:
        out.append('## Constants')
        out.append('')
        out.append(', '.join('%s = %d' % (k, v) for k, v in picked))
        out.append('')

    out.append('## Records passed between stages (%s)' % PKG)
    out.append('')
    for rec in records(rtl_dir):
        out.append('### %s  (line %d)' % (rec['name'], rec['line']))
        fields = rec['fields'][:MAX_FIELDS_PER_RECORD]
        for fname, decl in fields:
            size = size_of(decl, consts)
            out.append('- `%s` : %s%s' % (fname, decl, '  [%s]' % size if size else ''))
        if len(rec['fields']) > len(fields):
            out.append('- ... %d more fields' % (len(rec['fields']) - len(fields)))
        out.append('')

    out.append('## Module tree')
    out.append('')
    out.append('```')
    out.extend(module_tree(rtl_dir))
    out.append('```')
    out.append('')

    out.append('## Files, with line numbers to read from')
    out.append('')
    for name in names:
        info = file_map(rtl_dir, name)
        out.append('### rtl/%s  (%d lines)' % (name, info['lines']))
        if info['entities']:
            out.append('entities: %s' % ', '.join(sorted(set(info['entities']))))
        shown = info['blocks'][:MAX_BLOCKS_PER_FILE]
        for kind, label, start, end in shown:
            out.append('- %s `%s` lines %d-%d' % (kind, label, start, end))
        if len(info['blocks']) > len(shown):
            out.append('- ... %d more blocks' % (len(info['blocks']) - len(shown)))
        out.append('')

    if facts:
        out.append('## Facts measured on this design')
        out.append('')
        for fact in facts:
            out.append('- %s' % fact)
        out.append('')

    if notes:
        out.append('## How it works, from the session whose change was adopted')
        out.append('')
        out.append(notes.strip())
        out.append('')

    return '\n'.join(out).rstrip() + '\n'


def write_guide(path, rtl_dir, widths=None, notes=None, facts=None):
    text = build_guide(rtl_dir, widths=widths, notes=notes, facts=facts)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fil:
        fil.write(text)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--rtl', default=os.path.join(ROOT, 'rtl'))
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    text = build_guide(os.path.abspath(args.rtl))
    if args.out:
        with open(args.out, 'w', encoding='utf-8', newline='\n') as fil:
            fil.write(text)
        print('%s: %d characters, about %d tokens'
              % (args.out, len(text), len(text) // 4))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
