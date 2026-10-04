#!/usr/bin/env python3
"""
Checks for the packing model (packmodel.py) and the session gate around it.

test_kit.py runs every test_* here with its own `check`; run alone, this file
uses a minimal one. Nothing here starts a session or a simulation. The one
slow check (the diag numbers on the real pages) runs only when the page cache
for the current corpus already exists, or with AGENTIC_SLOW_TESTS=1.

Held-out per-table numbers never appear in this file: fixtures use invented
values, and the slow check reads the measured ones from the run's state.json
at run time.
"""

import ast
import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import packmodel as pm         # noqa: E402
import propose                 # noqa: E402
import stim                    # noqa: E402

check = None                   # set by test_kit.main
SCRATCH = os.path.join(ROOT, '.agentic', 'ktest')


def _rmtree(path):
    """Remove a scratch folder; git marks its object files read-only, which
    Windows refuses to delete until the flag is cleared."""
    def unlock(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=unlock)


def _scratch(name):
    os.makedirs(SCRATCH, exist_ok=True)
    return tempfile.mkdtemp(prefix=name + '-', dir=SCRATCH)


def _run_main(argv, env=None):
    """packmodel.main with stdout captured: (rc, text)."""
    saved = {}
    for key, val in (env or {}).items():
        saved[key] = os.environ.get(key)
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = pm.main(argv)
            except SystemExit as exc:
                rc = exc.code
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
    return rc, out.getvalue()


def _varint(n):
    out = bytearray()
    while True:
        low, n = n & 0x7f, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _replay(chunk, els):
    """Rebuild a chunk's bytes from parsed elements."""
    out = bytearray()
    for pos, kind, hdr, length, off in els:
        if kind == 'L':
            out += chunk[pos + hdr:pos + hdr + length]
        else:
            for _ in range(length):
                out.append(out[-off])
    return bytes(out)


# ---------------------------------------------------------------------------
# the model

def test_packmodel_parse_matches_model():
    from snappy import compress_raw, decompress_raw
    # Every element form by hand: short literal, 2-byte-length literal, and
    # copies with 1-, 2- and 4-byte offsets.
    lit = bytes(range(70))
    body = bytearray()
    body += bytes([(8 - 1) << 2]) + lit[:8]                        # literal, 8
    body += bytes([60 << 2, 61 - 1]) + lit[:61]                     # literal, 61
    body += bytes([1 | ((8 - 4) << 2) | ((69 >> 8) << 5), 69 & 0xff])   # copy 8 @69
    body += bytes([2 | ((20 - 1) << 2), 30, 0])                     # copy 20 @30
    body += bytes([3 | ((5 - 1) << 2), 7, 0, 0, 0])                 # copy 5 @7
    plain_len = 8 + 61 + 8 + 20 + 5
    chunk = _varint(plain_len) + bytes(body)
    els = pm.parse(chunk)
    kinds = [(e[1], e[2], e[3], e[4]) for e in els]
    check('packmodel parses every Snappy element form',
          kinds == [('L', 1, 8, 0), ('L', 2, 61, 0), ('C', 2, 8, 69),
                    ('C', 3, 20, 30), ('C', 5, 5, 7)], str(kinds))
    # The reference decompressor refuses 4-byte offsets, so the whole chunk
    # is checked against bytes built by hand, and the rest against it.
    want = lit[:8] + lit[:61]
    want += want[-69:-69 + 8]
    want += want[-30:-30 + 20]
    want += want[-7:-7 + 5]
    short = _varint(plain_len - 5) + bytes(body[:-5])
    check('replaying the parsed elements gives the decompressed bytes',
          _replay(chunk, els) == want
          and _replay(short, pm.parse(short)) == decompress_raw(short))
    data = stim.selftest_sample()
    comp = compress_raw(data)
    check('a reference-compressed chunk parses back to its own bytes',
          _replay(comp, pm.parse(comp)) == data)


