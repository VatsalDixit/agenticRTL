#!/usr/bin/env python3
"""
Checks for the internal probe (agentic/probe.py) and its wiring into
measure.py and analyse.py.

test_kit.py runs every test_* here with its own check(); run on its own,
this file uses the check() below. Nothing here simulates except the one
slow check at the end, which compiles the instrumented i35 under GHDL and is
skipped when the EDA shell is not there (or AGENTIC_FAST_TESTS=1).

    python agentic/kitcheck_probe.py
"""

import hashlib
import os
import shutil
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import analyse                                       # noqa: E402
import freeze                                        # noqa: E402
import measure                                       # noqa: E402
import probe                                         # noqa: E402

FAILED = []
TMP = os.path.join(ROOT, '.agentic', 'ktest', 'probe')
# Sibling checkouts the plan names; skipped when absent.
DIAG_RTL = os.environ.get('AGENTIC_I35_RTL') or os.path.join(
    os.path.dirname(ROOT), 'diag-i35', 'rtl')
DSW4_RTL = os.environ.get('AGENTIC_DSW4_RTL') or os.path.join(
    os.path.dirname(ROOT), 'dsw4', 'rtl')


def check(name, condition, detail=''):
    """Replaced by test_kit.py's check() when run from there."""
    if condition:
        print('ok    %s' % name)
    else:
        FAILED.append(name)
        print('FAIL  %s%s' % (name, ('  -- ' + detail) if detail else ''))


def _fresh(name):
    path = os.path.join(TMP, name)
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return path


