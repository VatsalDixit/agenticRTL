#!/usr/bin/env python3
"""
The check that decides whether a design is correct.

    compressed bytes  ->  RTL  ->  decompressed bytes
           |                              |
           +--> decompress_raw (frozen) --+--> compare

Both ends are outside the reach of anything the optimising agent may edit.
The input is read back out of the stimulus the RTL was given (cs.tv), and the
expected output comes from agentic/ref/snappy.py, which is hash-pinned by
agentic/freeze.py. A candidate that rewrites the RTL cannot rewrite the answer.

The comparison is over BYTES, not transfers. Which bytes appear in which
transfer is a property of the micro-architecture and an architectural change
is allowed to change it. Which bytes come out is not negotiable.

Files:
    cs.tv    the stimulus the RTL was fed (8-byte granules, one per line)
    out.hex  what the testbench saw come back (hex per transfer, EOC per chunk)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ref'))

from snappy import decompress_raw  # noqa: E402  (frozen; the reference)


class Undefined(ValueError):
    """The design emitted values that are not bytes at all."""


def read_stimulus(cs_tv):
    """Recover the compressed chunks from the stimulus file.

    Line layout: 8 bytes of 8 bits (MSB first), then last(1), then endi(3),
    endi being the index of the last valid byte in the line.
    """
    chunks, cur = [], bytearray()
    with open(cs_tv, encoding='ascii') as fil:
        for line in fil:
            line = line.strip()
            if not line:
                continue
            endi = int(line[-3:], 2)
            last = line[-4] == '1'
            for idx in range(endi + 1):
                bits = line[idx * 8:(idx + 1) * 8]
                if '-' in bits:
                    raise ValueError('%s: valid byte %d is a don\'t-care'
                                     % (cs_tv, idx))
                cur.append(int(bits, 2))
            if last:
                chunks.append(bytes(cur))
                cur = bytearray()
    if cur:
        raise ValueError('%s: ends mid-chunk (no final last flag)' % cs_tv)
    return chunks


def read_output(out_hex):
    """Recover the decompressed chunks the testbench recorded."""
    chunks, cur = [], bytearray()
    with open(out_hex, encoding='ascii', errors='replace') as fil:
        for line in fil:
            line = line.strip()
            if line == 'EOC':
                chunks.append(bytes(cur))
                cur = bytearray()
            elif line:
                try:
                    cur.extend(bytes.fromhex(line))
                except ValueError:
                    bad = ''.join(c for c in line
                                  if c not in '0123456789abcdefABCDEF')
                    raise Undefined(
                        'the design drove undefined values at its output '
                        '(%d bytes in): the line contains %r, so some byte '
                        'lanes were never driven'
                        % (len(cur), sorted(set(bad))[:4]))
    if cur:
        chunks.append(bytes(cur))
    return chunks


def _where(got, want):
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            lo = max(0, i - 8)
            return ('at byte %d: got %s, expected %s'
                    % (i, got[lo:i + 8].hex(), want[lo:i + 8].hex()))
    return 'lengths differ: got %d bytes, expected %d' % (len(got), len(want))


def check(cs_tv, out_hex):
    """Compare what came out against what the reference says should have.

    Returns a list of problems; empty means every chunk was correct.
    """
    for path in (cs_tv, out_hex):
        if not os.path.exists(path):
            return ['missing %s' % path]

    compressed = read_stimulus(cs_tv)
    try:
        produced = read_output(out_hex)
    except Undefined as exc:
        return [str(exc)]
    expected = [decompress_raw(c) for c in compressed]

    problems = []
    if len(produced) != len(expected):
        problems.append('produced %d chunk(s), expected %d'
                        % (len(produced), len(expected)))
    for idx, (got, want) in enumerate(zip(produced, expected)):
        if got != want:
            problems.append('chunk %d differs, %s' % (idx, _where(got, want)))
    return problems


def summary(cs_tv, out_hex):
    compressed = read_stimulus(cs_tv)
    produced = read_output(out_hex)
    return {
        'chunks': len(produced),
        'compressed_bytes': sum(len(c) for c in compressed),
        'decompressed_bytes': sum(len(c) for c in produced),
    }


def main():
    if len(sys.argv) != 3:
        print('usage: oracle.py CS_TV OUT_HEX')
        return 2
    problems = check(sys.argv[1], sys.argv[2])
    if problems:
        print('ORACLE CHECK FAILED:')
        for p in problems:
            print('  - %s' % p)
        return 1
    info = summary(sys.argv[1], sys.argv[2])
    print('Oracle check passed: %d chunk(s), %d compressed bytes -> %d '
          'decompressed, every byte matching the reference decompressor.'
          % (info['chunks'], info['compressed_bytes'],
             info['decompressed_bytes']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