def test_packmodel_ideal_tiny():
    far = [([(1, 'C', 2, 4, 64), (3, 'C', 2, 4, 64), (5, 'C', 2, 4, 64),
             (7, 'C', 2, 4, 64)], 9)]
    near = [([(1, 'C', 2, 4, 4), (3, 'C', 2, 4, 4), (5, 'C', 2, 4, 4),
              (7, 'C', 2, 4, 4)], 9)]
    got = [pm.ideal(far, 2, 16, 8), pm.ideal(far, 4, 16, 8)]
    check('ideal packer: four far copies take 2 cycles at K2, 1 at K4 (+1 chunk start)',
          got == [(16, 3), (16, 2)], str(got))
    got = [pm.ideal(near, 4, 16, 8), pm.ideal(near, 4, 16, 8, hazard='none'),
           pm.ideal(near, 4, 16, 8, hazard='strict16'),
           pm.ideal(far, 4, 16, 8, hazard='strict16')]
    check('the window hazard serialises offset-4 copies; none lifts it; strict16 is stricter',
          got == [(16, 5), (16, 2), (16, 5), (16, 2)], str(got))
    longc = [([(1, 'C', 2, 20, 64), (3, 'C', 2, 4, 64)], 5)]
    got = [pm.ideal(longc, 4, 16, 8, split='off'), pm.ideal(longc, 4, 16, 8, split='on')]
    check('split=on lets a cut copy\'s remainder share the next cycle',
          got == [(24, 4), (24, 3)], str(got))
    # On real-looking data: no hazard is never slower than the window rule.
    chunks = [(pm.parse(c), len(c)) for c in stim.selftest_chunks(1024)]
    worse = []
    for K, L, N in ((2, 16, 8), (4, 32, 32)):
        a = pm.ideal(chunks, K, L, N)
        b = pm.ideal(chunks, K, L, N, hazard='none')
        if b[0] / float(b[1]) < a[0] / float(a[1]):
            worse.append((K, L, N))
    check('hazard=none is an upper bound of hazard=window', not worse, str(worse))


# ---------------------------------------------------------------------------
# fixtures for the session-mode checks

RUN = 'kt-run'
COMMIT = 'abcdef0123456789abcdef0123456789abcdef01'
KEY = 'feedfacecafe'
# Invented numbers, one per table, chosen so no two print alike.
MEASURED = (8.61, 9.93, 9.12, 10.47, 9.58, 11.03, 8.87)


def _fixture_cache(with_cfgs, rules='i35'):
    """A cache dir holding a pointer, a calibration and raw results for
    with_cfgs (and the calibration's own model), and no pages."""
    cdir = _scratch('packmodel')
    calib = {'commit': COMMIT, 'rules': rules, 'K': 2, 'L': 16, 'N': 8,
             'source': 'test', 'measured': dict(zip(pm.TABLES, MEASURED))}
    calib['best_bpc'] = round(pm._geomean(MEASURED), 4)
    env = {'AGENTIC_PACKMODEL_DIR': cdir, 'AGENTIC_RUN': RUN, 'AGENTIC_CALIB': None}
    saved = os.environ.get('AGENTIC_PACKMODEL_DIR')
    os.environ['AGENTIC_PACKMODEL_DIR'] = cdir
    try:
        pm.write_json_atomic(pm._calib_path(COMMIT), calib)
        pm.write_pointer(RUN, KEY, COMMIT)
        raw = {}
        cfgs = list(with_cfgs) + [pm.calib_cfg(calib)]
        for j, cfg in enumerate(cfgs):
            for i, table in enumerate(pm.TABLES):
                raw['%s|%s' % (pm.cfg_id(cfg), table)] = 7.0 + 1.37 * i + 3.11 * j + 0.0123 * i * j
        pm.merge_raw(pm._raw_path(KEY), raw)
    finally:
        if saved is None:
            os.environ.pop('AGENTIC_PACKMODEL_DIR', None)
        else:
            os.environ['AGENTIC_PACKMODEL_DIR'] = saved
    return cdir, calib, raw, env


