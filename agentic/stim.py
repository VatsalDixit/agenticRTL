#!/usr/bin/env python3
"""
Stimulus for the loop, written as cs.tv files the throughput testbench reads.

Two kinds:

  * real row groups   every Snappy page of row group 0 of a Parquet file, in
                      file order, fed back to back: one page is one chunk.
                      The files (agentic/data/, rebuilt by make_data.py) are
                      written with DuckDB's default Parquet settings, so a
                      page runs from a few hundred bytes to about 16 MB and
                      most of the bytes are in pages far longer than 64 KiB.
                      This is the data the design exists to decompress.
  * synthetic         a built-in text sample compressed by the frozen
                      reference compressor into chunks of a chosen size.
                      These cover chunk-boundary behaviour.

The loop scores a design on the train table (NYC taxi trips) and the held-out
TPC-H SF1 tables. nation and region must decompress correctly but are not
scored: their whole row group is a few kilobytes, and at that size bytes per
cycle measures how fast a chunk starts, not throughput. Synthetic draws must
decompress correctly and never enter the score; an earlier loop let one
synthetic draw outvote every real one.

An earlier corpus drew 12 random pages of at most 64 KiB from each table. It
rewarded what small chunks reward, and its gains did not carry over to whole
row groups.

cs.tv format (one line per 8-byte granule):
    64 chars   data bits, byte 0 first, MSB of each byte first
     1 char    last granule of the chunk
     3 chars   index of the last valid byte in this granule (7 = full)

Usage:
    python agentic/stim.py --list
    python agentic/stim.py --build            build every draw into .agentic/corpus
"""

import argparse
import hashlib
import json
import os
import random
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, os.path.join(KIT, 'ref'))

from snappy import compress, compress_raw, decompress_raw, check_raw   # noqa: E402
from parquet_pages import iter_pages                                   # noqa: E402

DATA_DIRS = [os.path.join(KIT, 'data'), os.path.join(ROOT, 'test_data')]

# The table the agent gets per-draw feedback about.
TRAIN_TABLES = ('taxi',)
# Scored, but the agent never sees these names or their individual numbers.
HELD_OUT_TABLES = ('lineitem', 'orders', 'customer', 'part', 'partsupp',
                   'supplier')
# Must decompress correctly; too small for bytes/cycle to mean throughput.
SMALL_TABLES = ('nation', 'region')
# The Parquet file behind each table.
FILES = dict([('taxi', 'taxi.parquet')]
             + [(t, 'tpch1_%s.parquet' % t) for t in HELD_OUT_TABLES + SMALL_TABLES])
# Which row group of each file is fed. DuckDB writes 122,880 rows per group.
ROW_GROUP = 0
# Chunk sizes for the synthetic draws (bytes).
SYNTHETIC_CHUNKS = (512, 8192)
# The history holds this much; a chunk longer than it wraps the history.
HISTORY_BYTES = 65536

SAMPLE = (
    b"the quick brown fox jumps over the lazy dog. " * 40
    + b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
    + b"the quick brown fox jumps over the lazy dog again and again. " * 30
    + bytes(range(256)) * 8
    + b"vhsnunzip vhsnunzip vhsnunzip decompresses snappy in hardware. " * 25
)


def table_path(table):
    name = FILES.get(table, '%s.parquet' % table)
    for base in DATA_DIRS:
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    raise IOError('no Parquet file %s for table %s (looked in %s; '
                  'python agentic/make_data.py writes them)'
                  % (name, table, ', '.join(DATA_DIRS)))


def write_cs_tv(chunks, path):
    """Write compressed chunks as the 8-byte-granule stimulus file."""
    lines = []
    for chunk in chunks:
        if not chunk:
            raise ValueError('empty chunk cannot be written')
        for pos in range(0, len(chunk), 8):
            gran = chunk[pos:pos + 8]
            last = pos + 8 >= len(chunk)
            endi = len(gran) - 1
            padded = gran + b'\x00' * (8 - len(gran))
            bits = ''.join(format(b, '08b') for b in padded)
            lines.append(bits + ('1' if last else '0') + format(endi, '03b'))
    with open(path, 'w', encoding='ascii', newline='\n') as fil:
        fil.write('\n'.join(lines) + '\n')
    return len(lines)


