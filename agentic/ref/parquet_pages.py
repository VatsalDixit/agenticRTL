#!/usr/bin/env python3
"""
Pull the raw Snappy blocks out of a Parquet file.

A Parquet file is nested::

    file
     +- row group          a horizontal slice of rows
         +- column chunk   one column's data inside that row group
             +- page       a few kiB of that column -- COMPRESSED ON ITS OWN

One page is one independently compressed Snappy block, which is exactly what
vhsnunzip calls a "chunk".  (A Parquet *column chunk* is just a container
holding many pages; it is not a vhsnunzip chunk.)  So the pages of a real
Parquet file are drop-in stimulus for the decompressor: no re-compressing and
no format conversion, these are the bytes the hardware would see in production.

This module only reads; ``gen_vectors.py`` is what runs the model and writes
the ``*.tv`` files.

Usage:
    python parquet_pages.py FILE [options]

    --list            print one line per page and a summary (the default)
    --column NAME     only this column; repeatable, matches the full dotted
                      path or just the leaf name
    --row-group N     only this row group; repeatable
    --no-dict         skip dictionary pages
    --max-block N     flag pages whose decompressed size exceeds N bytes
                      (default 65536, the decoder's limit; 0 disables)

Need a file to test with?  Any Snappy-compressed Parquet will do; TPC-H is one
keystroke away in DuckDB, and ROW_GROUP_SIZE is the knob that controls page
size (the default of ~120k rows makes pages past the 64kiB limit)::

    INSTALL tpch; LOAD tpch; CALL dbgen(sf=0.01);
    COPY lineitem TO 'lineitem.parquet'
        (FORMAT parquet, COMPRESSION snappy, ROW_GROUP_SIZE 20000);

DuckDB is only a convenient way to produce a file -- nothing here depends on
it, or on anything outside the standard library.

Implementation note: Parquet writes its index and its page headers using
Thrift's compact binary encoding, so a small reader for that lives at the top
of this file.  It walks fields generically, which means unknown ones are
skipped safely and we only have to name the handful we actually want.
"""

import argparse
import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from snappy import check_raw
except ImportError:  # running inside the original repo layout
    from emu.snappy import check_raw


# -- Thrift compact reader ---------------------------------------------------
#
# Field types, as encoded in the low nibble of a field header.  Only the ones
# Parquet actually uses are handled; anything else raises.
_T_TRUE = 1
_T_FALSE = 2
_T_BYTE = 3
_T_I16 = 4
_T_I32 = 5
_T_I64 = 6
_T_DOUBLE = 7
_T_BINARY = 8
_T_LIST = 9
_T_SET = 10
_T_MAP = 11
_T_STRUCT = 12


def _read_uvarint(buf, i):
    """Read an unsigned varint, returning ``(value, next_index)``."""
    val = 0
    shift = 0
    while True:
        byte = buf[i]
        i += 1
        val |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return val, i


def _read_zigzag(buf, i):
    """Read a zigzag-encoded signed varint, returning ``(value, next_index)``."""
    val, i = _read_uvarint(buf, i)
    return (val >> 1) ^ -(val & 1), i


def _read_value(buf, i, ttype):
    """Read one value of ``ttype``, returning ``(value, next_index)``."""
    if ttype == _T_TRUE:
        return True, i
    if ttype == _T_FALSE:
        return False, i
    if ttype == _T_BYTE:
        return buf[i], i + 1
    if ttype in (_T_I16, _T_I32, _T_I64):
        return _read_zigzag(buf, i)
    if ttype == _T_DOUBLE:
        return buf[i:i + 8], i + 8
    if ttype == _T_BINARY:
        size, i = _read_uvarint(buf, i)
        return buf[i:i + size], i + size
    if ttype in (_T_LIST, _T_SET):
        header = buf[i]
        i += 1
        count = header >> 4
        elem_type = header & 0x0f
        if count == 15:
            count, i = _read_uvarint(buf, i)
        out = []
        for _ in range(count):
            # Booleans inside a collection take a whole byte each.
            if elem_type in (_T_TRUE, _T_FALSE):
                out.append(buf[i] == 1)
                i += 1
            else:
                val, i = _read_value(buf, i, elem_type)
                out.append(val)
        return out, i
    if ttype == _T_MAP:
        count, i = _read_uvarint(buf, i)
        out = {}
        if count:
            header = buf[i]
            i += 1
            key_type = header >> 4
            val_type = header & 0x0f
            for _ in range(count):
                key, i = _read_value(buf, i, key_type)
                val, i = _read_value(buf, i, val_type)
                out[key] = val
        return out, i
    if ttype == _T_STRUCT:
        return _read_struct(buf, i)
    raise ValueError('unsupported thrift type %d at offset %d' % (ttype, i))


