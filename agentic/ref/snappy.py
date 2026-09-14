"""
Pure-Python raw-Snappy compressor for vhsnunzip test-vector generation.

This replaces the upstream dependency on the `snzip` C command-line tool, so the
repository is fully self-contained: no C toolchain, no external binaries, no
submodules.  It emits the *raw* Snappy format that
``emu.operators.decoder`` understands: a varint of the decompressed length
followed by a sequence of literal / copy tags.

Only the 1-byte and 2-byte copy-offset forms are produced.  The 4-byte form
(``tag & 3 == 3``) is deliberately avoided because neither the hardware nor the
Python model support it (``emu/operators.py`` raises ``oh_snap`` on it).  Chunks
are at most 64 kiB, so a 2-byte offset (<= 65535) is always sufficient.

The compressor is a straightforward greedy LZ77 matcher with a 4-byte hash
table, mirroring the structure of Snappy's fast path.  It is not tuned for
ratio -- it is tuned for producing valid, well-varied streams (literals, short
copies, long copies and run-length/overlapping copies) that exercise the
decoder datapath.  Every produced stream is round-tripped through an
independent reference decompressor when ``verify=True``.
"""

import random

_MIN_MATCH = 4          # Snappy minimum copy length
_MAX_OFF = 65535        # fits in a 2-byte offset
_HASH_BITS = 14


def _emit_literal(out, lit):
    """Append a literal element covering the bytes in ``lit``."""
    n = len(lit)
    if n == 0:
        return
    val = n - 1                          # length is stored diminished-one
    if val < 60:
        out.append(val << 2)             # tag 00, length in the top 6 bits
    else:
        # 60..63 select 1..4 little-endian extra length bytes.
        extra = bytearray()
        v = val
        while v:
            extra.append(v & 0xff)
            v >>= 8
        out.append((59 + len(extra)) << 2)
        out.extend(extra)
    out.extend(lit)


def _emit_copy(out, offset, length):
    """Append one or more copy elements for ``length`` bytes at ``offset``."""
    while length > 0:
        if offset < 2048 and 4 <= length <= 11:
            # 1-byte offset form (tag 01): length 4..11, offset 1..2047.
            out.append(0x01 | ((length - 4) << 2) | ((offset >> 8) << 5))
            out.append(offset & 0xff)
            length = 0
        else:
            # 2-byte offset form (tag 10): length 1..64, offset 1..65535.
            take = min(length, 64)
            out.append(0x02 | ((take - 1) << 2))
            out.append(offset & 0xff)
            out.append((offset >> 8) & 0xff)
            length -= take


def _hash(word):
    return ((word * 0x1e35a7bd) & 0xffffffff) >> (32 - _HASH_BITS)


def compress_raw(data):
    """Compress a single chunk (``bytes``) into raw Snappy format (``bytes``)."""
    out = bytearray()

    # Preamble: decompressed length as a varint.
    n = len(data)
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break

    size = len(data)
    table = [-1] * (1 << _HASH_BITS)
    lit_start = 0
    i = 0
    while i + _MIN_MATCH <= size:
        word = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)
        h = _hash(word)
        cand = table[h]
        table[h] = i
        if cand >= 0 and i - cand <= _MAX_OFF and data[cand:cand + 4] == data[i:i + 4]:
            # Flush pending literals, then emit the (possibly overlapping) copy.
            _emit_literal(out, data[lit_start:i])
            offset = i - cand
            length = 4
            while i + length < size and data[cand + length] == data[i + length]:
                length += 1
            _emit_copy(out, offset, length)
            i += length
            lit_start = i
        else:
            i += 1

    _emit_literal(out, data[lit_start:size])
    return bytes(out)