def page_chunks(table):
    """Every usable Snappy page of the table's row group, in file order.

    Returns (chunks, info). A page the design cannot be fed (check_raw) is
    left out and counted in info; none of the shipped files has one.
    """
    path = table_path(table)
    chunks, skipped, sizes = [], [], []
    for page in iter_pages(path, None, {ROW_GROUP}, True):
        why = check_raw(page.data)
        if why:
            skipped.append(why)
            continue
        plain = decompress_raw(page.data)
        if page.kind != 'data_v2' and len(plain) != page.uncompressed_size:
            raise ValueError('%s: a page expands to %d bytes but its header '
                             'says %d' % (table, len(plain),
                                          page.uncompressed_size))
        chunks.append(page.data)
        sizes.append(len(plain))
    info = {'table': table, 'file': os.path.basename(path),
            'row_group': ROW_GROUP, 'pages': len(chunks),
            'unusable_pages': len(skipped), 'unusable_reasons': sorted(set(skipped)),
            'expanded_bytes': sum(sizes), 'largest_page_bytes': max(sizes or [0]),
            'long_pages': sum(1 for s in sizes if s > HISTORY_BYTES)}
    return chunks, info


def synthetic_chunks(chunk_size):
    random.seed(0)
    comp, _plain = compress(SAMPLE, min_chunk_size=chunk_size,
                            max_chunk_size=chunk_size, max_prob=0.0,
                            verify=True)
    return comp


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fil:
        for block in iter(lambda: fil.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


class Draw(object):
    """One stimulus set, with a stable name."""

    def __init__(self, kind, name, table=None, chunk=None, scored=True,
                 visible=True):
        self.kind = kind          # 'real' or 'synthetic'
        self.name = name
        self.table = table
        self.chunk = chunk
        self.scored = scored      # enters the throughput score
        self.visible = visible    # the agent may see this draw's number

    def as_dict(self):
        out = dict(kind=self.kind, name=self.name, table=self.table,
                   chunk=self.chunk, scored=self.scored, visible=self.visible)
        if self.kind == 'real':
            # A rewritten data file must not be served from a stale cache.
            path = table_path(self.table)
            out.update(file=os.path.basename(path), row_group=ROW_GROUP,
                       file_bytes=os.path.getsize(path))
        return out

    def chunks(self):
        return self.build()[0]

    def build(self):
        """(chunks, info) for this draw."""
        if self.kind == 'real':
            return page_chunks(self.table)
        return synthetic_chunks(self.chunk), {}


def all_draws():
    """The full draw list, in a fixed order."""
    draws = []
    for table in TRAIN_TABLES:
        draws.append(Draw('real', 'train-%s' % table, table=table,
                          scored=True, visible=True))
    for size in SYNTHETIC_CHUNKS:
        draws.append(Draw('synthetic', 'synth-%dB' % size, chunk=size,
                          scored=False, visible=True))
    for table in HELD_OUT_TABLES:
        draws.append(Draw('real', 'held-%s' % table, table=table,
                          scored=True, visible=False))
    for table in SMALL_TABLES:
        draws.append(Draw('real', 'held-%s' % table, table=table,
                          scored=False, visible=False))
    return draws


def build(draw, corpus_dir):
    """Write a draw's cs.tv (cached). Returns (dir, chunk_count)."""
    out_dir = os.path.join(corpus_dir, draw.name)
    meta_path = os.path.join(out_dir, 'meta.json')
    want = draw.as_dict()
    if os.path.exists(meta_path) and os.path.exists(os.path.join(out_dir, 'cs.tv')):
        try:
            with open(meta_path, encoding='utf-8') as fil:
                meta = json.load(fil)
            if meta.get('draw') == want:
                return out_dir, meta['chunks']
        except (ValueError, IOError, KeyError):
            pass
    os.makedirs(out_dir, exist_ok=True)
    chunks, info = draw.build()
    if not chunks:
        raise ValueError('draw %s produced no chunks' % draw.name)
    lines = write_cs_tv(chunks, os.path.join(out_dir, 'cs.tv'))
    meta = {'draw': want, 'chunks': len(chunks), 'granules': lines,
            'compressed_bytes': sum(len(c) for c in chunks)}
    if draw.kind == 'real':
        meta.update(info)
        meta['file_sha256'] = _sha256(table_path(draw.table))
    else:
        meta['expanded_bytes'] = sum(len(decompress_raw(c)) for c in chunks)
    with open(meta_path, 'w', encoding='utf-8') as fil:
        json.dump(meta, fil, indent=1, sort_keys=True)
    return out_dir, len(chunks)


def build_all(corpus_dir):
    out = []
    for draw in all_draws():
        path, count = build(draw, corpus_dir)
        out.append((draw, path, count))
    return out


# ---------------------------------------------------------------------------
# Invented shapes for a candidate's own self-test. Chosen to break things:
# chunk sizes straddling line widths, overlapping copies, one big chunk, and
# one chunk longer than the history.

SELFTEST_SHAPES = (
    ('tiny-chunks', 24),
    ('line-straddle-17', 17),
    ('line-straddle-33', 33),
    ('line-straddle-65', 65),
    ('sub-line', 7),
    ('medium', 1024),
    ('single-chunk', 0),
    ('long-chunk', 'long'),
)


def selftest_sample():
    body = (b'the quick brown fox jumps over the lazy dog. ' * 24
            + b'a' * 96
            + bytes(range(256)) * 4
            + b'vhsnunzip decompresses snappy in hardware. ' * 16)
    rnd = random.Random(20260901)
    tail = bytes(rnd.randrange(256) for _ in range(512))
    return body + tail + body[:400]


def _varint(n):
    out = bytearray()
    while True:
        low, n = n & 0x7f, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def selftest_long_chunk():
    """One 192 KiB chunk, three times the history.

    Real pages are like this: one chunk far longer than the 64 KiB history,
    so the history wraps while the chunk is still running, and copies reach
    up to 64 KiB back. Built the way Snappy builds a long chunk: fragments
    under 64 KiB compressed on their own and joined under one length header.
    Each fragment ends with a copy 65,000 bytes back, next to the far end of
    what the history holds; between those, a literal of 1,000 bytes and long
    overlapping copies (offsets 7, 13 and 1).
    """
    rnd = random.Random(20260930)
    plain, body = bytearray(), bytearray()
    for period in (7, 13, 1):
        head = bytes(rnd.randrange(256) for _ in range(1000))
        unit = bytes(rnd.randrange(256) for _ in range(period))
        fill = (unit * (64000 // period + 1))[:64000]
        frag = head + fill + head[:500]
        comp = compress_raw(frag)
        start = 0
        while comp[start] & 0x80:
            start += 1
        body += comp[start + 1:]
        plain += frag
    chunk = _varint(len(plain)) + bytes(body)
    if decompress_raw(chunk) != bytes(plain):
        raise ValueError('the long self-test chunk does not round-trip')
    return [chunk]


def selftest_chunks(chunk):
    if chunk == 'long':
        return selftest_long_chunk()
    data = selftest_sample()
    random.seed(0)
    size = chunk or len(data)
    comp, _plain = compress(data, min_chunk_size=size, max_chunk_size=size,
                            max_prob=0.0, verify=True)
    return comp


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--build', action='store_true')
    ap.add_argument('--corpus', default=os.path.join(ROOT, '.agentic', 'corpus'))
    args = ap.parse_args()

    draws = all_draws()
    if args.list or not args.build:
        for d in draws:
            print('%-20s %-9s scored=%-5s visible=%s' % (
                d.name, d.kind, d.scored, d.visible))
        return 0
    for draw in draws:
        path, count = build(draw, args.corpus)
        with open(os.path.join(path, 'meta.json'), encoding='utf-8') as fil:
            meta = json.load(fil)
        print('%-20s %3d chunk(s)  %10d bytes  largest %9s  %s'
              % (draw.name, count, meta.get('expanded_bytes') or 0,
                 meta.get('largest_page_bytes', '-'), path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
