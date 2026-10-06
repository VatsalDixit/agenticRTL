#!/usr/bin/env python3
"""
The elements of a raw Snappy chunk, as the decompressor's decoder sees them.

check.py writes them next to the invented stimulus (elements.tv) so a unit
test can compare a decoder against the exact element stream.
"""


def parse(c):
    """Elements of one Snappy chunk as (pos, kind, hdr, length, offset).

    kind is 'L' (literal) or 'C' (copy); pos is the header's byte offset in
    the chunk, hdr the header length, so a literal's payload is
    c[pos+hdr : pos+hdr+length].
    """
    i = 0
    while True:                     # the uncompressed-length varint
        b = c[i]
        i += 1
        if b < 128:
            break
    els = []
    while i < len(c):
        b = c[i]
        t = b & 3
        if t == 0:
            ln = b >> 2
            if ln < 60:
                h, L = 1, ln + 1
            else:
                nb = ln - 59
                h = 1 + nb
                L = int.from_bytes(c[i + 1:i + 1 + nb], 'little') + 1
            els.append((i, 'L', h, L, 0))
            i += h + L
        elif t == 1:
            els.append((i, 'C', 2, ((b >> 2) & 7) + 4, ((b >> 5) << 8) | c[i + 1]))
            i += 2
        elif t == 2:
            els.append((i, 'C', 3, (b >> 2) + 1, c[i + 1] | (c[i + 2] << 8)))
            i += 3
        else:
            els.append((i, 'C', 5, (b >> 2) + 1, int.from_bytes(c[i + 1:i + 5], 'little')))
            i += 5
    return els