def _held_strings(calib, raw, cfgs):
    """Every per-table held-out number a leak could print, in the formats a
    leak would use."""
    denom = pm.calib_cfg(calib)
    out = set()
    for table in pm.HELD:
        vals = [calib['measured'][table]]
        for cfg in cfgs:
            r = raw['%s|%s' % (pm.cfg_id(cfg), table)]
            d = raw['%s|%s' % (pm.cfg_id(denom), table)]
            vals += [r, calib['measured'][table] / d * r, calib['measured'][table] / d]
        for v in vals:
            out.add('%.2f' % v)
            out.add('%.3f' % v)
    return out


def _names_in(text):
    return [t for t in pm.HELD if re.search(r'\b%s\b' % re.escape(t), text)]


def test_packmodel_output_hides_held_out():
    calib0 = {'K': 2, 'L': 16, 'N': 8}
    cfgs = [c for c, _n in pm.standard_cfgs(calib0)]
    cdir, calib, raw, env = _fixture_cache(cfgs)
    try:
        rc, text = _run_main([], env)
        rc2, text2 = _run_main(['--k', '4', '--l', '32', '--n', '32'], env)
        printed = set(re.findall(r'\d+\.\d+', text + text2))
        leaks = sorted(printed & _held_strings(calib, raw, cfgs))
        check('packmodel prints the standard set and a what-if from the cache',
              rc == 0 and rc2 == 0 and 'held-out tables, geomean of 6' in text
              and 'K4 L32 N32' in text2, '%s %s %s' % (rc, rc2, text[-300:]))
        check('packmodel output names no held-out table and prints no held-out number',
              not _names_in(text + text2) and not leaks,
              '%s %s' % (_names_in(text + text2), leaks))
        visible = [t for t in pm.VISIBLE if '%s (visible)' % t in text]
        check('the visible table is shown per table', visible == list(pm.VISIBLE))
        check('no area, f_max or per-LUT figure in the packing model output',
              not re.search(r'LUT|MHz|f_max|area', text + text2), text[:200])
    finally:
        _rmtree(cdir)


def test_packmodel_error_line_is_fixed():
    saved = pm._context

    def boom(*_a, **_k):
        raise ValueError('C:/secret/state.json %s 1.2345' % pm.HELD[0])
    pm._context = boom
    try:
        rc, text = _run_main([])
    finally:
        pm._context = saved
    check('any packmodel failure prints one fixed line and exits 3',
          rc == 3 and text == pm.FIXED_ERROR + '\n', repr((rc, text)))
    rc, text = _run_main(['--k', '99'])
    check('an argument outside the limits exits 2', rc == 2, repr((rc, text)))
    cdir = _scratch('packmodel-empty')
    try:
        rc, text = _run_main([], {'AGENTIC_PACKMODEL_DIR': cdir, 'AGENTIC_RUN': RUN})
    finally:
        _rmtree(cdir)
    check('without the loop\'s pointer it says it is not prepared',
          rc == 3 and text == pm.NOT_PREPARED + '\n', repr((rc, text)))


def test_packmodel_session_mode_never_parses():
    cached = pm.make_cfg('ideal', 4, 32, 32)
    cdir, _calib, _raw, env = _fixture_cache([cached])
    calls = []
    saved_tp, saved_pc = stim.table_path, stim.page_chunks

    def spy(*args, **_k):
        calls.append(args)
        raise IOError('no data here')
    stim.table_path = spy
    stim.page_chunks = spy
    try:
        rc1, text1 = _run_main(['--k', '4', '--l', '32', '--n', '32'], env)
        rc2, text2 = _run_main(['--k', '5', '--l', '32', '--n', '32'], env)
    finally:
        stim.table_path, stim.page_chunks = saved_tp, saved_pc
        _rmtree(cdir)
    check('a session answers a cached what-if without data files',
          rc1 == 0 and 'K4 L32 N32' in text1, repr((rc1, text1[-200:])))
    check('an uncached what-if with no parsed pages prints the fixed line',
          rc2 == 3 and text2 == pm.FIXED_ERROR + '\n', repr((rc2, text2)))
    check('session mode never touches the Parquet files', not calls, str(calls))


