#!/usr/bin/env python3
"""
The packing model: how many bytes per cycle the real scored pages allow a
decoder of a given shape, before anyone builds it.

Why it exists. The loop stalled for 18 iterations on a design whose decoder
already issued one transfer every cycle. Widening one dimension at a time
(elements per cycle K, output line bytes L, or compressed input bytes per
cycle N) measured about nothing each time, because another of the three took
over as the limit; replaying the real pages offline showed that K4/L32/N32
together allows about twice the bytes per cycle. Nothing in the loop could
see that. This tool lets a session or the planner ask before building.

What it models. Every Snappy element (literal or copy) of every page of the
seven scored row groups (the same whole row groups the draws feed) is replayed
through an ideal packer. Per cycle it takes up to K elements of any kind, in
order, while their headers (and a literal's payload) sit in a 2N-byte input
window that advances N bytes per cycle, and while their bytes fit one L-byte
output line. Switches:

  --hazard window    a copy after the first may not read bytes written in the
                     same cycle (the default; what a real datapath needs)
           none      no hazard rule (an upper bound)
           strict16  window, and a copy with an offset under 16 never shares
                     a cycle with another copy (the rule the i35 decoder has)
  --litrate B        bytes per cycle a long literal streams at (default: the
                     original rule, min(N, L) past the line and N past the
                     input window)
  --split off        a copy longer than the line goes alone for ceil(len/L)
                     cycles (the default)
          on         a copy cut by the line keeps its remainder at the head,
                     and the remainder shares the next cycle with what follows
  --rules ideal      the ideal packer above (the default)
          dsw4       the exact cycle model of the DSW-4 design (decoupled
                     parser, writer and input) at K slots, an L-byte line and
                     N input bytes; the switches above do not apply
          i35        the exact rule model of the i35 decoder (K, L and N
                     ignored); it exists to calibrate against

Calibration. A model is not the design: the ideal packer at i35's own widths
reads about 8% under what i35 measures. So every prediction is scaled per
table by measured / model of the current best design, where "model" is the
model of that design's own family (the i35 or DSW-4 rule model when the
design is one of those, otherwise the ideal packer at its own widths). The
prediction for a what-if is ratio x model(what-if), and the score is the
geomean over the seven scored tables, like the loop's. The loop records the
calibration per best commit; numbers made under different calibrations are
never compared.

Who sees what. The held-out tables are scored and never shown to a session.
This tool prints the visible table per table and the held-out tables only as
one geomean; it never prints a held-out table's name, a held-out number, or
page bytes. Its cache (parsed pages and per-table model results) lives in the
loop checkout's .agentic/packmodel/, outside every worktree, where a session's
file tools cannot reach. A session never parses Parquet: the loop parses the
pages once per corpus, and a session's run finds them through a pointer file
the loop writes (current-<run>.json). Any failure prints one fixed line.

Usage:
    python agentic/packmodel.py                       current widths and the standard what-ifs
    python agentic/packmodel.py --k 4 --l 32 --n 32   one what-if
    python agentic/packmodel.py --k 4 --l 32 --n 32 --rules dsw4
    python agentic/packmodel.py --selfcheck           (not for sessions) reproduce the diag
                                                      numbers, time a run, measure memory
Loop only (the session gate refuses these):
    --prepare        parse the pages if this corpus has none yet, then print as with no arguments
    --extra SPEC     also evaluate "K4/L32/N32[ rules=dsw4]" (repeatable), with --json PATH

A new setting takes a few minutes (multiprocessing, packmodel_jobs workers,
one table per task); a repeated one is read from the cache at once.
"""

import argparse
import hashlib
import json
import math
import os
import pickle
import re
import sys
import time

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import stim                    # noqa: E402  (table names; Parquet only in loop mode)
from tools import CONFIG       # noqa: E402

# Bump when a model changes, so cached raw results of the old one are ignored.
MODEL_VERSION = 1

# The scored tables, in the loop's order. Only the train tables are shown per
# table; the rest only as one geomean.
VISIBLE = tuple(stim.TRAIN_TABLES)
HELD = tuple(stim.HELD_OUT_TABLES)
TABLES = VISIBLE + HELD

RULES = ('ideal', 'dsw4', 'i35')
HAZARDS = ('window', 'none', 'strict16')
SPLITS = ('off', 'on')
K_RANGE = (1, 16)
L_SET = (8, 16, 32, 64, 128)
N_SET = (8, 16, 32, 64)
LITRATE_RANGE = (1, 128)

# Fixed lines: a failure must never print a path, a table name or a number.
FIXED_ERROR = 'packing model unavailable (internal error)'
NOT_PREPARED = ('packing model unavailable (the loop has not prepared it for '
                'this run yet)')
BUSY = ('packing model busy: another run is computing; try again in a few '
        'minutes')

# The lock that serialises computing: stale after 30 minutes (a killed run),
# and a second caller waits this long before giving up with BUSY.
LOCK_STALE_S = 1800
LOCK_WAIT_S = 600


class Unavailable(Exception):
    """The model cannot answer without data a session may not touch."""


class NotPrepared(Exception):
    """No pointer file: the loop has not run write_calibration for this run."""


class Busy(Exception):
    """Another process held the compute lock for the whole wait."""


# ---------------------------------------------------------------------------
# Parsing (ported unchanged from diag/model.py)

def parse(c):
    """Elements of one Snappy chunk as (pos, kind, hdr, length, offset).

    kind is 'L' (literal) or 'C' (copy); pos is the header's byte offset in
    the chunk, hdr the header length, so a literal's payload is
    c[pos+hdr : pos+hdr+length].
    """
    i = 0
    while True:
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


# ---------------------------------------------------------------------------
# The models. Each takes chunks = [(elements, chunk_bytes)] and returns
# (output bytes, cycles).

