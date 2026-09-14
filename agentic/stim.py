#!/usr/bin/env python3
"""
Stimulus for the loop, written as cs.tv files the throughput testbench reads.

Two kinds:

  * real pages    Snappy blocks lifted straight out of Parquet files
                  (agentic/data/*.parquet, TPC-H tables). One page is one
                  chunk. This is the data the design exists to decompress.
  * synthetic     a built-in text sample compressed by the frozen reference
                  compressor into chunks of a chosen size. These cover
                  chunk-boundary behaviour that real pages rarely exercise.

The loop scores a design on the REAL draws (train table plus the held-out
tables). Synthetic draws must decompress correctly but never enter the score;
an earlier loop let one synthetic draw outvote every real one.

cs.tv format (one line per 8-byte granule):
    64 chars   data bits, byte 0 first, MSB of each byte first
     1 char    last granule of the chunk
     3 chars   index of the last valid byte in this granule (7 = full)

Usage:
    python agentic/stim.py --list
    python agentic/stim.py --build            build every draw into .agentic/corpus
"""

import argparse
import json
import os
import random
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, os.path.join(KIT, 'ref'))

from snappy import compress, decompress_raw, check_raw   # noqa: E402
from parquet_pages import iter_pages                     # noqa: E402

DATA_DIRS = [os.path.join(KIT, 'data'), os.path.join(ROOT, 'test_data')]

# The table the agent gets per-draw feedback about.
TRAIN_TABLES = ('lineitem',)
# Scored, but the agent never sees these names or their individual numbers.
HELD_OUT_TABLES = ('orders', 'customer', 'part', 'partsupp',
                   'supplier', 'nation', 'region')
# Chunk sizes for the synthetic draws (bytes).
SYNTHETIC_CHUNKS = (512, 8192)

MAX_BLOCK = 65536

SAMPLE = (
    b"the quick brown fox jumps over the lazy dog. " * 40
    + b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
    + b"the quick brown fox jumps over the lazy dog again and again. " * 30
    + bytes(range(256)) * 8
    + b"vhsnunzip vhsnunzip vhsnunzip decompresses snappy in hardware. " * 25
)


def table_path(table):
    for base in DATA_DIRS:
        path = os.path.join(base, '%s.parquet' % table)
        if os.path.exists(path):
            return path
    raise IOError('no Parquet file for table %s (looked in %s)'
                  % (table, ', '.join(DATA_DIRS)))


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


def page_chunks(table, pages, seed):
    """Draw usable Snappy pages out of one table. Returns (chunks, info)."""
    path = table_path(table)
    pool = [p for p in iter_pages(path, None, None, True)
            if p.uncompressed_size <= MAX_BLOCK]
    usable = [p for p in pool if check_raw(p.data) is None]
    exhaustive = len(usable) <= pages
    if not exhaustive:
        random.Random(seed).shuffle(usable)
    chosen = usable[:pages]
    chunks = []
    for page in chosen:
        plain = decompress_raw(page.data)
        if page.kind != 'data_v2' and len(plain) != page.uncompressed_size:
            raise ValueError('%s: a page expands to %d bytes but its header '
                             'says %d' % (table, len(plain),
                                          page.uncompressed_size))
        chunks.append(page.data)
    info = {'table': table, 'usable_pages': len(usable),
            'pages': len(chunks), 'exhaustive': exhaustive, 'seed': seed}
    return chunks, info


def synthetic_chunks(chunk_size):
    random.seed(0)
    comp, _plain = compress(SAMPLE, min_chunk_size=chunk_size,
                            max_chunk_size=chunk_size, max_prob=0.0,
                            verify=True)
    return comp


class Draw(object):
    """One stimulus set, with a stable name."""

    def __init__(self, kind, name, table=None, chunk=None, pages=12, seed=0,
                 scored=True, visible=True):
        self.kind = kind          # 'real' or 'synthetic'
        self.name = name
        self.table = table
        self.chunk = chunk
        self.pages = pages
        self.seed = seed
        self.scored = scored      # enters the throughput score
        self.visible = visible    # the agent may see this draw's number

    def as_dict(self):
        return dict(kind=self.kind, name=self.name, table=self.table,
                    chunk=self.chunk, pages=self.pages, seed=self.seed,
                    scored=self.scored, visible=self.visible)

    def chunks(self):
        if self.kind == 'real':
            chunks, _info = page_chunks(self.table, self.pages, self.seed)
            return chunks
        return synthetic_chunks(self.chunk)


def all_draws(pages=12, seed=0):
    """The full draw list, in a fixed order."""
    draws = []
    for table in TRAIN_TABLES:
        draws.append(Draw('real', 'train-%s' % table, table=table,
                          pages=pages, seed=seed, scored=True, visible=True))
    for size in SYNTHETIC_CHUNKS:
        draws.append(Draw('synthetic', 'synth-%dB' % size, chunk=size,
                          scored=False, visible=True))
    for table in HELD_OUT_TABLES:
        draws.append(Draw('real', 'held-%s' % table, table=table,
                          pages=pages, seed=seed, scored=True, visible=False))
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
    chunks = draw.chunks()
    if not chunks:
        raise ValueError('draw %s produced no chunks' % draw.name)
    lines = write_cs_tv(chunks, os.path.join(out_dir, 'cs.tv'))
    meta = {'draw': want, 'chunks': len(chunks), 'granules': lines,
            'compressed_bytes': sum(len(c) for c in chunks),
            'expanded_bytes': sum(len(decompress_raw(c)) for c in chunks)}
    with open(meta_path, 'w', encoding='utf-8') as fil:
        json.dump(meta, fil, indent=1, sort_keys=True)
    return out_dir, len(chunks)


def build_all(corpus_dir, pages=12, seed=0):
    out = []
    for draw in all_draws(pages=pages, seed=seed):
        path, count = build(draw, corpus_dir)
        out.append((draw, path, count))
    return out


# ---------------------------------------------------------------------------
# Invented shapes for a candidate's own self-test. Chosen to break things:
# chunk sizes straddling line widths, overlapping copies, one big chunk.

SELFTEST_SHAPES = (
    ('tiny-chunks', 24),
    ('line-straddle-17', 17),
    ('line-straddle-33', 33),
    ('line-straddle-65', 65),
    ('sub-line', 7),
    ('medium', 1024),
    ('single-chunk', 0),
)


def selftest_sample():
    body = (b'the quick brown fox jumps over the lazy dog. ' * 24
            + b'a' * 96
            + bytes(range(256)) * 4
            + b'vhsnunzip decompresses snappy in hardware. ' * 16)
    rnd = random.Random(20260901)
    tail = bytes(rnd.randrange(256) for _ in range(512))
    return body + tail + body[:400]


def selftest_chunks(chunk):
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
    ap.add_argument('--pages', type=int, default=12)
    ap.add_argument('--seed', type=int, default=0)
    args = ap.parse_args()

    draws = all_draws(pages=args.pages, seed=args.seed)
    if args.list or not args.build:
        for d in draws:
            print('%-20s %-9s scored=%-5s visible=%s' % (
                d.name, d.kind, d.scored, d.visible))
        return 0
    for draw in draws:
        path, count = build(draw, args.corpus)
        print('%-20s %3d chunk(s)  %s' % (draw.name, count, path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