def test_packmodel_cache_writes_are_atomic_and_merge():
    cdir = _scratch('packmodel-cache')
    try:
        path = os.path.join(cdir, 'raw-x.json')
        pm.merge_raw(path, {'a': 1.0})
        pm.merge_raw(path, {'b': 2.0})
        both = pm.read_json_safe(path)
        with open(path, 'w', encoding='utf-8') as fil:
            fil.write('{"a": 1.0, "b"')
        empty = pm.read_json_safe(path)
        pm.merge_raw(path, {'c': 3.0})
        rebuilt = pm.read_json_safe(path)
        leftovers = [n for n in os.listdir(cdir) if n.endswith('.tmp')]
    finally:
        _rmtree(cdir)
    check('two writers\' raw entries both survive', both == {'a': 1.0, 'b': 2.0}, str(both))
    check('a truncated cache file reads as empty and is rebuilt',
          empty == {} and rebuilt == {'c': 3.0}, '%s %s' % (empty, rebuilt))
    check('atomic writes leave no temp files', not leftovers, str(leftovers))


def test_packmodel_calibration_mapping():
    base = _scratch('packmodel-rtl')
    try:
        i35 = os.path.join(base, 'i35')
        dsw = os.path.join(base, 'dsw4')
        other = os.path.join(base, 'other')
        for path in (i35, dsw, other):
            os.makedirs(path)
        with open(os.path.join(i35, 'vhsnunzip_int_pkg.vhd'), 'w') as fil:
            fil.write('type element_stream is record\n  cp1_val : std_logic;\n'
                      '  cp2_val : std_logic;\nend record;\n')
        shutil.copy(os.path.join(i35, 'vhsnunzip_int_pkg.vhd'), dsw)
        with open(os.path.join(dsw, 'vhsnunzip_dsw4_pkg.vhd'), 'w') as fil:
            fil.write('  constant K_SLOTS    : natural := 4;\n'
                      '  constant LINE_B     : natural := 32;\n')
        with open(os.path.join(other, 'vhsnunzip_int_pkg.vhd'), 'w') as fil:
            fil.write('type element_stream is record\n  cp_val : std_logic;\nend record;\n')
        w = {'copy_slots': 2.0, 'core_line_bytes': 16.0, 'in_bytes': 8,
             'elements_per_transfer': 3.0, 'defaulted': []}
        a = pm.design_mapping('c1', w, i35)
        b = pm.design_mapping('c1', dict(w, in_bytes=32), dsw)
        c = pm.design_mapping('c1', dict(w, defaulted=['copy_slots', 'elements_per_transfer']), i35)
        d = pm.design_mapping('c1', w, other)
        e = pm.design_mapping('c1', w, i35, override={'commit': 'c0', 'rules': 'ideal',
                                                       'K': 4, 'L': 32, 'N': 32})
        f = pm.design_mapping('c1', w, i35, override={'commit': 'c1', 'rules': 'ideal',
                                                       'K': 4, 'L': 32, 'N': 32})
    finally:
        _rmtree(base)
    check('i35 widths map to the i35 rules at K2/L16/N8',
          (a['rules'], a['K'], a['L'], a['N']) == ('i35', 2, 16, 8), str(a))
    check('a DSW-4 package maps to the DSW-4 model at its own K and line',
          (b['rules'], b['K'], b['L'], b['N']) == ('dsw4', 4, 32, 32), str(b))
    check('a defaulted K maps to uncalibrated', c['rules'] is None, str(c))
    check('another design with read widths maps to the ideal packer at its elements',
          (d['rules'], d['K']) == ('ideal', 3), str(d))
    check('a packmodel_widths override for another commit is ignored',
          e['rules'] == 'i35' and f['rules'] == 'ideal' and f['K'] == 4, '%s %s' % (e, f))