def decompress_raw(comp):
    """Independent reference decompressor.

    Used for the compressor self-check below, and by ``parquet_pages.py`` to
    work out what a real Parquet page is supposed to expand to.  Deliberately
    strict: a block with trailing junk or a length preamble that disagrees with
    the payload raises rather than returning a plausible-looking result.
    """
    i = 0
    shift = 0
    length = 0
    while True:
        b = comp[i]
        i += 1
        length |= (b & 0x7f) << shift
        shift += 7
        if not (b & 0x80):
            break

    out = bytearray()
    while i < len(comp):
        tag = comp[i]
        kind = tag & 3
        if kind == 0:                                   # literal
            ln = tag >> 2
            i += 1
            if ln >= 60:
                nbytes = ln - 59
                ln = 0
                for k in range(nbytes):
                    ln |= comp[i + k] << (8 * k)
                i += nbytes
            ln += 1
            out.extend(comp[i:i + ln])
            i += ln
        elif kind == 1:                                 # copy, 1-byte offset
            ln = ((tag >> 2) & 7) + 4
            off = (((tag >> 5) & 7) << 8) | comp[i + 1]
            i += 2
            for _ in range(ln):
                out.append(out[len(out) - off])
        elif kind == 2:                                 # copy, 2-byte offset
            ln = (tag >> 2) + 1
            off = comp[i + 1] | (comp[i + 2] << 8)
            i += 3
            for _ in range(ln):
                out.append(out[len(out) - off])
        else:                                           # copy, 4-byte offset
            raise ValueError('4-byte copy offset is not supported')

    if length != len(out):
        raise ValueError('length preamble does not match decompressed size')
    return bytes(out)


# Kept so the older private name still works.
_decompress_raw = decompress_raw


def check_raw(comp):
    """Report why ``comp`` cannot be fed to the model, or ``None`` if it can.

    Data compressed elsewhere -- a Parquet page, say -- may use parts of the
    Snappy format this datapath does not implement.  Checking up front means a
    bad block is skipped with an explanation instead of blowing up half way
    through writing the vector files and leaving broken ones behind.

    Returns a short reason string, suitable for printing next to the block it
    describes.
    """
    if not comp:
        return 'empty block'

    # The length preamble is a varint; the pre-decoder carries its length in a
    # 3-bit field, and the RTL saturates at 5 bytes.
    i = 0
    while i < len(comp) and comp[i] & 0x80:
        i += 1
        if i >= 5:
            return 'length preamble longer than 5 bytes'
    if i >= len(comp):
        return 'truncated length preamble'

    # Neither the model nor the RTL implements the 4-byte copy offset form.
    # It only shows up when data repeats from more than 64kiB back, which the
    # reference Snappy compressor never does, but other compressors might.
    i += 1
    try:
        while i < len(comp):
            tag = comp[i]
            kind = tag & 3
            if kind == 0:
                ln = tag >> 2
                i += 1
                if ln >= 60:
                    nbytes = ln - 59
                    ln = 0
                    for k in range(nbytes):
                        ln |= comp[i + k] << (8 * k)
                    i += nbytes
                i += ln + 1
            elif kind == 1:
                i += 2
            elif kind == 2:
                i += 3
            else:
                return '4-byte copy offset is not supported'
    except IndexError:
        return 'block is truncated'
    if i != len(comp):
        return 'element runs past the end of the block'

    try:
        decompress_raw(comp)
    except ValueError as exc:
        return str(exc)
    return None


def compress(data, bindir=None, snzip_args=None, verify=False,
             chunk_size=65536, max_chunk_size=None, min_chunk_size=None,
             max_prob=0.0):
    """Split ``data`` into chunks and compress each into raw Snappy format.

    Signature-compatible with the upstream ``emu.snappy.compress`` so the rest
    of the test-vector generator is unchanged.  ``bindir`` / ``snzip_args`` are
    accepted and ignored (kept for drop-in compatibility).  When ``verify`` is
    set, every chunk is round-tripped through ``_decompress_raw``.

    Returns ``(compressed_chunks, uncompressed_chunks)``.
    """
    if max_chunk_size is None:
        max_chunk_size = chunk_size
    if min_chunk_size is None:
        min_chunk_size = chunk_size

    offset = 0
    uncompressed_chunks = []
    compressed_chunks = []
    if data:
        while offset < len(data):
            if random.random() < max_prob:
                size = max_chunk_size
            else:
                size = random.randint(min_chunk_size, max_chunk_size)
            chunk = data[offset:offset + size]
            uncompressed_chunks.append(chunk)
            offset += len(chunk)
    else:
        uncompressed_chunks.append(b'')

    for chunk in uncompressed_chunks:
        comp = compress_raw(chunk)
        if verify and _decompress_raw(comp) != chunk:
            raise ValueError('compressor self-check failed!')
        compressed_chunks.append(comp)

    return compressed_chunks, uncompressed_chunks