def ideal(chunks, K, L, N, hazard='window', litrate=None, split='off'):
    """The ideal packer (ported from diag/ideal_par.ideal, plus three switches).

    With the default switches it is ideal_par.ideal exactly, which is what the
    diag numbers were made with.
    """
    LINE, IN = L, N
    # The original streams a long literal at min(IN, LINE) past the line and
    # at IN past the input window; --litrate replaces both.
    lit_line = litrate or min(IN, LINE)
    lit_win = litrate or IN
    cut_on = split == 'on'
    cyc = 0
    outb = 0
    for els, _size in chunks:
        k = 0
        base = 0
        cyc += 1
        n = len(els)
        cut_k, cut_rem = -1, 0          # a copy cut by the line (split on)
        while k < n:
            p = els[k][0]
            if p >= base + 2 * IN:
                cyc += 1
                base += IN
                continue
            win_end = base + 2 * IN
            wrote = 0
            took = 0
            short_copy = False          # strict16: a copy with offset < 16 is in
            cyc += 1
            while k < n and took < K:
                p, kind, h, Ln, off = els[k]
                if p + h > win_end:
                    break
                if kind == 'L':
                    if took == 0 and Ln > LINE:
                        cyc += math.ceil(max(0, Ln - LINE) / lit_line)
                        outb += Ln
                        k += 1
                        base = max(base, ((p + h + Ln) // IN) * IN - IN)
                        break
                    if wrote + Ln > LINE:
                        break
                    if p + h + Ln > win_end:
                        if took > 0:
                            break
                        # first element: its payload streams in past the window
                        cyc += math.ceil((p + h + Ln - win_end) / lit_win)
                        base = max(base, ((p + h + Ln) // IN) * IN - IN)
                else:
                    if took > 0 and hazard == 'strict16' and (off < 16 or short_copy):
                        break
                    if cut_on:
                        rem = cut_rem if k == cut_k else Ln
                        nb = min(rem, LINE - wrote)
                        if took > 0 and hazard != 'none':
                            # may read only bytes from before this cycle
                            nb = min(nb, off - wrote)
                        if nb <= 0:
                            break
                        wrote += nb
                        outb += nb
                        took += 1
                        short_copy = short_copy or off < 16
                        if nb < rem:
                            cut_k, cut_rem = k, rem - nb
                            break
                        k += 1
                        continue
                    if took == 0 and Ln > LINE:
                        cyc += math.ceil(Ln / LINE) - 1
                        outb += Ln
                        k += 1
                        break
                    if wrote + Ln > LINE:
                        break
                    if took > 0 and hazard != 'none' and off < wrote + Ln:
                        break
                    short_copy = short_copy or off < 16
                wrote += Ln
                outb += Ln
                took += 1
                k += 1
            if k < n:
                base += min(IN, max(0, (els[k][0] // IN) * IN - base))
    return outb, cyc


def i35(chunks):
    """The i35 decoder's packing rules (ported unchanged from diag/model.run).

    One transfer per cycle: a pair of copies, a copy and a literal, or one
    element, inside an 8-byte line step with a 16-byte header window.
    """
    cycles = 0
    outb = 0
    for els, size in chunks:
        k = 0
        line = 0
        cycles += 1                     # chunk-start dead cycle
        while k < len(els):
            pos = els[k][0]
            if pos > line * 8 + 7:      # inside literal payload: skip a line
                cycles += 1
                line += 1
                continue
            o = pos - line * 8
            last_line = (line + 1) * 8 >= size
            wendi = (size - 1 - line * 8) if last_line else min(15, size - 1 - line * 8)
            dwen = wendi - o
            p, kind, h, L, off = els[k]
            e2 = els[k + 1] if k + 1 < len(els) else None
            cycles += 1
            if kind == 'C':
                why = None
                if e2 is None:
                    why = 'end'
                elif h == 5:
                    why = 'first has 5-byte header'
                elif e2[1] == 'L':
                    why = 'next is literal'
                elif e2[2] == 5:
                    why = 'next has 5-byte header'
                elif off < 16:
                    why = 'first offset <16'
                elif e2[4] < 16:
                    why = 'second offset <16'
                elif L + e2[3] > 16:
                    why = 'lengths >16'
                elif h + 3 > dwen:
                    why = 'header outside window'
                if why is None:
                    outb += L + e2[3]
                    newpos = e2[0] + e2[2]
                    k += 2
                else:
                    if e2 is not None and e2[1] == 'L' and h <= dwen and e2[2] == 1:
                        outb += L + e2[3]
                        newpos = e2[0] + 1 + e2[3]
                        k += 2
                    else:
                        outb += L
                        newpos = p + h
                        k += 1
            else:
                e2ok = (e2 is not None and h == 1 and L <= 8 and e2[1] == 'C'
                        and e2[2] in (2, 3) and e2[4] >= 16 and L + e2[3] <= 16
                        and (L - 1) <= dwen - 4)
                if e2ok:
                    outb += L + e2[3]
                    newpos = e2[0] + e2[2]
                    k += 2
                else:
                    outb += L
                    newpos = p + h + L
                    k += 1
            if newpos > line * 8 + 7:
                line += 1
    return outb, cycles


# The DSW-4 specification's final design point (diag/work-spec/smodel.DEFAULT);
# --rules dsw4 overrides only K, B (the line) and N.
DSW4_DEFAULT = dict(K=4, B=32, N=32, BUF=896, BLK=16, PK=4, Q=48, LITP=2,
                    REP=1, DBL=1, LAG=0, JLAT=5, JFAR=8, PEFAR=3, CH=16, INF=0,
                    PLAT=7, LITHW=1, NOHAZ=0, CAP2=0, LWLAT=10, DBLLAG=1, CUTK=1)


def _dsw4_chunk(els, size, P):
    """One chunk through the DSW-4 cycle model (ported from
    diag/work-spec/smodel.run_chunk, without its verification and stats)."""
    K, B, N, BUF, BLK, PK, Q, LITP = (P[k] for k in ('K', 'B', 'N', 'BUF', 'BLK', 'PK', 'Q', 'LITP'))
    REP, DBL, LAG, JLAT, INF = P['REP'], P['DBL'], P['LAG'], P['JLAT'], P['INF']
    PLAT, LITHW = P['PLAT'], P['LITHW']
    pkd = [0] * PLAT
    Ihist = [0, 0]
    LWL = P['LWLAT']
    n = len(els)
    cyc = P['CH']
    out = 0
    I = 0            # compressed bytes arrived (end of this cycle)   # noqa: E741
    pk = 0           # parser: next element index to parse
    vis = 0          # elements visible to the writer this cycle
    wb = (els[0][0] // BLK) * BLK if n else 0
    pbusy = 0
    wk = 0           # writer: head element index
    hrem = els[0][3] if n else 0
    hq = els[0][4] if n else 0
    W = 0
    wl = [0] * (LAG + 1)
    if INF:
        pk = vis = n
        I = size     # noqa: E741
    lwq = [els[0][0] if n else 0] * LWL
    while wk < n:
        cyc += 1
        I_prev = I
        Ihist = [Ihist[1], I]
        # input
        if not INF:
            p, kind, h, L, off = els[wk]
            low = (p + h + (L - hrem)) if kind == 'L' else p
            lwq.append(low)
            low = lwq.pop(0)
            I = min(size, I + N, low + BUF)    # noqa: E741
        # parser
        if not INF and cyc >= pbusy and pk < n:
            need = min(size, wb + 2 * BLK + 4)
            if I_prev >= need:
                e = 0
                while e < PK and pk < n and pk - wk < Q and els[pk][0] < wb + 2 * BLK:
                    pk += 1
                    e += 1
                np_ = els[pk][0] if pk < n else size
                if np_ >= wb + BLK:
                    if np_ < wb + 2 * BLK:
                        wb += BLK
                    else:
                        nb = np_ // BLK * BLK
                        steps = (nb - wb) // BLK
                        wb = nb
                        xp, xk, xh, xL, xo = els[pk - 1]
                        if xk == 'L' and xh > 1:
                            pbusy = cyc + min(P['PEFAR'] + steps, P['JFAR'])
                        else:
                            pbusy = cyc + min(steps, JLAT)
        # writer (sees elements parsed in earlier cycles)
        lagbytes = W - wl[0] if LAG else 0
        d = 0
        seg = 0
        nlit = 0
        j = wk
        while True:
            if seg >= K or d >= B or j >= vis:
                break
            p, kind, h, L, off = els[j]
            rem = hrem
            if kind == 'L':
                if nlit >= LITP:
                    break
                if LITHW:
                    avail = Ihist[0] - (p + h + (L - rem))
                    nb = min(rem, B - d)
                    if seg == 0:
                        nb = min(nb, avail)
                    elif avail < nb:
                        nb = 0
                else:
                    avail = I_prev - (p + h + (L - rem))
                    nb = min(rem, B - d, avail)
                if nb <= 0:
                    break
                nlit += 1
            else:
                q = hq
                if REP and d == 0 and lagbytes == 0 and q < 32 and (q & (q - 1)) == 0:
                    nb = min(rem, B)
                else:
                    lim = q - d - lagbytes
                    if P['NOHAZ']:
                        lim = 1 << 40
                    if lim <= 0:
                        break
                    nb = min(rem, B - d, lim)
            if not P['CUTK'] and seg > 0 and nb < rem:
                break
            if P['CAP2'] and seg >= 2 and nb > P['CAP2']:
                nb = P['CAP2']
            d += nb
            seg += 1
            if nb < rem:
                hrem = rem - nb
                if kind == 'C' and DBL:
                    produced = (L - rem) if P['DBLLAG'] else (L - hrem)
                    while q < 32 and 2 * q <= produced + off:
                        q *= 2
                    hq = q
                break
            j += 1
            wk = j
            if j < n:
                hrem = els[j][3]
                hq = els[j][4]
        if LAG:
            wl = wl[1:] + [W]
        W += d
        out += d
        if INF:
            vis = pk
        else:
            pkd = pkd[1:] + [pk]
            vis = pkd[0]
    return cyc, out


def dsw4(chunks, K, B, N):
    P = dict(DSW4_DEFAULT, K=K, B=B, N=N)
    cyc = 0
    out = 0
    for els, size in chunks:
        c, o = _dsw4_chunk(els, size, P)
        cyc += c
        out += o
    return out, cyc


# ---------------------------------------------------------------------------
# Configurations

def make_cfg(rules='ideal', K=None, L=None, N=None, hazard='window',
             litrate=None, split='off'):
    """A normalised what-if: fields a model ignores are cleared, so two
    spellings of the same question share one cache entry."""
    if rules not in RULES:
        raise ValueError('rules')
    if rules == 'i35':
        return {'rules': 'i35', 'K': None, 'L': None, 'N': None,
                'hazard': None, 'litrate': None, 'split': None}
    cfg = {'rules': rules, 'K': int(K), 'L': int(L), 'N': int(N),
           'hazard': None, 'litrate': None, 'split': None}
    if rules == 'ideal':
        if hazard not in HAZARDS or split not in SPLITS:
            raise ValueError('switch')
        cfg.update(hazard=hazard, split=split,
                   litrate=int(litrate) if litrate else None)
    return cfg


def cfg_id(cfg):
    if cfg['rules'] == 'i35':
        return 'v%d|i35' % MODEL_VERSION
    if cfg['rules'] == 'dsw4':
        return 'v%d|dsw4|K%d|L%d|N%d' % (MODEL_VERSION, cfg['K'], cfg['L'], cfg['N'])
    return 'v%d|ideal|K%d|L%d|N%d|%s|%s|%s' % (
        MODEL_VERSION, cfg['K'], cfg['L'], cfg['N'], cfg['hazard'],
        cfg['litrate'] or '-', cfg['split'])


def cfg_label(cfg):
    if cfg['rules'] == 'i35':
        return 'rules=i35 (the i35 decoder itself; K, L and N do not apply)'
    if cfg['rules'] == 'dsw4':
        return 'K%d L%d N%d rules=dsw4' % (cfg['K'], cfg['L'], cfg['N'])
    text = 'K%d L%d N%d hazard=%s split=%s' % (cfg['K'], cfg['L'], cfg['N'],
                                                cfg['hazard'], cfg['split'])
    if cfg['litrate']:
        text += ' litrate=%d' % cfg['litrate']
    return text + ' rules=ideal'


def run_model(chunks, cfg):
    """Raw modelled bytes/cycle of one table."""
    if cfg['rules'] == 'i35':
        outb, cyc = i35(chunks)
    elif cfg['rules'] == 'dsw4':
        outb, cyc = dsw4(chunks, cfg['K'], cfg['L'], cfg['N'])
    else:
        outb, cyc = ideal(chunks, cfg['K'], cfg['L'], cfg['N'], cfg['hazard'],
                          cfg['litrate'], cfg['split'])
    return float(outb) / cyc if cyc else 0.0


TAG_RE = re.compile(r'^\s*K(\d{1,2})\s*/\s*[LB](\d{1,3})\s*/\s*N(\d{1,2})'
                    r'(?:\s+rules=(ideal|dsw4))?\s*$', re.I)


def parse_spec(text):
    """'K4/L32/N32' or 'K4/L32/N32 rules=dsw4' -> cfg (roadmap tags, --extra)."""
    m = TAG_RE.match(text or '')
    if not m:
        raise ValueError('not a K/L/N spec')
    K, L, N = int(m.group(1)), int(m.group(2)), int(m.group(3))
    check_limits(K, L, N, None)
    return make_cfg((m.group(4) or 'ideal').lower(), K, L, N)


def check_limits(K, L, N, litrate):
    """The argument limits; a ValueError names the argument, never data."""
    if K is not None and not K_RANGE[0] <= K <= K_RANGE[1]:
        raise ValueError('--k must be %d to %d' % K_RANGE)
    if L is not None and L not in L_SET:
        raise ValueError('--l must be one of %s' % ', '.join(map(str, L_SET)))
    if N is not None and N not in N_SET:
        raise ValueError('--n must be one of %s' % ', '.join(map(str, N_SET)))
    if litrate is not None and not LITRATE_RANGE[0] <= litrate <= LITRATE_RANGE[1]:
        raise ValueError('--litrate must be %d to %d' % LITRATE_RANGE)


# ---------------------------------------------------------------------------
# The cache: never inside a worktree

def loop_root(root=ROOT):
    """The checkout the loop runs from, seen from this copy of the kit.

    The loop's base and candidate worktrees all live under
    <loop checkout>/.agentic/runs/..., so the loop checkout is the part of
    the path before the first '.agentic' component; a kit outside .agentic
    is the loop checkout itself. This is decided by the path, not by git:
    git's common dir names whichever checkout first created the repository,
    and the kit's own checkout can itself be a worktree of another one (it
    is here), which would put the cache in a different checkout.
    """
    parts = os.path.normpath(os.path.abspath(root)).split(os.sep)
    if '.agentic' in parts:
        return os.sep.join(parts[:parts.index('.agentic')]) or os.sep
    return os.path.normpath(os.path.abspath(root))


def cache_dir():
    """$AGENTIC_PACKMODEL_DIR, else <loop checkout>/.agentic/packmodel.

    The same folder from the loop's checkout, the base worktree and every
    candidate worktree, so they share one cache, and it is inside none of
    the worktrees (a session's file tools cannot reach it).
    """
    env = os.environ.get('AGENTIC_PACKMODEL_DIR')
    if env:
        return env
    return os.path.join(loop_root(), '.agentic', 'packmodel')


def _replace(tmp, path):
    # Windows refuses a replace while another process has the target open
    # for reading; that lasts milliseconds, so retry briefly.
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def write_json_atomic(path, data):
    """Temp file in the same folder, then replace: a reader sees the old file
    or the new one, never half of one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '%s.%d.%d.tmp' % (path, os.getpid(), int(time.time() * 1000) % 100000)
    with open(tmp, 'w', encoding='utf-8') as fil:
        json.dump(data, fil, indent=1, sort_keys=True)
    _replace(tmp, path)


def read_json_safe(path):
    """A missing, truncated or corrupt file reads as empty and is rebuilt."""
    try:
        with open(path, encoding='utf-8') as fil:
            data = json.load(fil)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge_raw(path, entries):
    """Add entries to a raw-results file, keeping what another writer added
    since it was read."""
    data = read_json_safe(path)
    data.update(entries)
    write_json_atomic(path, data)
    return data


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fil:
        for block in iter(lambda: fil.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def page_key():
    """Names the corpus: a rebuilt data file never reuses stale pages. Only
    the loop computes it (a session has no data files)."""
    digest = hashlib.sha256()
    for table in TABLES:
        digest.update(('%s %s %d\n' % (table, _file_sha(stim.table_path(table)),
                                       stim.ROW_GROUP)).encode('ascii'))
    return digest.hexdigest()[:12]


def _pages_dir(key):
    return os.path.join(cache_dir(), 'pages-%s' % key)


def _raw_path(key):
    return os.path.join(cache_dir(), 'raw-%s.json' % key)


def _run_name(run):
    if not run or not re.match(r'^[A-Za-z0-9_.-]{1,80}$', run):
        raise ValueError('run name')
    return run


def _commit_name(commit):
    if not commit or not re.match(r'^[0-9a-fA-F]{7,64}$', str(commit)):
        raise ValueError('commit')
    return str(commit)


class _Lock(object):
    """One packmodel computes at a time (each would otherwise load the same
    tables and compute the same entries). Held only while computing."""

    def __init__(self, path, wait_s=None):
        self.path = path
        self.wait_s = LOCK_WAIT_S if wait_s is None else wait_s

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + self.wait_s
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w') as fil:
                    fil.write('%d %d\n' % (os.getpid(), int(time.time())))
                return self
            except OSError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE_S:
                        os.remove(self.path)      # its owner was killed
                        continue
                except OSError:
                    continue                      # it went away: try again
            if time.time() >= deadline:
                raise Busy()
            time.sleep(2.0)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# Workers (module level so a spawned process can import them)

_WORKER = {'path': None, 'chunks': None}


def _peak_mb():
    """Peak memory of this process in MB (Windows working set, else maxrss)."""
    try:
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t),
                            ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t),
                            ('PeakPagefileUsage', ctypes.c_size_t)]
            cnt = Counters()
            cnt.cb = ctypes.sizeof(Counters)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                                   wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(cnt), cnt.cb):
                return cnt.PeakWorkingSetSize / 1048576.0
            return 0.0
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def _load_table(path):
    # One table per worker at a time: the whole corpus in one process took
    # 1.43 GB; one table bounds a worker to that table.
    if _WORKER['path'] != path:
        _WORKER['path'], _WORKER['chunks'] = None, None
        with open(path, 'rb') as fil:
            _WORKER['chunks'] = pickle.load(fil)
        _WORKER['path'] = path
    return _WORKER['chunks']


def _model_task(args):
    """(pages file, table, [cfgs]) -> (table, {cfg id: bpc}, peak MB)."""
    path, table, cfgs = args
    chunks = _load_table(path)
    return table, dict((cfg_id(c), run_model(chunks, c)) for c in cfgs), _peak_mb()


def _parse_task(args):
    """(table, out path) -> (table, peak MB). Loop mode only: reads Parquet."""
    table, out_path = args
    chunks = stim.page_chunks(table)[0]
    data = [(parse(c), len(c)) for c in chunks]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = '%s.%d.tmp' % (out_path, os.getpid())
    with open(tmp, 'wb') as fil:
        pickle.dump(data, fil, protocol=pickle.HIGHEST_PROTOCOL)
    _replace(tmp, out_path)
    return table, _peak_mb()


def _jobs():
    try:
        return max(1, int(CONFIG.get('packmodel_jobs', 3)))
    except (TypeError, ValueError):
        return 3


def _map(func, tasks, jobs, stats):
    """Run tasks in a pool of `jobs` workers (in-process for one task), and
    keep the largest worker peak memory in stats."""
    if not tasks:
        return []
    jobs = min(jobs, len(tasks))
    if jobs <= 1:
        out = [func(t) for t in tasks]
    else:
        from multiprocessing import Pool
        with Pool(jobs) as pool:
            out = list(pool.imap_unordered(func, tasks, chunksize=1))
    for res in out:
        stats['worker_peak_mb'] = max(stats.get('worker_peak_mb', 0.0), res[-1])
    return out


def ensure_pages(key, tables=TABLES, allow_parse=False, jobs=None, stats=None):
    """Make sure the parsed pages exist. Only the loop and --selfcheck may
    parse Parquet; a session gets Unavailable instead."""
    stats = stats if stats is not None else {}
    pdir = _pages_dir(key)
    missing = [t for t in tables if not os.path.exists(os.path.join(pdir, '%s.pkl' % t))]
    if not missing:
        return
    if not allow_parse:
        raise Unavailable()
    t0 = time.time()
    # Biggest first, so the longest parse starts at once.
    order = sorted(missing, key=lambda t: -os.path.getsize(stim.table_path(t)))
    _map(_parse_task, [(t, os.path.join(pdir, '%s.pkl' % t)) for t in order],
         jobs or _jobs(), stats)
    stats['parse_s'] = round(stats.get('parse_s', 0.0) + time.time() - t0, 1)


def ensure_results(key, cfgs, allow_parse=False, jobs=None, stats=None):
    """Raw bytes/cycle for every (cfg, table); computes what is missing.
    Returns {cfg id: {table: bpc}}."""
    stats = stats if stats is not None else {}
    want = list(dict((cfg_id(c), c) for c in cfgs).values())
    raw_path = _raw_path(key)

    def missing_of(raw):
        return [(t, c) for c in want for t in TABLES
                if '%s|%s' % (cfg_id(c), t) not in raw]

    raw = read_json_safe(raw_path)
    if missing_of(raw):
        with _Lock(os.path.join(cache_dir(), 'lock')):
            raw = read_json_safe(raw_path)       # another run may have done it
            todo = missing_of(raw)
            if todo:
                ensure_pages(key, sorted(set(t for t, _c in todo)), allow_parse,
                             jobs, stats)
                t0 = time.time()
                pdir = _pages_dir(key)
                by_table = {}
                for t, c in todo:
                    by_table.setdefault(t, []).append(c)
                tasks = []
                # Biggest table first; a big table's settings are split over
                # the workers, a small table's go to one worker in one load.
                for t in sorted(by_table, key=lambda x: -os.path.getsize(
                        os.path.join(pdir, '%s.pkl' % x))):
                    cs = by_table[t]
                    size = os.path.getsize(os.path.join(pdir, '%s.pkl' % t))
                    per = 1 if size > (8 << 20) else len(cs)
                    for i in range(0, len(cs), per):
                        tasks.append((os.path.join(pdir, '%s.pkl' % t), t, cs[i:i + per]))
                found = {}
                for table, res, _peak in _map(_model_task, tasks, jobs or _jobs(), stats):
                    for cid, bpc in res.items():
                        found['%s|%s' % (cid, table)] = bpc
                raw = merge_raw(raw_path, found)
                stats['model_s'] = round(stats.get('model_s', 0.0) + time.time() - t0, 1)
    return dict((cfg_id(c), dict((t, raw['%s|%s' % (cfg_id(c), t)]) for t in TABLES))
                for c in want)


# ---------------------------------------------------------------------------
# Calibration

def _geomean(values):
    values = [float(v) for v in values]
    if not values or min(values) <= 0:
        return 0.0
    return math.exp(sum(math.log(v) for v in values) / len(values))


def _family(rtl_dir):
    """'dsw4', 'i35' or None, read from the design's packages."""
    if not rtl_dir or not os.path.isdir(rtl_dir):
        return None, {}
    texts = []
    for name in sorted(os.listdir(rtl_dir)):
        if name.endswith('.vhd'):
            try:
                with open(os.path.join(rtl_dir, name), encoding='utf-8',
                          errors='replace') as fil:
                    texts.append((name, fil.read()))
            except OSError:
                pass
    consts = {}
    for name, text in texts:
        if name.endswith('_pkg.vhd'):
            for cname in ('K_SLOTS', 'LINE_B'):
                m = re.search(r'constant\s+%s\s*:\s*(?:natural|integer|positive)'
                              r'\s*:=\s*(\d+)\s*;' % cname, text, re.I)
                if m:
                    consts[cname] = int(m.group(1))
    if 'K_SLOTS' in consts and 'LINE_B' in consts:
        return 'dsw4', consts
    if any(re.search(r'\bcp2_val\b', text) for name, text in texts
           if name.endswith('_pkg.vhd')):
        return 'i35', consts
    return None, consts


def design_mapping(commit, widths, rtl_dir=None, override=None):
    """Which model describes the best design, at which widths.

    Returns {'rules', 'K', 'L', 'N', 'source'}; rules None means
    uncalibrated. In order:
      1. a packmodel_widths override, only if it names this very commit, so a
         hand setting never carries over to the next adopted design;
      2. the DSW-4 family (its package declares K_SLOTS and LINE_B): the
         exact DSW-4 model at those widths and the read input width;
      3. the i35 family (a package with cp2_val, and K, L and N read, not
         defaulted): the exact i35 model; K = copy slots, L = core line, N =
         input bytes, the convention behind the diag numbers;
      4. elements per transfer, core line and input bytes all read:
         packmodel_calib_rules (default the ideal packer) at those widths;
      5. otherwise uncalibrated: raw model numbers, labelled as such.
    widths come from analyse.rtl_widths; its 'defaulted' list names the
    values it did not read from the RTL (records from before the list
    existed have none, and count as read).
    """
    widths = widths or {}
    defaulted = set(widths.get('defaulted') or [])

    def read(key):
        val = widths.get(key)
        if key in defaulted or val is None:
            return None
        try:
            return int(round(float(val)))
        except (TypeError, ValueError):
            return None

    if override and isinstance(override, dict) and commit \
            and str(override.get('commit', '')) == str(commit):
        try:
            rules = str(override.get('rules') or 'ideal')
            cfg = make_cfg(rules, override.get('K') or 1, override.get('L') or 8,
                           override.get('N') or 8)
            return {'rules': rules, 'K': cfg['K'], 'L': cfg['L'], 'N': cfg['N'],
                    'source': 'packmodel_widths override'}
        except (TypeError, ValueError):
            pass
    family, consts = _family(rtl_dir)
    N = read('in_bytes')
    if family == 'dsw4' and N:
        return {'rules': 'dsw4', 'K': consts['K_SLOTS'], 'L': consts['LINE_B'],
                'N': N, 'source': 'DSW-4 family'}
    K, L = read('copy_slots'), read('core_line_bytes')
    if family == 'i35' and K and L and N:
        return {'rules': 'i35', 'K': K, 'L': L, 'N': N, 'source': 'i35 family'}
    E = read('elements_per_transfer')
    if E and L and N:
        rules = CONFIG.get('packmodel_calib_rules') or 'ideal'
        if rules not in ('ideal', 'dsw4'):
            rules = 'ideal'
        return {'rules': rules, 'K': E, 'L': L, 'N': N,
                'source': 'elements per transfer read from the RTL'}
    return {'rules': None, 'K': K or E, 'L': L, 'N': N,
            'source': 'widths not read from the RTL'}


def calib_cfg(calib):
    """The model a calibration divides by, or None (uncalibrated)."""
    rules = (calib or {}).get('rules')
    if not rules or len((calib or {}).get('measured') or {}) < len(TABLES):
        return None
    if rules == 'i35':
        return make_cfg('i35')
    return make_cfg(rules, calib['K'], calib['L'], calib['N'])


def measured_from_metrics(metrics):
    """{table: bytes/cycle} of the scored real draws."""
    out = {}
    for draw in (metrics or {}).get('draws') or []:
        name = draw.get('name') or ''
        table = name.split('-', 1)[1] if '-' in name else name
        if table in TABLES and draw.get('scored') and draw.get('bytes_per_cycle'):
            out[table] = float(draw['bytes_per_cycle'])
    return out


def _calib_path(commit):
    return os.path.join(cache_dir(), 'calib-%s.json' % _commit_name(commit))


def _pointer_path(run):
    return os.path.join(cache_dir(), 'current-%s.json' % _run_name(run))


def write_pointer(run, key, commit):
    """Where a session's packmodel finds the pages and the calibration."""
    write_json_atomic(_pointer_path(run), {'run': run, 'key': key,
                                           'commit': _commit_name(commit)})


def write_calibration(run, best_metrics, commit, widths, rtl_dir=None, key=None):
    """Record the best design's calibration and point the run at it.

    Loop only (it hashes the data files). Returns {'calib', 'previous',
    'changed'}: previous is the calibration the run pointed at before, so the
    loop can log a change of rules or ratio instead of hiding it.
    """
    mapping = design_mapping(commit, widths, rtl_dir, CONFIG.get('packmodel_widths'))
    measured = measured_from_metrics(best_metrics)
    calib = dict(mapping, commit=_commit_name(commit), measured=measured,
                 best_bpc=round(_geomean(measured.values()), 4) if len(measured) == len(TABLES)
                 else (best_metrics or {}).get('bytes_per_cycle'))
    if len(measured) < len(TABLES):
        calib['rules'] = None
        calib['source'] = 'not every scored table measured'
    key = key or page_key()
    old_ptr = read_json_safe(_pointer_path(run))
    previous = read_json_safe(_calib_path(old_ptr['commit'])) if old_ptr.get('commit') else {}
    write_json_atomic(_calib_path(commit), calib)
    write_pointer(run, key, commit)
    changed = bool(previous) and (
        previous.get('rules'), previous.get('K'), previous.get('L'), previous.get('N')) != (
        calib.get('rules'), calib.get('K'), calib.get('L'), calib.get('N'))
    return {'calib': calib, 'previous': previous or None, 'changed': changed}


def _context(run=None, calib_commit=None):
    """(key, calib) for this run, from the pointer: never from data files."""
    run = run or os.environ.get('AGENTIC_RUN')
    if run:
        ptr = read_json_safe(_pointer_path(run))
    else:
        # No run named: the newest pointer. The header names the commit it
        # calibrated to, so a mismatch is visible.
        ptrs = []
        cdir = cache_dir()
        if os.path.isdir(cdir):
            for name in os.listdir(cdir):
                if name.startswith('current-') and name.endswith('.json'):
                    path = os.path.join(cdir, name)
                    ptrs.append((os.path.getmtime(path), path))
        ptr = read_json_safe(max(ptrs)[1]) if ptrs else {}
    if not ptr.get('key'):
        raise NotPrepared()
    commit = calib_commit or os.environ.get('AGENTIC_CALIB') or ptr.get('commit')
    calib = read_json_safe(_calib_path(commit)) if commit else {}
    calib.setdefault('commit', commit)
    return ptr['key'], calib


def evaluate(key, calib, cfgs, allow_parse=False, jobs=None, stats=None):
    """Calibrated predictions per cfg: {cfg id: {'tables': {t: bpc},
    'visible', 'held', 'all', 'calibrated'}}. Per-table numbers stay inside;
    only format_block and the aggregate fields leave this module."""
    denom = calib_cfg(calib)
    need = list(cfgs) + ([denom] if denom else [])
    raw = ensure_results(key, need, allow_parse, jobs, stats)
    ratios = None
    if denom:
        d = raw[cfg_id(denom)]
        ratios = dict((t, calib['measured'][t] / d[t]) for t in TABLES if d[t] > 0)
        if len(ratios) < len(TABLES):
            ratios = None
    out = {}
    for cfg in cfgs:
        r = raw[cfg_id(cfg)]
        tables = dict((t, r[t] * (ratios[t] if ratios else 1.0)) for t in TABLES)
        out[cfg_id(cfg)] = {
            'tables': tables, 'calibrated': bool(ratios),
            'visible': [(t, tables[t]) for t in VISIBLE],
            'held': _geomean(tables[t] for t in HELD),
            'all': _geomean(tables.values())}
    mean_ratio = sum(ratios.values()) / len(ratios) if ratios else None
    return out, mean_ratio


def header_line(calib, mean_ratio):
    commit = str(calib.get('commit') or '')[:10]
    if mean_ratio is None:
        why = 'no calibration recorded' if not calib.get('measured') else \
            'the design\'s widths were not read from its RTL'
        return ('packing model, UNCALIBRATED (%s%s): raw model numbers'
                % (why, ' for %s' % commit if commit else ''))
    rules = calib.get('rules')
    if rules == 'i35':
        shape = 'K%s/L%s/N%s, i35 rules' % (calib.get('K'), calib.get('L'), calib.get('N'))
    else:
        shape = 'K%s/L%s/N%s, %s rules' % (calib.get('K'), calib.get('L'), calib.get('N'), rules)
    return ('packing model, calibrated to %s (%s; measured/model %.2f)'
            % (commit, shape, mean_ratio))


def format_block(cfg, res, best_bpc, note=''):
    """The only per-what-if output: the visible tables one by one, the
    held-out tables as one geomean, and the overall geomean."""
    tag = '' if res['calibrated'] else '  uncalibrated'
    lines = [cfg_label(cfg) + (('  (%s)' % note) if note else '')]
    for table, bpc in res['visible']:
        lines.append('  %-30s %5.1f B/cycle%s' % ('%s (visible)' % table, bpc, tag))
    lines.append('  %-30s %5.1f B/cycle%s' % ('held-out tables, geomean of %d' % len(HELD),
                                               res['held'], tag))
    tail = ''
    if best_bpc and res['calibrated']:
        tail = '   (%+.0f%% bytes/cycle vs the current best\'s %.2f)' % (
            100.0 * (res['all'] / best_bpc - 1.0), best_bpc)
    lines.append('  %-30s %5.1f B/cycle%s%s' % ('all scored tables, geomean',
                                                res['all'], tag, tail))
    return '\n'.join(lines)


def current_widths(calib):
    """(K, L, N) of the best design in the ideal packer's terms, or None."""
    try:
        K, L, N = int(calib['K']), int(calib['L']), int(calib['N'])
        return K, L, N
    except (KeyError, TypeError, ValueError):
        return None


def standard_cfgs(calib):
    """[(cfg, note)]: the current widths, each dimension widened alone, the
    combinations the diag measured, and the DSW-4 design point."""
    out = []
    cur = current_widths(calib)
    if cur:
        K, L, N = cur
        out.append(((K, L, N), 'current widths'))
        out.append(((K + 1, L, N), 'K+1 alone'))
        out.append(((K, 2 * L, N), '2L alone'))
        out.append(((K, L, 2 * N), '2N alone'))
    for kln in ((3, 32, 32), (4, 32, 16), (4, 32, 32), (6, 64, 32)):
        out.append((kln, ''))
    cfgs, seen = [], set()
    for (K, L, N), note in out:
        try:
            check_limits(K, L, N, None)
        except ValueError:
            continue
        cfg = make_cfg('ideal', K, L, N)
        if cfg_id(cfg) not in seen:
            seen.add(cfg_id(cfg))
            cfgs.append((cfg, note))
    cfgs.append((make_cfg('dsw4', 4, 32, 32), 'the DSW-4 design point'))
    return cfgs


def render(key, calib, cfg_notes, allow_parse=False, jobs=None, stats=None,
           also=()):
    """(text, results). `also` cfgs are computed (one pass, one lock) but
    left out of the text: the loop's roadmap tags."""
    res, mean_ratio = evaluate(key, calib, [c for c, _n in cfg_notes] + list(also),
                               allow_parse, jobs, stats)
    best = calib.get('best_bpc')
    blocks = [header_line(calib, mean_ratio)]
    for cfg, note in cfg_notes:
        blocks.append(format_block(cfg, res[cfg_id(cfg)], best, note))
    return '\n'.join(blocks), res


def prompt_summary(run=None, cfgs=None, allow_parse=False):
    """The text the planner and the session brief get (aggregates only)."""
    key, calib = _context(run)
    cfg_notes = [(c, '') for c in cfgs] if cfgs else standard_cfgs(calib)
    return render(key, calib, cfg_notes, allow_parse)[0]


def predict(run, cfg, calib=None, allow_parse=False):
    """Aggregate prediction for one cfg: {'all', 'held', 'visible',
    'calibrated'}; calib names a commit (a track's calib_commit)."""
    key, cal = _context(run, calib)
    res, _ratio = evaluate(key, cal, [cfg], allow_parse)
    one = res[cfg_id(cfg)]
    return {'all': one['all'], 'held': one['held'], 'calibrated': one['calibrated'],
            'visible': dict(one['visible'])}


# ---------------------------------------------------------------------------
# Self-check (engineer only; the gate does not allow it)

KNOWN = (((2, 16, 8), 8.99), ((3, 32, 32), 16.00), ((4, 32, 32), 19.28))


def selfcheck(state_path=None, tol_pct=3.0):
    """Reproduce the diag's i35-calibrated geomeans and time a run.

    The measured per-table values come from hacc-real200's state.json at run
    time, never from this file. Cold means nothing was cached when it started
    (point AGENTIC_PACKMODEL_DIR at an empty folder to force one).
    """
    state_path = state_path or os.path.join(ROOT, '.agentic', 'runs', 'hacc-real200',
                                            'state.json')
    with open(state_path, encoding='utf-8') as fil:
        best = json.load(fil)['best']
    calib = {'rules': 'i35', 'K': 2, 'L': 16, 'N': 8, 'commit': best['commit'],
             'measured': measured_from_metrics(best['metrics'])}
    calib['best_bpc'] = round(_geomean(calib['measured'].values()), 4)
    stats = {}
    t0 = time.time()
    key = page_key()
    stats['hash_s'] = round(time.time() - t0, 1)
    cold = not os.path.isdir(_pages_dir(key)) and not read_json_safe(_raw_path(key))
    cfg_notes = standard_cfgs(calib)
    cfg_notes += [(make_cfg('ideal', *kln), 'known') for kln, _v in KNOWN]
    text, res = render(key, calib, cfg_notes, allow_parse=True, stats=stats)
    first_s = time.time() - t0
    t2 = time.time()
    render(key, calib, cfg_notes, allow_parse=True)
    warm_s = time.time() - t2
    print(text)
    print()
    ok = True
    for (K, L, N), want in KNOWN:
        got = res[cfg_id(make_cfg('ideal', K, L, N))]['all']
        miss = abs(got / want - 1.0) * 100.0
        ok = ok and miss <= tol_pct
        print('%-4s K%d/L%d/N%d geomean %.2f, diag %.2f (%.1f%% off; limit %.0f%%)'
              % ('ok' if miss <= tol_pct else 'MISS', K, L, N, got, want, miss, tol_pct))
    record = {'cold': cold, 'first_run_s': round(first_s, 1), 'warm_run_s': round(warm_s, 2),
              'jobs': _jobs(), 'main_peak_mb': round(_peak_mb(), 0),
              'worker_peak_mb': round(stats.get('worker_peak_mb', 0.0), 0),
              'parse_s': stats.get('parse_s', 0.0), 'model_s': stats.get('model_s', 0.0),
              'hash_s': stats['hash_s'], 'pass': ok, 'when': time.strftime('%Y-%m-%d %H:%M:%S')}
    write_json_atomic(os.path.join(cache_dir(), 'selfcheck.json'), record)
    print('%s run %.0f s (parse %.0f s, model %.0f s, hashing %.0f s), warm run %.2f s, '
          '%d workers, peak memory %.0f MB per worker, %.0f MB main'
          % ('cold' if cold else 'first (partly cached)', first_s, record['parse_s'],
             record['model_s'], record['hash_s'], warm_s, record['jobs'],
             record['worker_peak_mb'], record['main_peak_mb']))
    return 0 if ok else 1


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description='The packing model: modelled bytes/cycle on the real pages.')
    ap.add_argument('--k', type=int, help='elements (literals or copies) per cycle, 1-16')
    ap.add_argument('--l', type=int, help='output line bytes: 8, 16, 32, 64 or 128')
    ap.add_argument('--n', type=int, help='compressed input bytes per cycle: 8, 16, 32 or 64')
    ap.add_argument('--hazard', choices=HAZARDS)
    ap.add_argument('--litrate', type=int, help='literal streaming bytes per cycle, 1-128')
    ap.add_argument('--split', choices=SPLITS)
    ap.add_argument('--rules', choices=RULES)
    ap.add_argument('--selfcheck', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--prepare', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--extra', action='append', default=[], help=argparse.SUPPRESS)
    ap.add_argument('--json', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        check_limits(args.k, args.l, args.n, args.litrate)
    except ValueError as exc:
        print('packmodel: %s' % exc)
        return 2
    if args.selfcheck:
        return selfcheck()          # an engineer's tool: tracebacks are wanted
    try:
        return _main(args)
    except Busy:
        print(BUSY)
    except NotPrepared:
        print(NOT_PREPARED)
    except Exception:               # Unavailable included: never a path or a name
        print(FIXED_ERROR)
    return 3


def _main(args):
    run = os.environ.get('AGENTIC_RUN')
    if args.prepare:
        # Loop mode: the corpus is here; re-point the run if it was rebuilt.
        key, calib = _context(run)
        now = page_key()
        if now != key:
            write_pointer(run, now, calib['commit'])
            key = now
        ensure_pages(key, allow_parse=True)
    else:
        key, calib = _context(run)
    allow = bool(args.prepare)
    whatif = any(v is not None for v in (args.k, args.l, args.n, args.hazard,
                                          args.litrate, args.split, args.rules))
    if whatif:
        cur = current_widths(calib) or (None, None, None)
        K = args.k if args.k is not None else cur[0]
        L = args.l if args.l is not None else cur[1]
        N = args.n if args.n is not None else cur[2]
        rules = args.rules or 'ideal'
        if rules != 'i35' and None in (K, L, N):
            print('packmodel: give --k, --l and --n (the current widths are not known)')
            return 2
        check_limits(K, L, N, args.litrate)
        cfg = make_cfg(rules, K, L, N, args.hazard or 'window', args.litrate,
                       args.split or 'off')
        cfg_notes = [(cfg, '')]
    else:
        cfg_notes = standard_cfgs(calib)
    extras = [(parse_spec(s), s) for s in args.extra]
    # The loop's roadmap tags go to the JSON file, not into the text.
    text, res = render(key, calib, cfg_notes, allow, also=[c for c, _s in extras])
    print(text)
    if args.json:
        out = {'text': text, 'extra': {}}
        for cfg, spec in extras:
            one = res[cfg_id(cfg)]
            out['extra'][spec] = {'all': one['all'], 'held': one['held'],
                                  'calibrated': one['calibrated']}
        write_json_atomic(os.path.abspath(args.json), out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