def test_packmodel_cache_is_outside_worktrees():
    loop = os.path.join('C:' + os.sep, 'work', 'kit')
    cand = os.path.join(loop, '.agentic', 'runs', 'r1', 'iter-5', 'c1')
    base = os.path.join(loop, '.agentic', 'runs', 'r1', 'base')
    got = [pm.loop_root(p) for p in (loop, cand, base)]
    check('the main checkout, the base and every candidate worktree share one cache',
          got == [os.path.normpath(loop)] * 3, str(got))
    saved = os.environ.pop('AGENTIC_PACKMODEL_DIR', None)
    try:
        cdir = os.path.normpath(pm.cache_dir())
    finally:
        if saved is not None:
            os.environ['AGENTIC_PACKMODEL_DIR'] = saved
    check('the cache sits in the loop checkout\'s .agentic, which git ignores',
          cdir == os.path.join(pm.loop_root(), '.agentic', 'packmodel'), cdir)


# ---------------------------------------------------------------------------
# the gate

ALLOWED_PACKMODEL = ('python agentic/packmodel.py',
                     'python agentic/packmodel.py --k 4 --l 32 --n 32 --rules dsw4',
                     'python3 agentic/packmodel.py --hazard none --split on --litrate 16')
DENIED_PACKMODEL = ('python agentic/packmodel.py; rm -rf rtl',
                    'python agentic/packmodel.py | cat',
                    'python agentic/packmodel.py --dump',
                    'python agentic/packmodel.py --selfcheck',
                    'python agentic/packmodel.py --prepare',
                    'python agentic/packmodel.py --json x.json',
                    'python agentic/stim.py',
                    'python agentic/packmodel.py $(x)',
                    'python agentic/packmodel.py --k 4 > out.txt')
ALLOWED_GIT = ('git status', 'git status --porcelain',
               'git show HEAD:rtl/vhsnunzip_pkg.vhd',
               'git show agentic-cand/hacc-real200/i35-c2:rtl/vhsnunzip_decoder.vhd',
               'git show HEAD --stat', 'git show HEAD~2 -- rtl/',
               'git diff HEAD~1..HEAD -- rtl/', 'git diff -- rtl',
               'git diff --stat main -- rtl docs/track-x/SPEC.md',
               'git log --oneline -n 20', 'git log', 'git log -p -n 3 -- rtl/',
               'git checkout -- rtl', 'git checkout -- rtl/vhsnunzip_decoder.vhd')
DENIED_GIT = ('git diff --no-index /etc/x rtl/a.vhd', 'git log --output=../x',
              'git diff --output=x -- rtl/', 'git diff --ext-diff -- rtl/',
              'git show --textconv HEAD:rtl/a', 'git diff -a -- rtl/',
              'git show HEAD:agentic/data/x.parquet', 'git show HEAD:../x',
              'git show HEAD:/abs', 'git diff HEAD', 'git -C .. status',
              'git show HEAD', 'git show HEAD:rtl/../agentic/check.py',
              'git diff HEAD -- agentic', 'git log -p', 'git diff -- rtl/.git',
              'git show HEAD:agentic/check.py', 'git log ..HEAD',
              'git checkout -- rtl/../agentic', 'git status; ls',
              'git log -n 3 --format=%H', 'git show -C HEAD:rtl/a')


def _gate_results(cmds, root, manifest_path=None):
    gate = propose.make_gate(root, manifest_path)

    async def ask_all():
        return [await gate('Bash', {'command': c}, None) for c in cmds]
    return asyncio.run(ask_all())