def _write(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as fil:
        fil.write(text)


def _names(arch, what):
    return sorted(h['name'] for h in arch[what])


# ---------------------------------------------------------------------------
# finding handshakes

def _check_i35_shape(rtl, label):
    found = probe.find_handshakes(rtl)
    by = dict((a['entity'], a) for a in found['arches'])
    pipe, top = by.get('vhsnunzip_pipeline'), by.get('vhsnunzip_unbuffered')
    check('probe: the i35 pipeline yields cs, cd, el, c1, cm (%s)' % label,
          pipe is not None and {'cs', 'cd', 'el', 'c1', 'cm'} <= set(_names(pipe, 'pairs')),
          str(pipe and _names(pipe, 'pairs')))
    check('probe: the top yields co and de, and s1_cm is occupancy (%s)' % label,
          top is not None and {'co', 'de'} <= set(_names(top, 'pairs'))
          and pipe is not None and 's1_cm' in _names(pipe, 'occupancy'),
          str(top and _names(top, 'pairs')))
    # A leaf entity's own ports are its parent's nets: a pair whose names are
    # both ports is counted only at the top.
    recount = ['%s/%s' % (a['key'], h['name']) for a in found['arches'] if not a['top']
               for h in a['pairs'] if all(h['ports'])]
    leaf_skips = [n for a in found['arches'] if a['entity'] == 'vhsnunzip_pipeline'
                  for n, why in a['skipped'] if 'both ports' in why]
    check('probe: no leaf entity re-counts a net of its parent (%s)' % label,
          not recount and {'co', 'de'} <= set(leaf_skips), ', '.join(recount))
    dec = next((h for h in pipe['pairs'] if h['name'] == 'el'), None) if pipe else None
    check('probe: el runs from the decoder to cmd_gen_1 (%s)' % label,
          dec is not None and 'decoder' in (dec['producer'] or '')
          and 'cmd_gen_1' in (dec['consumer'] or ''), str(dec))


def test_probe_finds_i35_handshakes():
    rtl = os.path.join(ROOT, 'rtl')
    if os.path.isdir(rtl):
        _check_i35_shape(rtl, 'rtl/')
    if os.path.isdir(DIAG_RTL):
        _check_i35_shape(DIAG_RTL, 'diag-i35')
    else:
        print('skip  probe on diag-i35/rtl (not present)')


# The sets found on DSW-4 (branch wide4-dsw4, ccf02c6) by the first run of the
# reader, frozen so a change to the reader that loses one shows up here.
DSW4_PAIRS = {
    'vhsnunzip_cofifo(behavior)': {'rd'},
    'vhsnunzip_core(behavior)': {'csf', 'cef'},
    'vhsnunzip_pipeline(behavior)': {'cs', 'cd', 'el', 'c1', 'cm'},
    'vhsnunzip_unbuffered(behavior)': {'co', 'de'},
}
DSW4_OCCUPANCY = {
    'vhsnunzip_agen(rtl)': {'a1', 'a2', 'rd_r(0)'},
    'vhsnunzip_blkrd(behavior)': {'blk_r', 't'},
    'vhsnunzip_core(behavior)': {'cof_rd', 'el(0)', 'pf(0)', 'wcmd', 'ag', 'push'},
    'vhsnunzip_defifo(rtl)': {'rd'},
    'vhsnunzip_dpath(rtl)': {'d1', 'd2', 's1', 's2', 'push_r', 'wr_r(0)', 's3'},
    'vhsnunzip_parser(behavior)': {'blk', 'win', 'wf_head', 'wf_mem(0)'},
    'vhsnunzip_pipeline(behavior)': {'s1_cm', 's1_cm_exp', 's1', 's2'},
    'vhsnunzip_pt(behavior)': {'s0', 's1', 'w1', 'w2', 'w3'},
    'vhsnunzip_unbuffered(behavior)': {'ram_a_cmd(0)', 'ram_a_resp(0)', 'ram_b_cmd(0)',
                                       'ram_b_resp(0)'},
    'vhsnunzip_walker(behavior)': {'el_r(0)'},
    'vhsnunzip_writer(behavior)': {'cmd_r', 'cmd_n'},
}


def test_probe_finds_dsw4_handshakes():
    if not os.path.isdir(DSW4_RTL):
        print('skip  probe on DSW-4 (dsw4/rtl not present)')
        return
    found = probe.find_handshakes(DSW4_RTL)
    pairs = dict((a['key'], set(_names(a, 'pairs'))) for a in found['arches'] if a['pairs'])
    occ = dict((a['key'], set(_names(a, 'occupancy'))) for a in found['arches']
               if a['occupancy'])
    check('probe: DSW-4 pairs are exactly the frozen set', pairs == DSW4_PAIRS, str(pairs))
    check('probe: DSW-4 occupancy is exactly the frozen set', occ == DSW4_OCCUPANCY, str(occ))
    core = next(a for a in found['arches'] if a['entity'] == 'vhsnunzip_core')
    styles = dict((h['name'], h['style']) for h in core['pairs'])
    check('probe: csf and cef are pop-style handshakes from cbuf to the parser',
          styles == {'csf': 'pop', 'cef': 'pop'}
          and all('cbuf' in h['producer'] and 'parse' in h['consumer'] for h in core['pairs']),
          str(core['pairs']))
    cof = next(a for a in found['arches'] if a['entity'] == 'vhsnunzip_cofifo')
    rd = cof['pairs'][0]
    check('probe: cofifo rd pairs a local rd_valid with the rd_ready port',
          rd['valid'] == 'rd_valid' and rd['ready'] == 'rd_ready' and rd['src'] == 'flat')


_BAD_TYPES = """\
library ieee;
use ieee.std_logic_1164.all;

entity odd is
  port (
    clk      : in  std_logic;
    reset    : in  std_logic;
    a_ready  : out std_logic
  );
end odd;

architecture rtl of odd is
  signal a_valid : std_logic_vector(0 downto 0);  -- not a scalar valid
  signal b_valid : std_logic;
  signal b_ready : std_logic_vector(0 downto 0);  -- not a scalar ready
  signal c_valid : std_logic;
  signal c_ready : boolean;                       -- a boolean ready is fine
  signal d_valid : std_logic;
  signal d_ready : std_logic;
begin
  g: for i in 0 to 1 generate
    signal e_valid : std_logic;                   -- local to a generate
  begin
  end generate;
end rtl;
"""


def test_probe_skips_bad_types():
    base = _fresh('badtypes')
    tmp = os.path.join(base, 'rtl')
    os.makedirs(tmp)
    _write(os.path.join(tmp, 'odd.vhd'), _BAD_TYPES)
    found = probe.find_handshakes(tmp, top='none')
    arch = found['arches'][0] if found['arches'] else {'pairs': [], 'skipped': []}
    pairs = _names(arch, 'pairs')
    skipped = dict(arch['skipped'])
    check('probe: a vector valid and a vector ready are skipped, with reasons',
          'a' in skipped and 'b' in skipped and 'a' not in pairs and 'b' not in pairs,
          str(arch['skipped']))
    check('probe: the other pairs are still found (a boolean ready included)',
          pairs == ['c', 'd'], str(pairs))
    check('probe: a signal local to a generate is skipped with its reason',
          'generate' in skipped.get('e_valid', ''), str(arch['skipped']))
    out = os.path.join(base, 'copy')
    probe.instrument(tmp, out, top='none')
    with open(os.path.join(out, 'odd.vhd'), encoding='utf-8') as fil:
        text = fil.read()
    check('probe: a boolean ready is read as a boolean, a std_logic one with = \'1\'',
          'if c_ready then' in text and "if d_ready = '1' then" in text)


_CLASH = """\
library ieee;
use ieee.std_logic_1164.all;

entity clash is
  port (clk : in std_logic; reset : in std_logic);
end clash;

architecture rtl of clash is
  -- Names that a careless probe would declare too.
  signal line    : std_logic;
  signal write   : std_logic;
  signal cycles  : std_logic;
  signal f       : std_logic;
  signal x_valid : std_logic;
  signal x_ready : std_logic;
begin
  line <= '0'; write <= '0'; cycles <= '0'; f <= '0';
  x_valid <= '1'; x_ready <= '1';
end rtl;
"""


def _inserted(text):
    """The blocks probe.py inserted into one file (with their newlines)."""
    out = []
    pos = 0
    while True:
        start = text.find('-- pragma translate_off', pos)
        if start < 0:
            break
        stop = text.find('-- pragma translate_on', start)
        stop = text.find('\n', stop) + 1
        if '%s_p: process' % probe.PREFIX in text[start:stop]:
            out.append((start, stop))
        pos = stop
    return out


def test_probe_identifiers_are_prefixed():
    import re
    base = _fresh('clash')
    tmp = os.path.join(base, 'rtl')
    os.makedirs(tmp)
    _write(os.path.join(tmp, 'clash.vhd'), _CLASH)
    out = os.path.join(base, 'copy')
    info = probe.instrument(tmp, out, top='clash')
    with open(os.path.join(out, 'clash.vhd'), encoding='utf-8') as fil:
        text = fil.read()
    blocks = _inserted(text)
    block = text[blocks[0][0]:blocks[0][1]] if blocks else ''
    declared = re.findall(r'^\s*(?:variable|constant|type|file|signal)\s+(\w+)', block, re.M)
    declared += re.findall(r'\b(?:function|procedure)\s+(\w+)', block)
    declared += re.findall(r'\bfor\s+(\w+)\s+in\b', block)
    declared += re.findall(r'^\s*(\w+)\s*:\s*process\b', block, re.M)
    declared += re.findall(r'\(\s*(\w+)\s*:\s*string\s*\)', block)
    bad = [d for d in declared if not d.lower().startswith(probe.PREFIX + '_')]
    check('probe: every name the probe declares starts with agentic_probe_',
          info and declared and not bad, 'unprefixed: %s' % bad)
    check('probe: it uses no use clause and qualifies textio',
          'use ' not in block.lower() and 'std.textio.writeline' in block
          and re.search(r'(?<![.\w])writeline\b', block) is None)
    drives = re.findall(r'^\s*(\w+)\s*<=', block, re.M)
    check('probe: it drives no signal', not drives, str(drives))


def _sha(path):
    with open(path, 'rb') as fil:
        return hashlib.sha256(fil.read()).hexdigest()


def test_probe_never_touches_the_original():
    rtl = DIAG_RTL if os.path.isdir(DIAG_RTL) else os.path.join(ROOT, 'rtl')
    if not os.path.isdir(rtl):
        print('skip  probe copy check (no rtl/)')
        return
    before = dict((f, _sha(os.path.join(rtl, f))) for f in sorted(os.listdir(rtl))
                  if os.path.isfile(os.path.join(rtl, f)))
    out = os.path.join(_fresh('copy'), 'probe_rtl')
    info = probe.instrument(rtl, out)
    after = dict((f, _sha(os.path.join(rtl, f))) for f in before)
    check('probe: the candidate rtl/ is byte for byte unchanged', before == after)
    differ = []
    for name in before:
        with open(os.path.join(rtl, name), 'rb') as fil:
            orig = fil.read().decode('latin-1')
        with open(os.path.join(out, name), 'rb') as fil:
            copy = fil.read().decode('latin-1')
        for start, stop in reversed(_inserted(copy)):
            copy = copy[:start] + copy[stop:]
        if copy != orig:
            differ.append(name)
    check('probe: the copy differs from rtl/ only inside the inserted pragmas',
          info and not differ, ', '.join(differ))
    try:
        probe.instrument(rtl, os.path.join(rtl, 'inside'))
        refused = False
    except ValueError:
        refused = True
    check('probe: it refuses to write its copy inside rtl/', refused
          and not os.path.exists(os.path.join(rtl, 'inside')))


# ---------------------------------------------------------------------------
# measure.py: fallback, reruns, the window marker

class _Draw(object):
    kind, chunk = 'synthetic', 1

    def __init__(self, name, visible=True, scored=False):
        self.name, self.visible, self.scored = name, visible, scored


WIDTHS = {'in_bytes': 8, 'in_cnt_bits': 3, 'out_bytes': 16, 'out_cnt_bits': 5,
          'cores': 1, 'core_line_bytes': 16.0, 'unresolved': []}


def _draws(tmp, names):
    out = []
    for name in names:
        src = os.path.join(tmp, 'corpus', name)
        os.makedirs(src, exist_ok=True)
        _write(os.path.join(src, 'cs.tv'), 'x\n')
        out.append((_Draw(name, visible=not name.startswith('held-')), src, 1))
    return out


def _prep(tmp):
    return {'rtl': os.path.join(tmp, 'probe_rtl'), 'plain': os.path.join(tmp, 'rtl'),
            'probed': True, 'instrumented': True, 'status': 'on', 'exclude': [],
            'out': os.path.join(tmp, 'probe_rtl'), 'window': 4096,
            'info': {'arches': ['a(rtl)', 'b(rtl)'],
                     'ranges': {'a(rtl)': ('a.vhd', 10, 40), 'b(rtl)': ('b.vhd', 5, 30)}}}


class _Patch(object):
    """Swap module attributes for one test and put them back."""

    def __init__(self, **swaps):
        self.swaps = swaps
        self.saved = []

    def __enter__(self):
        for key, val in self.swaps.items():
            mod, attr = key.split('__')
            target = {'measure': measure, 'probe': probe, 'analyse': analyse,
                      'oracle': measure.oracle}[mod]
            self.saved.append((target, attr, getattr(target, attr)))
            setattr(target, attr, val)
        return self

    def __exit__(self, *exc):
        for target, attr, val in reversed(self.saved):
            setattr(target, attr, val)


def _perf(ddir):
    _write(os.path.join(ddir, 'perf.txt'), 'cycles=100\nbytes_out=800\nco_beats=10\n'
                                           'de_beats=50\nco_stall=0\nde_bubble=50\n')


def test_probe_build_failure_falls_back():
    tmp = _fresh('fallback')
    draws = _draws(tmp, ['synth-a', 'held-b'])
    calls, excludes = [], []

    def fake_run(rtl_dir, build_dir, generics, ds, timeout, jobs=None, probed=False):
        calls.append(('probed' if probed else 'plain', rtl_dir))
        if probed:
            raise measure.ProbeBuildError('the probe copy does not compile',
                                          log='/x/probe_rtl/a.vhd:20:3:error: bad\nELAB_FAIL')
        out = {}
        for _d, ddir, _c in ds:
            _perf(ddir)
            out[measure.shell_path(ddir)] = (0, False)
        return out

    def fake_instrument(rtl_dir, out_dir, window=4096, exclude=(), top='x'):
        excludes.append(list(exclude))
        return {'arches': ['b(rtl)'], 'ranges': {'b(rtl)': ('b.vhd', 5, 30)},
                'pairs': 1, 'occupancy': 0, 'skipped': [], 'excluded': list(exclude)}

    prep = _prep(tmp)
    with _Patch(measure___run_draws=fake_run, probe__instrument=fake_instrument,
                oracle__check=lambda cs, out: []):
        res = measure.simulate(prep['plain'], os.path.join(tmp, 'build'), draws, WIDTHS,
                               probe_rtl=prep)
    kinds = [c[0] for c in calls]
    check('probe: a probe copy that does not compile is retried without the architecture '
          'the compiler named, then measured plain',
          kinds == ['probed', 'probed', 'plain'] and excludes == [['a(rtl)']]
          and calls[-1][1] == prep['plain'], '%s %s' % (calls, excludes))
    check('probe: the plain run\'s results are returned, with no probe in them',
          len(res) == 2 and all(r['oracle_pass'] and r['bytes_per_cycle'] == 8.0
                                and 'probe' not in r for r in res), str(res))
    check('probe: probe_status says it fell back',
          prep['status'].startswith('fallback') and not prep['probed'], prep['status'])


def test_probe_timeout_is_not_rerun():
    tmp = _fresh('timeout')
    draws = _draws(tmp, ['synth-a'])
    calls = []

    def fake_run(rtl_dir, build_dir, generics, ds, timeout, jobs=None, probed=False):
        calls.append(probed)
        raise measure.MeasureError('simulation did not finish within 10 s')

    try:
        with _Patch(measure___run_draws=fake_run):
            measure.simulate('rtl', os.path.join(tmp, 'build'), draws, WIDTHS,
                             probe_rtl=_prep(tmp))
        raised = False
    except measure.ProbeBuildError:
        raised = False
    except measure.MeasureError:
        raised = True
    check('probe: a timeout under the probe is the design\'s; it is not run again',
          raised and calls == [True], str(calls))


def test_probe_rerun_rules():
    tmp = _fresh('rerun')
    names = ['synth-good', 'synth-crash', 'synth-wrong', 'synth-hang']
    draws = _draws(tmp, names)
    calls, stale_seen = [], []

    def fake_run(rtl_dir, build_dir, generics, ds, timeout, jobs=None, probed=False):
        calls.append((probed, sorted(d[0].name for d in ds)))
        out = {}
        for d, ddir, _c in ds:
            key = measure.shell_path(ddir)
            name = d.name
            if not probed:
                stale_seen.extend(f for f in os.listdir(ddir) if f.startswith('agentic_probe'))
            if name == 'synth-crash' and probed:
                _write(os.path.join(ddir, 'agentic_probe.x.tot'), 'stale\n')
                out[key] = (1, False)
            elif name == 'synth-hang':
                out[key] = (1, True)
            else:
                _perf(ddir)
                out[key] = (0, False)
        return out

    with _Patch(measure___run_draws=fake_run,
                oracle__check=lambda cs, out: ['byte 3 differs'] if 'wrong' in cs else []):
        res = measure.simulate('rtl', os.path.join(tmp, 'build'), draws, WIDTHS,
                               probe_rtl=_prep(tmp))
    by = dict((r['name'], r) for r in res)
    check('probe: an error exit with no perf.txt and no deadlock is rerun once, plain',
          calls == [(True, sorted(names)), (False, ['synth-crash'])]
          and by['synth-crash']['oracle_pass'] and 'probe' not in by['synth-crash'],
          str(calls))
    check('probe: the rerun starts with the stale probe files deleted', not stale_seen,
          str(stale_seen))
    check('probe: a wrong output and a deadlock are not rerun',
          not by['synth-wrong']['oracle_pass'] and 'wrong output' in by['synth-wrong']['problem']
          and 'deadlock' in by['synth-hang']['problem'])


def test_probe_shared_across_one_measurement():
    tmp = _fresh('shared')
    draws = [(_Draw('small', scored=True), 'quick', 1), (_Draw('long', scored=True), 'slow', 1)]
    preps, seen = [], []

    def fake_prepare(rtl_dir, build_dir):
        prep = dict(_prep(tmp), status='on')
        preps.append(prep)
        return prep

    def fake_sim(rtl, build, ds, widths, timeout=None, jobs=None):
        with measure._SHARED_LOCK:
            seen.append(measure._SHARED_PROBE.get(measure._probe_key(rtl, build)))
        return [{'name': d.name, 'kind': 'real', 'scored': True, 'visible': True,
                 'chunks': 1, 'oracle_pass': True, 'bytes_per_cycle': 5.0, 'problem': None}
                for d, _p, _c in ds]

    with _Patch(measure__prepare_probe=fake_prepare, measure__simulate=fake_sim,
                measure__draw_bytes=lambda e: 10 if e[1] == 'quick' else 10 ** 8):
        saved = measure.analyse.rtl_widths
        measure.analyse.rtl_widths = lambda rtl: {}
        try:
            out = measure.measure('rtl-x', tmp, draws, synth=False)
        finally:
            measure.analyse.rtl_widths = saved
    check('probe: one measurement instruments once and both simulate() calls share it',
          len(preps) == 1 and len(seen) == 2 and seen[0] is preps[0] and seen[1] is preps[0],
          '%d preps, seen %s' % (len(preps), [s is not None for s in seen]))
    check('probe: the shared entry is gone after the measurement, and its status is kept',
          not measure._SHARED_PROBE and out.get('probe_status') == 'on', str(out.get('probe_status')))


def test_probe_windows_only_with_marker():
    tmp = _fresh('marker')
    draws = _draws(tmp, ['train-taxi', 'synth-512B', 'held-supplier'])
    stale = os.path.join(tmp, 'build', 'draws', 'held-supplier')
    os.makedirs(stale, exist_ok=True)
    _write(os.path.join(stale, 'agentic_probe.old.win'), 'W 0 1 2 3\n')
    _write(os.path.join(stale, probe.MARKER), 'windows\n')
    private = measure._private_draws(draws, os.path.join(tmp, 'build'), windows=True)
    have = dict((d.name, sorted(f for f in os.listdir(p) if f.startswith('agentic_probe')))
                for d, p, _c in private)
    check('probe: the window marker goes into train- and synth- folders only, and old '
          'probe files are cleared',
          have == {'train-taxi': [probe.MARKER], 'synth-512B': [probe.MARKER],
                   'held-supplier': []}, str(have))
    private = measure._private_draws(draws, os.path.join(tmp, 'build2'), windows=False)
    check('probe: with windows off no folder gets the marker',
          not any(os.path.exists(os.path.join(p, probe.MARKER)) for _d, p, _c in private))


# ---------------------------------------------------------------------------
# reading the counts, and the verdict

def _tot(path, inst_path, entity, top, cycles, order):
    lines = ['agentic_probe 1', 'path ' + inst_path, 'arch %s behavior %d' % (entity, top),
             'cycles %d' % cycles, 'window 4096']
    for item in order:
        lines.append(item)
    _write(path, '\n'.join(lines) + '\n')


def _i35_like(ddir, cycles=100000):
    """Totals shaped like i35 on taxi: cd waits 38%, el moves 97%, nothing after waits."""
    def hs(kind, name, prod, cons, moved, blocked):
        empty = 1.0 - moved - blocked
        return 'H %s %s %s %s %s %d %d %d' % (
            kind, 'ready' if kind == 'P' else '-', name, prod, cons,
            round(moved * cycles), round(blocked * cycles), round(empty * cycles))
    top = ':vhsnunzip_perf_tc:uut:'
    _tot(os.path.join(ddir, 'agentic_probe.vhsnunzip_perf_tc.uut.tot'), top,
         'vhsnunzip_unbuffered', 1, cycles,
         [hs('P', 'co', '^', 'datapath_inst:vhsnunzip_pipeline', 0.30, 0.65),
          'I datapath_inst vhsnunzip_pipeline',
          hs('P', 'de', 'datapath_inst:vhsnunzip_pipeline', '^', 0.60, 0.0)])
    _tot(os.path.join(ddir, 'agentic_probe.vhsnunzip_perf_tc.uut.datapath_inst.tot'),
         top + 'datapath_inst:', 'vhsnunzip_pipeline', 0, cycles,
         ['I co_fifo_inst vhsnunzip_fifo',
          hs('P', 'cs', 'co_fifo_inst:vhsnunzip_fifo', 'pre_dec_inst:vhsnunzip_pre_decoder',
             0.55, 0.40),
          'I pre_dec_inst vhsnunzip_pre_decoder',
          hs('P', 'cd', 'pre_dec_inst:vhsnunzip_pre_decoder',
             'main_dec_inst:vhsnunzip_decoder_long', 0.58, 0.38),
          'I main_dec_inst vhsnunzip_decoder_long',
          hs('P', 'el', 'main_dec_inst:vhsnunzip_decoder_long',
             'cmd_gen_1_inst:vhsnunzip_cmd_gen_1', 0.97, 0.0),
          'I cmd_gen_1_inst vhsnunzip_cmd_gen_1',
          hs('P', 'c1', 'cmd_gen_1_inst:vhsnunzip_cmd_gen_1',
             'cmd_gen_2_inst:vhsnunzip_cmd_gen_2', 0.96, 0.0),
          'I cmd_gen_2_inst vhsnunzip_cmd_gen_2',
          hs('P', 'cm', 'cmd_gen_2_inst:vhsnunzip_cmd_gen_2', '-', 0.95, 0.0),
          hs('O', 's1_cm', 'cm_fifo_inst:vhsnunzip_fifo', '-', 0.95, 0.0)])


def test_probe_file_parser_and_verdict():
    tmp = _fresh('verdict')
    _i35_like(tmp)
    summary = probe.summarise(probe.read(tmp))
    keys = [h['key'] for h in (summary or {}).get('handshakes', [])]
    check('probe: totals are read back in data-flow order across the hierarchy',
          keys == ['co', 'datapath_inst/cs', 'datapath_inst/cd', 'datapath_inst/el',
                   'datapath_inst/c1', 'datapath_inst/cm', 'datapath_inst/s1_cm', 'de'],
          str(keys))
    verdict = analyse.probe_verdict(summary)
    check('probe: the verdict names the decoder, not the pipeline around it',
          verdict and verdict['kind'] == 'rate'
          and verdict['limit'] == 'datapath_inst/main_dec_inst'
          and 'decoder' in verdict['text'], str(verdict and verdict['text']))
    combined = {'binding': 'core datapath', 'tightest': 'core datapath',
                'tightest_utilisation': 0.6, 'stale_ceilings': [],
                'mean_output_idle_pct': 40.0, 'probe': verdict}
    lev = analyse.lever(combined)
    check('probe: output idle that is the decoder\'s rate is a width lever, not latency',
          lev['lever'] == 'width' and 'not bubbles' in lev['reason'], str(lev))
    text = analyse.describe_probe(verdict)
    check('probe: describe prints one line per handshake and the verdict',
          text.count('\n') >= len(keys) and 'probe verdict' in text)
    both = probe.combine([summary, probe.summarise(probe.read(tmp))])
    check('probe: two equal draws combine to the same fractions',
          both and [h['moved'] for h in both['handshakes']]
          == [h['moved'] for h in summary['handshakes']])


def test_probe_verdict_on_i35_measured_numbers():
    # The cycle-weighted fractions the probe measured on hacc-real200's best
    # (i35) over its 7 scored draws. The decoder's output waits 4.2% and co,
    # cs and cd tie at 41.6%; the verdict once named the wrapper datapath_inst.
    import copy
    import loop
    tmp = _fresh('i35-real')
    _i35_like(tmp)
    summary = probe.summarise(probe.read(tmp))
    real = {'co': (0.584, 0.416), 'datapath_inst/cs': (0.584, 0.416),
            'datapath_inst/cd': (0.584, 0.416), 'datapath_inst/el': (0.958, 0.042),
            'datapath_inst/c1': (0.997, 0.003), 'datapath_inst/cm': (1.0, 0.0),
            'de': (0.62, 0.0)}
    for h in summary['handshakes']:
        if h['key'] in real:
            h['moved'], h['blocked'] = real[h['key']]
            h['empty'] = round(1.0 - h['moved'] - h['blocked'], 3)
    verdict = analyse.probe_verdict(summary)
    check('probe: on i35\'s measured numbers the decoder is the limit (its output waits 4.2%)',
          verdict and verdict['kind'] == 'rate'
          and verdict['limit'] == 'datapath_inst/main_dec_inst', str(verdict and verdict['text']))
    # With no stage meeting the rate rule (the decoder's output waits 20%,
    # cmd_gen_1 moves on 80%), the backpressure rule must still name the
    # decoder: co, cs and cd are one stall, and co's consumer is only the
    # wrapper around them all.
    worse = copy.deepcopy(summary)
    for h in worse['handshakes']:
        if h['key'] == 'datapath_inst/el':
            h['moved'], h['blocked'], h['empty'] = 0.80, 0.20, 0.0
        if h['key'] == 'datapath_inst/c1':
            h['moved'], h['blocked'], h['empty'] = 0.80, 0.0, 0.20
    verdict = analyse.probe_verdict(worse)
    check('probe: tied waiting goes to the most downstream inner stage, not the wrapper',
          verdict and verdict['kind'] == 'backpressure'
          and verdict['limit'] == 'datapath_inst/main_dec_inst', str(verdict and verdict['text']))
    check('probe: the loop\'s short phrase names the decoder too',
          'main_dec_inst' in loop.limit_phrase(verdict), loop.limit_phrase(verdict))


def test_probe_verdict_bug_is_harmless():
    tmp = _fresh('harmless')
    _i35_like(tmp)
    summary = probe.summarise(probe.read(tmp))
    report = {'binding': 'output port', 'binding_utilisation': 0.6, 'stale_ceilings': [],
              'output_idle_pct': 40.0, 'probe': analyse.probe_verdict(summary)}

    def boom(*args):
        raise RuntimeError('a probe bug')

    with _Patch(analyse__probe_verdict=boom):
        combined = analyse.combine([report])
        lev = analyse.lever(combined)
    check('probe: a probe bug leaves the port-counter verdict as it was',
          combined and 'probe' not in combined and lev['lever'] == 'latency', str(lev))
    with _Patch(analyse___probe_lever=boom):
        lev = analyse.lever(dict(combined, probe={'kind': 'rate'}))
    check('probe: a bug in the probe lever falls back too', lev['lever'] == 'latency')


def test_probe_summary_keeps_held_out_windows_away():
    tmp = _fresh('nowindows')
    _i35_like(tmp)
    summary = probe.summarise(probe.read(tmp), keep_windows=True)
    check('probe: without .win files a summary has no window data at all',
          summary and 'windows' not in summary and 'window_limit_share' not in summary)
    win = os.path.join(tmp, 'agentic_probe.vhsnunzip_perf_tc.uut.datapath_inst.win')
    rows = ['N cs cd el c1 cm s1_cm']
    for w in range(4):
        counts = [2000, 2000, 96, 2300, 1600, 196, 4000, 0, 96, 4000, 0, 96, 4000, 0, 96,
                  4000, 0, 96]
        rows.append('W %d %s' % (w, ' '.join(str(c) for c in counts)))
    _write(win, '\n'.join(rows) + '\n')
    summary = probe.summarise(probe.read(tmp))
    share = (summary or {}).get('window_limit_share') or {}
    check('probe: windows give the share of windows in which a stage is the limit',
          share.get('datapath_inst/main_dec_inst') == 1.0 and 'windows' not in summary,
          str(share))
    both = probe.combine([summary])
    check('probe: combined totals carry no windows',
          'windows' not in both and 'window_limit_share' not in both)


def test_probe_culprits():
    info = _prep(TMP)['info']
    got = probe.culprits('/mnt/c/x/probe_rtl/a.vhd:20:3:error: no declaration', info)
    check('probe: a compiler error inside a probe block names that architecture',
          got == ['a(rtl)'], str(got))
    got = probe.culprits('ELAB_FAIL something without a file', info)
    check('probe: a log naming no probed file names every probed architecture',
          sorted(got) == ['a(rtl)', 'b(rtl)'], str(got))


def test_rtl_widths_says_what_it_defaulted():
    rtl = os.path.join(ROOT, 'rtl')
    if os.path.isdir(rtl):
        w = analyse.rtl_widths(rtl)
        check('widths: everything read from the original design is not defaulted',
              'in_bytes' not in w['defaulted'] and 'copy_slots' not in w['defaulted']
              and 'core_line_bytes' not in w['defaulted'], str(w['defaulted']))
    if os.path.isdir(DSW4_RTL):
        # DSW-4 has no NUM_CORES generic, so the core count is a default. Its
        # line width is READ, but from the legacy vhsnunzip_int_pkg.vhd it
        # still carries (16 bytes, not its real 32): `defaulted` cannot catch
        # a stale package, only a missing one.
        w = analyse.rtl_widths(DSW4_RTL)
        check('widths: DSW-4\'s core count, which it does not declare, is reported as defaulted',
              w['defaulted'] == ['cores'], str(w['defaulted']))
    w = analyse.rtl_widths(os.path.join(TMP, 'no-such-rtl'))
    check('widths: an empty folder reports every width as defaulted',
          'in_bytes' in w['defaulted'] and 'elements_per_transfer' in w['defaulted'])


def test_frozen_files_unchanged():
    problems = freeze.check()
    check('freeze: every frozen file is as recorded', not problems, '; '.join(problems))
    import json
    with open(freeze.HASHES, encoding='utf-8') as fil:
        recorded = json.load(fil)
    check('freeze: probe.py is frozen and recorded',
          'probe.py' in freeze.FROZEN and 'probe.py' in recorded)


# ---------------------------------------------------------------------------
# slow: the instrumented i35 compiles

def test_probe_instrumented_i35_compiles():
    if os.environ.get('AGENTIC_FAST_TESTS'):
        print('skip  instrumented i35 compile (AGENTIC_FAST_TESTS)')
        return
    rtl = DIAG_RTL if os.path.isdir(DIAG_RTL) else os.path.join(ROOT, 'rtl')
    import tools
    try:
        alive = 'GHDL' in tools.eda_shell('ghdl --version', timeout=60).text
    except Exception:
        alive = False
    if not alive:
        print('skip  instrumented i35 compile (no EDA shell)')
        return
    build = _fresh('compile')
    out = os.path.join(build, 'probe_rtl')
    probe.instrument(rtl, out)
    for name in os.listdir(out):           # as sim_draws.sh: never the synthesis RAM
        if name.endswith('.syn.vhd'):
            os.remove(os.path.join(out, name))
    # The clash fixture rides along: names like line and write must not
    # collide with what the probe declares.
    clash_out = os.path.join(build, 'clash_rtl')
    os.makedirs(clash_out)
    _write(os.path.join(clash_out, 'clash.vhd'), _CLASH)
    probe.instrument(clash_out, os.path.join(build, 'clash_probe'), top='clash')
    shutil.copyfile(os.path.join(build, 'clash_probe', 'clash.vhd'),
                    os.path.join(out, 'zz_clash.vhd'))
    script = ('cd "%s" && ghdl -i --std=08 "%s"/*.vhd "%s" > a.log 2>&1 && '
              'ghdl -m --std=08 vhsnunzip_perf_tc > e.log 2>&1 && '
              'ghdl -a --std=08 "%s/zz_clash.vhd" >> e.log 2>&1 && echo ELAB_OK; '
              'tail -n 5 a.log e.log'
              % (measure.shell_path(build), measure.shell_path(out),
                 measure.shell_path(measure.TB_FILE), measure.shell_path(out)))
    res = tools.eda_shell(script, timeout=600)
    check('probe: the instrumented i35 (and a design naming signals line/write) compiles '
          'and elaborates', 'ELAB_OK' in res.text, res.text[-600:])


def main():
    for name, func in sorted(globals().items()):
        if name.startswith('test_') and callable(func):
            func()
    print()
    if FAILED:
        print('%d check(s) failed: %s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('all checks pass')
    return 0


if __name__ == '__main__':
    sys.exit(main())