def _read_struct(buf, i):
    """Read a struct into ``{field_id: value}``, returning ``(dict, next_index)``."""
    out = {}
    field_id = 0
    while True:
        header = buf[i]
        i += 1
        if header == 0:
            return out, i
        ttype = header & 0x0f
        delta = header >> 4
        if delta:
            field_id += delta
        else:
            field_id, i = _read_zigzag(buf, i)
        out[field_id], i = _read_value(buf, i, ttype)


# -- Parquet layout ----------------------------------------------------------
#
# Field numbers come from parquet.thrift upstream.  Only the ones we need are
# named; _read_struct skips the rest on its own.
_FM_ROW_GROUPS = 4              # FileMetaData.row_groups
_RG_COLUMNS = 1                 # RowGroup.columns
_CC_META_DATA = 3               # ColumnChunk.meta_data
_CM_PATH_IN_SCHEMA = 3          # ColumnMetaData.path_in_schema
_CM_CODEC = 4                   # ColumnMetaData.codec
_CM_TOTAL_COMPRESSED_SIZE = 7   # ColumnMetaData.total_compressed_size
_CM_DATA_PAGE_OFFSET = 9        # ColumnMetaData.data_page_offset
_CM_DICT_PAGE_OFFSET = 11       # ColumnMetaData.dictionary_page_offset

_PH_TYPE = 1                    # PageHeader.type
_PH_UNCOMPRESSED_SIZE = 2       # PageHeader.uncompressed_page_size
_PH_COMPRESSED_SIZE = 3         # PageHeader.compressed_page_size
_PH_DATA_PAGE_V2 = 8            # PageHeader.data_page_header_v2

_V2_DEF_LEVEL_BYTES = 5         # DataPageHeaderV2.definition_levels_byte_length
_V2_REP_LEVEL_BYTES = 6         # DataPageHeaderV2.repetition_levels_byte_length
_V2_IS_COMPRESSED = 7           # DataPageHeaderV2.is_compressed

_CODEC_SNAPPY = 1

_PAGE_KINDS = {0: 'data', 1: 'index', 2: 'dict', 3: 'data_v2'}

_MAGIC = b'PAR1'

# One page of one column.  ``data`` is the raw Snappy block, ready for the
# model exactly as-is.
Page = namedtuple('Page', [
    'row_group', 'column', 'kind', 'uncompressed_size', 'compressed_size',
    'offset', 'data',
])


def read_chunks(fil):
    """Read the index at the end of the file and yield its Snappy column chunks.

    Yields ``(row_group_index, dotted_column_name, start_offset, byte_count)``.
    Chunks compressed with anything other than Snappy are skipped.
    """
    fil.seek(0, os.SEEK_END)
    size = fil.tell()
    if size < 12:
        raise ValueError('too small to be a Parquet file (%d bytes)' % size)

    fil.seek(-8, os.SEEK_END)
    tail = fil.read(8)
    if tail[4:] != _MAGIC:
        raise ValueError('missing PAR1 footer magic -- not a Parquet file')
    footer_len = int.from_bytes(tail[:4], 'little')

    fil.seek(size - 8 - footer_len)
    meta, _ = _read_struct(fil.read(footer_len), 0)

    for rg_index, row_group in enumerate(meta.get(_FM_ROW_GROUPS, [])):
        for column in row_group.get(_RG_COLUMNS, []):
            cmd = column.get(_CC_META_DATA)
            if cmd is None or cmd.get(_CM_CODEC) != _CODEC_SNAPPY:
                continue
            path = b'.'.join(cmd.get(_CM_PATH_IN_SCHEMA, [])).decode('utf-8')
            # The dictionary page, when present, sits before the data pages.
            offsets = [o for o in (cmd.get(_CM_DICT_PAGE_OFFSET),
                                   cmd.get(_CM_DATA_PAGE_OFFSET)) if o]
            if not offsets:
                continue
            yield rg_index, path, min(offsets), cmd[_CM_TOTAL_COMPRESSED_SIZE]