def _git(args, cwd):
    return subprocess.run(['git', '-c', 'user.name=kit', '-c', 'user.email=kit@test',
                           '-c', 'core.autocrlf=false'] + args, cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def _throwaway_repo(name):
    root = _scratch(name)
    _git(['init', '-q'], root)
    os.makedirs(os.path.join(root, 'agentic', 'ref'))
    os.makedirs(os.path.join(root, 'rtl'))
    for rel, text in (('agentic/check.py', 'old check\n'), ('agentic/ref/snappy.py', 'old\n'),
                      ('rtl/a.vhd', '-- rtl\n')):
        with open(os.path.join(root, rel), 'w', newline='\n') as fil:
            fil.write(text)
    _git(['add', '-A'], root)
    _git(['commit', '-q', '-m', 'base'], root)
    return root


def test_gate_allows_packmodel_only_strictly():
    kinds = [propose.command_kind(c) for c in ALLOWED_PACKMODEL]
    bad = [c for c in DENIED_PACKMODEL if propose.command_kind(c) is not None]
    check('the gate grammar accepts the packing model with what-if flags only',
          kinds == ['packmodel'] * len(ALLOWED_PACKMODEL) and not bad, '%s %s' % (kinds, bad))
    if not propose.SDK_OK:
        print('skip  packmodel through the live gate (claude_agent_sdk not importable)')
        return
    root = _throwaway_repo('gate')
    try:
        res = _gate_results(ALLOWED_PACKMODEL + DENIED_PACKMODEL, root)
    finally:
        _rmtree(root)
    n = len(ALLOWED_PACKMODEL)
    allowed = [isinstance(r, propose.PermissionResultAllow) for r in res]
    check('the live gate allows the packing model and denies everything around it',
          allowed == [True] * n + [False] * len(DENIED_PACKMODEL), str(allowed))


def test_gate_git_forms():
    bad_allowed = [c for c in ALLOWED_GIT if propose.command_kind(c) not in ('git', 'checkout')]
    bad_denied = [c for c in DENIED_GIT if propose.command_kind(c) is not None]
    check('the read-only git grammar allows its forms', not bad_allowed, str(bad_allowed))
    check('git forms that read or write outside rtl/ and docs/ are refused',
          not bad_denied, str(bad_denied))
    check('the brief lists the git forms the gate enforces',
          propose.GIT_FORMS_TEXT in propose.SYSTEM_BRIEF
          and '@@GITFORMS@@' not in propose.SYSTEM_BRIEF
          and 'python agentic/packmodel.py' in propose.SYSTEM_BRIEF)
    if not propose.SDK_OK:
        print('skip  git forms through the live gate (claude_agent_sdk not importable)')
        return
    root = _throwaway_repo('gitgate')
    try:
        res = _gate_results(ALLOWED_GIT + DENIED_GIT, root)
    finally:
        _rmtree(root)
    allowed = [isinstance(r, propose.PermissionResultAllow) for r in res]
    check('the live gate agrees with the git grammar',
          allowed == [True] * len(ALLOWED_GIT) + [False] * len(DENIED_GIT), str(allowed))


# ---------------------------------------------------------------------------
# the harness sync

def test_harness_clean_accepts_synced_kit():
    root = _throwaway_repo('sync')
    kit = _scratch('kit')
    try:
        files = {'check.py': 'new check\n', 'packmodel.py': 'model\n',
                 'ref/snappy.py': 'new ref\n', 'syn/unit.sh': '#!/bin/sh\n',
                 'ref/__pycache__/snappy.cpython-311.pyc': 'junk',
                 'data/taxi.parquet': 'data', 'test_kit.py': 'tests\n',
                 'kitcheck_x.py': 'tests\n', 'loop.py': 'loop\n'}
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(kit, rel)), exist_ok=True)
            with open(os.path.join(kit, rel), 'w', newline='\n') as fil:
                fil.write(text)
        manifest_path = root + '.harness.json'
        manifest = propose.sync_harness(root, manifest_path, kit=kit)
        man = propose.read_manifest(manifest_path)
        clean_after_sync = propose.harness_clean(root, man)
        copied = sorted(manifest)
        unlisted = [rel for rel in ('agentic/test_kit.py', 'agentic/kitcheck_x.py',
                                    'agentic/loop.py', 'agentic/data/taxi.parquet')
                    if os.path.exists(os.path.join(root, rel))]
        # the engineer edits the live kit while the session runs
        with open(os.path.join(kit, 'check.py'), 'w') as fil:
            fil.write('edited later\n')
        still_clean = propose.harness_clean(root, man)
        target = os.path.join(root, 'agentic', 'check.py')
        with open(target, 'w') as fil:
            fil.write('tampered\n')
        tampered = propose.harness_clean(root, man)
        with open(target, 'w', newline='\n') as fil:
            fil.write('new check\n')
        evil = os.path.join(root, 'agentic', 'evil.py')
        with open(evil, 'w') as fil:
            fil.write('x\n')
        with_evil = propose.harness_clean(root, man)
        os.remove(evil)
        os.makedirs(os.path.join(root, 'agentic', 'x'))
        with open(os.path.join(root, 'agentic', 'x', 'y.py'), 'w') as fil:
            fil.write('x\n')
        with_folder = propose.harness_clean(root, man)
        _rmtree(os.path.join(root, 'agentic', 'x'))
        without_manifest = propose.harness_clean(root, {})
    finally:
        _rmtree(root)
        _rmtree(kit)
        for leftover in (root + '.harness.json',):
            if os.path.exists(leftover):
                os.remove(leftover)
    check('the sync copies only the allowlist',
          copied == ['agentic/check.py', 'agentic/packmodel.py', 'agentic/ref/snappy.py',
                     'agentic/syn/unit.sh'] and not unlisted, '%s %s' % (copied, unlisted))
    check('a synced harness counts as clean; without the manifest it would not',
          clean_after_sync and not without_manifest)
    check('a kit edited after the sync does not block that session', still_clean)
    check('an edited synced file, a new file, or a new folder under agentic/ blocks',
          not tampered and not with_evil and not with_folder,
          '%s %s %s' % (tampered, with_evil, with_folder))


