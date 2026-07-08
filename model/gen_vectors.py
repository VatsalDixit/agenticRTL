#!/usr/bin/env python3
"""
Test-vector generator for the vhsnunzip decompressor.

Runs the Python golden model (``emu/``) over some input data and serialises the
stream transfers at every datapath boundary into ``*.tv`` files that the VHDL
testbenches read back and self-check against:

    in.tv   compressed input   (wide)         <- vhsnunzip_tc
    out.tv  decompressed output (wide)         <- vhsnunzip_tc
    cs.tv   compressed input   (single line)   <- unbuffered / pre_decoder / pipeline
    cd.tv   pre-decoder output                 <- pre_decoder / decoder / pipeline
    el.tv   decoder output (elements)          <- decoder(_long) / cmd_gen_1 / pipeline
    c1.tv   cmd_gen stage 1 output             <- cmd_gen_1 / cmd_gen_2 / pipeline
    cm.tv   cmd_gen stage 2 output             <- cmd_gen_2 / pipeline
    de.tv   decompressed output (single line)  <- unbuffered / pipeline

The compressed data is produced by the pure-Python Snappy compressor in
``emu/snappy.py`` (no external tools required).  The whole model is
self-verified via ``verifier`` before the vectors are considered valid.

Usage:
    python gen_vectors.py [INPUT_FILE] [options]

    INPUT_FILE        file to compress and decompress; if omitted, a built-in
                      sample text is used so the script runs with zero args.

    --out DIR         where to write the *.tv files (default: ../vectors)
    --seed N          RNG seed for chunk splitting (default: 0)
    --chunk N         nominal chunk size in bytes (default: 65536)
    --min N           minimum chunk size (default: --chunk)
    --max N           maximum chunk size (default: --chunk)
    --max-prob P      probability of forcing a max-size chunk (default: 0)
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from emu.operators import (
    data_source, wide_data_source, pre_decoder, decoder,
    cmd_gen_1, cmd_gen_2, datapath, verifier, writer, drain, Counter,
)
from emu.snappy import compress


# A compressible built-in sample so `python gen_vectors.py` works with no args.
# Repetition and shared substrings exercise literals, short copies, long copies
# and run-length (overlapping) copies.
_SAMPLE = (
    b"the quick brown fox jumps over the lazy dog. " * 40
    + b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
    + b"the quick brown fox jumps over the lazy dog again and again. " * 30
    + bytes(range(256)) * 8
    + b"vhsnunzip vhsnunzip vhsnunzip decompresses snappy in hardware. " * 25
)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', nargs='?', default=None)
    ap.add_argument('--out', default=None)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--chunk', type=int, default=65536)
    ap.add_argument('--min', dest='min_chunk', type=int, default=None)
    ap.add_argument('--max', dest='max_chunk', type=int, default=None)
    ap.add_argument('--max-prob', dest='max_prob', type=float, default=0.0)
    args = ap.parse_args()

    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       '..', 'vectors')
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    def j(name):
        return os.path.join(out_dir, name)

    random.seed(args.seed)

    if args.input:
        with open(args.input, 'rb') as fin:
            data = fin.read()
        print('Read %d bytes from %s' % (len(data), args.input))
    else:
        data = _SAMPLE
        print('Using built-in sample (%d bytes)' % len(data))

    print('Compressing (pure-Python raw Snappy)...')
    compressed, uncompressed = compress(
        data,
        min_chunk_size=args.min_chunk if args.min_chunk is not None else args.chunk,
        max_chunk_size=args.max_chunk if args.max_chunk is not None else args.chunk,
        max_prob=args.max_prob,
        verify=True,
    )
    total_c = sum(len(c) for c in compressed)
    print('  %d chunk(s), %d -> %d bytes (%.1f%%)'
          % (len(compressed), len(data), total_c,
             100.0 * total_c / max(1, len(data))))

    print('Writing wide input/output vectors...')
    drain(writer(wide_data_source(compressed), j('in.tv')))
    drain(writer(wide_data_source(uncompressed), j('out.tv')))

    print('Running golden model and writing datapath vectors...')
    cs = Counter(writer(data_source(compressed), j('cs.tv')))
    cd = Counter(writer(pre_decoder(cs), j('cd.tv')))
    el = Counter(writer(decoder(cd), j('el.tv')))
    c1 = Counter(writer(cmd_gen_1(el), j('c1.tv')))
    cm = Counter(writer(cmd_gen_2(c1), j('cm.tv')))
    de = Counter(writer(datapath(cm), j('de.tv')))
    drain(verifier(de, uncompressed))

    print('Model self-check passed. Vectors written to %s' % out_dir)
    print('  transfers: cs=%d cd=%d el=%d c1=%d cm=%d de=%d'
          % (cs.count, cd.count, el.count, c1.count, cm.count, de.count))


if __name__ == '__main__':
    main()