def iter_pages(path, columns=None, row_groups=None, include_dict=True):
    """Yield a :class:`Page` for every Snappy page in the Parquet file at ``path``.

    ``columns`` matches either the full dotted path or the leaf name;
    ``row_groups`` is a collection of row-group indices.  Both default to
    everything.
    """
    with open(path, 'rb') as fil:
        for rg_index, column, start, length in read_chunks(fil):
            if row_groups is not None and rg_index not in row_groups:
                continue
            if columns and not (column in columns
                                or column.rsplit('.', 1)[-1] in columns):
                continue

            fil.seek(start)
            chunk = fil.read(length)

            pos = 0
            while pos < len(chunk):
                header, body = _read_struct(chunk, pos)
                comp_size = header[_PH_COMPRESSED_SIZE]
                kind = _PAGE_KINDS.get(header.get(_PH_TYPE, 0), 'unknown')
                data = chunk[body:body + comp_size]
                pos = body + comp_size

                if kind == 'dict' and not include_dict:
                    continue

                if kind == 'data_v2':
                    v2 = header.get(_PH_DATA_PAGE_V2, {})
                    if not v2.get(_V2_IS_COMPRESSED, True):
                        continue
                    # The level bytes at the front of a v2 page are stored
                    # uncompressed; only what follows them is a Snappy block.
                    data = data[v2.get(_V2_DEF_LEVEL_BYTES, 0)
                                + v2.get(_V2_REP_LEVEL_BYTES, 0):]

                yield Page(rg_index, column, kind,
                           header[_PH_UNCOMPRESSED_SIZE], len(data),
                           start + body, data)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('--list', action='store_true',
                    help='listing is the default; accepted for clarity')
    ap.add_argument('--column', action='append', default=None)
    ap.add_argument('--row-group', dest='row_group', action='append',
                    type=int, default=None)
    ap.add_argument('--no-dict', dest='include_dict', action='store_false')
    ap.add_argument('--max-block', dest='max_block', type=int, default=65536)
    args = ap.parse_args()

    row_groups = set(args.row_group) if args.row_group else None

    print('%3s %-24s %-8s %10s %10s  %s'
          % ('rg', 'column', 'kind', 'compressed', 'expanded', 'usable'))

    total = usable = oversize = 0
    bad = {}
    for page in iter_pages(args.file, args.column, row_groups,
                           args.include_dict):
        total += 1
        reason = check_raw(page.data)
        if reason is None and args.max_block and \
                page.uncompressed_size > args.max_block:
            reason = 'over %d bytes' % args.max_block
            oversize += 1
        elif reason is not None:
            bad[reason] = bad.get(reason, 0) + 1
        if reason is None:
            usable += 1
        print('%3d %-24s %-8s %10d %10d  %s'
              % (page.row_group, page.column[:24], page.kind,
                 page.compressed_size, page.uncompressed_size,
                 'yes' if reason is None else reason))

    print()
    print('%d page(s): %d usable, %d over the %d-byte limit, %d incompatible'
          % (total, usable, oversize, args.max_block, sum(bad.values())))
    for reason, count in sorted(bad.items()):
        print('  %d x %s' % (count, reason))
    if not total:
        print('No Snappy-compressed pages found -- is the file compressed '
              'with something else?')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