# ---------------------------------------------------------------------------
# no held-out numbers where a session can read

# A held-out table name within 40 characters of a decimal number. Known
# harmless matches, each named: hacc.py's `part` is the FPGA part variable;
# parquet_pages.py's dbgen(sf=0.01) is a scale factor; test_kit.py's 18.898 is
# a replay fixture from the old small-page corpus, when lineitem was the
# visible train table.
SCAN_EXCEPTIONS = (('agentic/hacc.py', 'part', '1000.0'),
                   ('agentic/ref/parquet_pages.py', 'lineitem', '0.01'),
                   ('agentic/test_kit.py', 'lineitem', '18.898'),
                   ('agentic/test_kit.py', 'lineitem', '0.01'))
NAME_RE = re.compile(r'\b(%s)\b' % '|'.join(re.escape(t) for t in stim.HELD_OUT_TABLES))
NUM_RE = re.compile(r'(?<![\w.])\d+\.\d+(?![\w.])')


def _held_values():
    """The run's measured held-out values, read at run time (never written
    in kit source): best and baseline of hacc-real200, as stored."""
    path = os.path.join(ROOT, '.agentic', 'runs', 'hacc-real200', 'state.json')
    out = set()
    try:
        with open(path, encoding='utf-8') as fil:
            state = json.load(fil)
    except (OSError, ValueError):
        return out
    for rec in (state.get('best') or {}, state.get('baseline') or {}):
        for draw in ((rec.get('metrics') or rec).get('draws') or []):
            if not draw.get('visible') and draw.get('bytes_per_cycle'):
                out.add('%.4f' % draw['bytes_per_cycle'])
                out.add('%.3f' % draw['bytes_per_cycle'])
    return out


def _scan(rel_paths, base):
    hits = []
    values = _held_values()
    for rel in rel_paths:
        try:
            with open(os.path.join(base, rel), encoding='utf-8', errors='replace') as fil:
                text = fil.read()
        except OSError:
            continue
        rel = rel.replace('\\', '/')
        if rel == 'agentic/stim.py':
            # the table-name tuple itself is the one place names are listed
            text = re.sub(r'HELD_OUT_TABLES = \([^)]*\)', '', text)
        # This file spells out the known harmless matches (SCAN_EXCEPTIONS),
        # so only the exact-value scan below applies to it.
        names = [] if rel == 'agentic/kitcheck_packmodel.py' else NAME_RE.finditer(text)
        for m in names:
            window = text[max(0, m.start() - 40):m.end() + 40]
            for num in NUM_RE.findall(window):
                if (rel, m.group(1), num) not in SCAN_EXCEPTIONS:
                    hits.append('%s: %s near %s' % (rel, m.group(1), num))
        for num in NUM_RE.findall(text):
            if num in values:
                hits.append('%s: a held-out measured value' % rel)
    return hits


def test_sync_allowlist_is_closed_and_clean():
    synced = ['agentic/' + rel for rel in propose.sync_list(KIT)]
    own = set(os.path.splitext(os.path.basename(p))[0] for p in synced if p.endswith('.py'))
    stdlib = set(getattr(sys, 'stdlib_module_names', ()))
    outside = []
    for rel in synced:
        if not rel.endswith('.py'):
            continue
        with open(os.path.join(ROOT, rel), encoding='utf-8') as fil:
            tree = ast.parse(fil.read())
        # An import in an `except ImportError:` handler is a fallback for
        # another repo layout (parquet_pages.py's emu.snappy), never reached
        # when the synced module beside it imports.
        fallback = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) \
                    and node.type.id in ('ImportError', 'ModuleNotFoundError'):
                for sub in node.body:
                    fallback.update(id(n) for n in ast.walk(sub))
        for node in ast.walk(tree):
            if id(node) in fallback:
                continue
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                top = name.split('.')[0]
                if top not in own and top not in stdlib:
                    outside.append('%s imports %s' % (rel, name))
    check('every module a synced file imports is stdlib or synced too',
          not outside, '; '.join(outside[:5]))
    hits = _scan(synced, ROOT)
    check('no synced file holds a held-out number', not hits, '; '.join(hits[:5]))


def test_blinded_worktree_has_no_held_out_numbers():
    # Tracked files, plus untracked ones git does not ignore: those are about
    # to be committed (this plan, new kit files), and then every worktree
    # and `git show` has them.
    res = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    tracked = sorted(set(p for p in res.stdout.splitlines() if p))
    # What blind() leaves in a candidate worktree outside rtl/.
    keep = [p for p in tracked
            if not p.startswith(('rtl/', 'test_data/', 'agentic/data/'))
            and not p.endswith('.parquet')]
    hits = _scan(keep, ROOT)
    check('no file a session can read outside rtl/ holds a held-out number',
          not hits, '; '.join(hits[:5]))


# ---------------------------------------------------------------------------
# slow: the diag numbers on the real pages

KNOWN = (((2, 16, 8), 8.99), ((3, 32, 32), 16.00), ((4, 32, 32), 19.28))


def test_packmodel_reproduces_diag():
    state_path = os.path.join(ROOT, '.agentic', 'runs', 'hacc-real200', 'state.json')
    try:
        key = pm.page_key()
    except Exception:
        print('skip  packing model vs diag (no Parquet data here)')
        return
    have_pages = os.path.isdir(pm._pages_dir(key))
    if not os.path.exists(state_path) or not (
            have_pages or os.environ.get('AGENTIC_SLOW_TESTS') == '1'):
        print('skip  packing model vs diag (no page cache yet; '
              'python agentic/packmodel.py --selfcheck builds it)')
        return
    with open(state_path, encoding='utf-8') as fil:
        best = json.load(fil)['best']
    calib = {'rules': 'i35', 'K': 2, 'L': 16, 'N': 8, 'commit': best['commit'],
             'measured': pm.measured_from_metrics(best['metrics'])}
    cfgs = [pm.make_cfg('ideal', *kln) for kln, _v in KNOWN]
    res, _ratio = pm.evaluate(key, calib, cfgs, allow_parse=True)
    got = [round(res[pm.cfg_id(c)]['all'], 2) for c in cfgs]
    misses = [(kln, g, want) for (kln, want), g in zip(KNOWN, got)
              if abs(g / want - 1.0) > 0.03]
    check('the packing model reproduces the diag geomeans within 3% '
          '(K2/L16/N8, K3/L32/N32, K4/L32/N32)', not misses, str(got))


if __name__ == '__main__':
    FAILED = []

    def check(name, condition, detail=''):          # noqa: F811
        if condition:
            print('ok    %s' % name)
        else:
            FAILED.append(name)
            print('FAIL  %s%s' % (name, ('  -- ' + detail) if detail else ''))
    for _name, _func in sorted(globals().items()):
        if _name.startswith('test_') and callable(_func):
            _func()
    print()
    print('%d check(s) failed' % len(FAILED) if FAILED else 'all checks pass')
    sys.exit(1 if FAILED else 0)
