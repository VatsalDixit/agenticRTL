#!/usr/bin/env python3
"""
Checks for the parts of the kit that decide what a session is told.

These are the failures that cost real money in earlier campaigns: a notebook
that demoted a skill when the learner only wanted to add a note, a strategy
rewritten into its own opposite, an analyser that pooled two element kinds and
so never saw a saturated stage. None of this needs a model or a simulator, so
it runs in a second.

    python agentic/test_kit.py
"""

import json
import os
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
sys.path.insert(0, KIT)

import analyse                                       # noqa: E402
import guide as guide_mod                            # noqa: E402
import measure                                       # noqa: E402
import skills as skills_mod                          # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    if condition:
        print('ok    %s' % name)
    else:
        FAILED.append(name)
        print('FAIL  %s%s' % (name, ('  -- ' + detail) if detail else ''))


def seed():
    return skills_mod.load(os.path.join(KIT, 'skills.json'))


# ---------------------------------------------------------------------------
# the notebook

def test_note_only_keeps_the_rating():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['confidence']
    skills_mod.apply_updates(data, [{'id': sk['id'], 'note': 'lost once here'}], 3)
    check('a note-only update does not change the rating',
          sk['confidence'] == before, '%s -> %s' % (before, sk['confidence']))


def test_unknown_confidence_keeps_the_rating():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['confidence']
    skills_mod.apply_updates(data, [{'id': sk['id'], 'confidence': 'unchanged',
                                     'note': 'still fine'}], 3)
    check('an unreadable rating is not treated as "low"',
          sk['confidence'] == before, '%s -> %s' % (before, sk['confidence']))


def test_named_rating_still_works():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    skills_mod.apply_updates(data, [{'id': sk['id'], 'confidence': 'medium',
                                     'note': 'measured worse twice'}], 3)
    check('a named rating still moves one step', sk['confidence'] == 'medium',
          sk['confidence'])


def test_strategy_is_not_overwritten():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['strategy']
    skills_mod.apply_updates(data, [{
        'id': sk['id'],
        'strategy': 'Be very cautious about widening the line, it cost area '
                    'for nothing the one time it was tried on this design.'}], 3)
    check('advice is kept and the rewrite becomes a caveat',
          sk['strategy'] == before and len(sk['caveats']) == 1)


def test_strategy_replaced_only_with_evidence():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = sk['strategy']
    skills_mod.apply_updates(data, [{'id': sk['id'], 'replace_strategy': True,
                                     'strategy': 'x' * 200}], 3)
    kept = sk['strategy'] == before
    sk['adopted'] = 1
    skills_mod.apply_updates(data, [{'id': sk['id'], 'replace_strategy': True,
                                     'strategy': 'y' * 200}], 4)
    check('a rewrite needs an adoption behind it',
          kept and sk['strategy'].startswith('y') and sk.get('strategy_history'))


def test_rules_are_never_counted():
    data = seed()
    rule = skills_mod.by_id(data, 'chunk-boundary-flush-invariant')
    check('the invariants are rules', rule.get('kind') == 'rule')
    skills_mod.record_outcome(data, [rule['id']], passed=True, adopted=False,
                              advantage=-1.0)
    check('a rule gets no counters from a candidate that cited it',
          rule['tried'] == 0 and not rule['advantages'])


def test_two_candidate_advantage_is_dropped():
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    skills_mod.record_outcome(data, [sk['id']], passed=True, adopted=False,
                              advantage=-1.0, count_advantage=False)
    check('a two-candidate coin flip is not recorded as evidence',
          sk['tried'] == 1 and not sk['advantages'])


def test_notes_are_clean():
    data = seed()
    long_note = ('i7: i7: c1 lost again. ' + 'The mechanism deepens the '
                 'decoder recurrence and costs more clock than the extra '
                 'element buys back. ' * 4)
    skills_mod.apply_updates(data, [{'id': 'raise-bytes-per-command',
                                     'note': long_note}], 7)
    note = skills_mod.by_id(data, 'raise-bytes-per-command')['notes'][-1]
    check('a note carries one iteration number and ends at a sentence',
          note.startswith('i7: c1 lost') and note.rstrip().endswith('.'), note[:80])


def test_render_has_rules_and_no_coin_flips():
    data = seed()
    text = skills_mod.format_for_prompt(data, max_entries=30, notes_per_skill=1)
    check('the rules block is rendered once, before the mechanisms',
          text.count('RULES (these always apply') == 1
          and text.index('RULES') < text.index('HIGH confidence'))
    check('mean advantage is gone from the brief',
          'mean advantage' not in text)
    check('the library is still small', len(text) // 4 < 4000,
          '%d tokens' % (len(text) // 4))


# ---------------------------------------------------------------------------
# the analyser

def test_copy_and_literal_lanes():
    state_path = os.path.join(ROOT, '.agentic', 'runs', 'campaign2', 'state.json')
    rtl = os.path.join(ROOT, '.agentic', 'runs', 'campaign2', 'base', 'rtl')
    if not (os.path.exists(state_path) and os.path.isdir(rtl)):
        print('skip  copy/literal replay (campaign2 records not present)')
        return
    widths = analyse.rtl_widths(rtl)
    with open(state_path, encoding='utf-8') as fil:
        best = json.load(fil)['best']['metrics']
    reports, lineitem = [], None
    for draw in best['draws']:
        counters = draw.get('counters')
        cs_tv = os.path.join(ROOT, '.agentic', 'corpus', draw['name'], 'cs.tv')
        if not counters or not os.path.exists(cs_tv):
            continue
        rep = analyse.analyse(counters, cs_tv, widths)
        if draw['scored']:
            reports.append(rep)
        if draw['name'] == 'train-lineitem':
            lineitem = rep
    if not reports:
        print('skip  copy/literal replay (no counters saved)')
        return
    lever = analyse.lever(analyse.combine(reports), best.get('wns_ns'))
    check('the saturated copy lane is what the lever names',
          lever['lever'] == 'width' and 'copy slot' in lever['reason'], str(lever))
    check('the visible draw still reads as it did',
          lineitem and abs(lineitem['bytes_per_cycle'] - 18.898) < 0.01)


# ---------------------------------------------------------------------------
# the design guide

def test_guide():
    rtl = os.path.join(ROOT, '.agentic', 'runs', 'campaign2', 'base', 'rtl')
    if not os.path.isdir(rtl):
        rtl = os.path.join(ROOT, 'rtl')
    if not os.path.isdir(rtl):
        print('skip  guide (no rtl/ to describe)')
        return
    text = guide_mod.build_guide(rtl)
    files = [f for f in os.listdir(rtl) if f.endswith('.vhd')]
    check('every RTL file is in the guide',
          all(('rtl/' + f) in text for f in files), '%d files' % len(files))
    check('the guide is small enough to paste into every brief',
          len(text) // 4 < 6000, '%d tokens' % (len(text) // 4))
    check('it gives no advice', 'should' not in text.lower().split('## files')[0])
    # every line number must land on the block it names
    bad = []
    for name in files:
        info = guide_mod.file_map(rtl, name)
        lines = guide_mod.read(os.path.join(rtl, name)).splitlines()
        for kind, label, start, _end in info['blocks']:
            if label not in lines[start - 1]:
                bad.append('%s:%d is not %s' % (name, start, label))
    check('every line number points at the block it names', not bad, '; '.join(bad[:3]))


# ---------------------------------------------------------------------------
# the timing report

def test_critical_path_naming(tmp=None):
    tmp = tmp or os.path.join(ROOT, '.agentic', 'ktest')
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, 'dffs.txt'), 'w', encoding='utf-8') as fil:
        fil.write('  cell \\DFF_X1 $auto$ff.cc:337:slice$36578\n'
                  '    connect \\CK \\clk\n'
                  '    connect \\D $flatten\\datapath_inst.\\x$3329\n'
                  '    connect \\Q \\datapath_inst.cmd_gen_2_inst.proc.c1h [0]\n'
                  '    connect \\QN $auto$dfflibmap.cc:539:dfflibmap$238014\n'
                  '  end\n'
                  '  cell \\DFF_X1 $auto$ff.cc:337:slice$11858\n'
                  '    connect \\CK \\clk\n'
                  '    connect \\D $auto$rtlil.cc:3510:MuxGate$219305\n'
                  '    connect \\Q \\datapath_inst.cmd_gen_2_inst.proc.li_off [5]\n'
                  '    connect \\QN $auto$dfflibmap.cc:539:dfflibmap$238916\n'
                  '  end\n')
    text = ('ABC: Start-point = pi19775 ($auto$dfflibmap.cc:539:dfflibmap$238014).'
            '  End-point = po18498 ($auto$rtlil.cc:3510:MuxGate$219305).')
    found = measure.critical_path(tmp, text)
    check('the worst path is named in RTL terms',
          found and found['from'].endswith('c1h [0]')
          and found['to'].endswith('li_off [5]'), str(found))


# ---------------------------------------------------------------------------
# the Vivado backend (agentic/hacc.py), without the host

# Excerpts of real reports from the U55C, 2026-09-26: the rows and shapes the
# parser has to find, and nothing else.
_UTIL = """\
| Device       : xcu55c-fsvh2892-2L-e
| Design State : Routed
| CLB LUTs                   | 15073 |     0 |          0 |   1303680 |  1.16 |
| CLB Registers              |  5687 |     0 |          0 |   2607360 |  0.22 |
| Block RAM Tile    |   58 |     0 |          0 |      2016 |  2.88 |
| URAM              |   16 |     0 |          0 |       960 |  1.67 |
| DSPs      |    0 |     0 |          0 |      9024 |  0.00 |
"""
_TIMING = """\
    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)
    -------      -------  ---------------------  -------------------      -------
     -0.501     -208.636                   1141                22940        0.019
"""
_PATHS = """\
Slack (VIOLATED) :        -0.501ns  (required time - arrival time)
  Source:                 core_gen[0].core_inst/ram_gen[2].ram_even_inst/uram_gen.uram_inst/CLK
                            (rising edge-triggered cell URAM288_BASE clocked by clk)
  Destination:            core_gen[0].core_inst/datapath_inst/long_decoder_gen.main_dec_inst/proc.off_reg[7]/D
"""


def test_vivado_reports_parse(tmp=None):
    import hacc
    tmp = tmp or os.path.join(ROOT, '.agentic', 'ktest', 'vivado')
    os.makedirs(tmp, exist_ok=True)
    for name, text in (('utilization.log', _UTIL), ('timing.log', _TIMING),
                       ('critical_paths.log', _PATHS)):
        with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fil:
            fil.write(text)
    m = hacc.parse_reports(tmp)
    check('Vivado utilisation is read',
          (m.get('luts'), m.get('regs'), m.get('bram'), m.get('uram'))
          == (15073, 5687, 58, 16), str(m))
    check('Vivado area is LUTs, labelled as such',
          m.get('area') == 15073 and m.get('area_unit') == 'LUTs'
          and m.get('synth_backend') == 'hacc', str(m))
    check('f_max is 1000 / (period - WNS)',
          m.get('wns_ns') == -0.501 and m.get('f_max_mhz') == 222.2, str(m))
    check('failing endpoints are counted',
          (m.get('failing_endpoints'), m.get('total_endpoints')) == (1141, 22940), str(m))
    cp = m.get('critical_path') or {}
    check('the Vivado worst path is named in RTL terms',
          cp.get('from') == 'core_gen[0].core_inst.ram_gen[2].ram_even_inst.uram_gen.uram_inst'
          and cp.get('to', '').endswith('main_dec_inst.proc.off[7]'), str(cp))


def test_fixed_ram_refuses_a_wider_memory(tmp=None):
    import hacc
    import shutil
    tmp = tmp or os.path.join(ROOT, '.agentic', 'ktest', 'ramguard')
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    pkg = os.path.join(ROOT, 'rtl', 'vhsnunzip_int_pkg.vhd')
    with open(pkg, encoding='utf-8') as fil:
        text = fil.read()
    dst = os.path.join(tmp, 'vhsnunzip_int_pkg.vhd')
    with open(dst, 'w', encoding='utf-8') as fil:
        fil.write(text)
    check('the design as it is fits the fixed RAM',
          hacc.ram_interface_problem(tmp) is None, str(hacc.ram_interface_problem(tmp)))
    with open(dst, 'w', encoding='utf-8') as fil:
        fil.write(text.replace('wdat          : byte_array(0 to 7)',
                               'wdat          : byte_array(0 to 15)'))
    check('a widened RAM record is refused, not silently truncated',
          hacc.ram_interface_problem(tmp) is not None)
    with open(dst, 'w', encoding='utf-8') as fil:
        fil.write(text)
    with open(os.path.join(tmp, 'my_ram.vhd'), 'w', encoding='utf-8') as fil:
        fil.write('entity vhsnunzip_ram is\nend vhsnunzip_ram;\n')
    check('a candidate that redeclares the RAM entity is refused',
          hacc.ram_interface_problem(tmp) is not None)


def _scored(metrics, parent, goal='throughput'):
    import loop
    return loop.evaluate(dict(metrics, oracle_pass=True), parent,
                         {'metric': goal, 'target_pct': None, 'text': ''})


def test_backends_are_never_compared():
    yosys = {'throughput_gbps': 5.0, 'bytes_per_cycle': 8.0, 'f_max_mhz': 625.0,
             'area_um2': 100000.0}
    vivado = {'throughput_gbps': 2.0, 'bytes_per_cycle': 8.0, 'f_max_mhz': 250.0,
              'area': 15000, 'area_unit': 'LUTs', 'synth_backend': 'hacc'}
    ev = _scored(vivado, yosys)
    check('a Vivado candidate is not scored against a Yosys parent',
          ev['outcome'] == 'failed_synth' and not ev['adoptable'], str(ev))


def test_place_and_route_noise_floor():
    parent = {'throughput_gbps': 2.0, 'bytes_per_cycle': 8.0, 'f_max_mhz': 250.0,
              'area': 15000, 'area_unit': 'LUTs', 'synth_backend': 'hacc'}
    clock_only = dict(parent, f_max_mhz=251.5, throughput_gbps=2.012)
    ev = _scored(clock_only, parent)
    check('a +0.6% gain from f_max alone is inside place-and-route noise',
          ev['outcome'] == 'no_gain', str(ev))
    bytes_carried = dict(parent, bytes_per_cycle=8.05, throughput_gbps=2.0125)
    ev = _scored(bytes_carried, parent)
    check('a +0.6% gain carried by bytes/cycle still counts',
          ev['outcome'] == 'candidate', str(ev))
    unplaced = {'area_um2', 'area', 'area_unit', 'synth_backend'}
    yosys_parent = dict({k: v for k, v in parent.items() if k not in unplaced},
                        area_um2=15000.0)
    yosys_cand = dict({k: v for k, v in clock_only.items() if k not in unplaced},
                      area_um2=15000.0)
    ev = _scored(yosys_cand, yosys_parent)
    check('Yosys, having no placer, keeps its small-gain floor',
          ev['outcome'] == 'candidate', str(ev))


def test_area_of_reads_old_runs():
    import tools
    check('area falls back to area_um2 for runs recorded before `area` existed',
          tools.area_of({'area_um2': 90188.0}) == 90188.0
          and tools.area_unit({'area_um2': 90188.0}) == 'um2'
          and tools.backend_of({'area_um2': 90188.0}) == 'yosys')


# ---------------------------------------------------------------------------
# a simulator killed by the operating system is not a failing design

def test_os_kill_is_told_apart():
    check('rc 137 without a deadlock is an outside kill',
          measure.os_killed(137, False))
    check('a design error, a hang or a pass is not',
          not measure.os_killed(1, False) and not measure.os_killed(0, True)
          and not measure.os_killed(0, False) and not measure.os_killed(137, True))


def test_measure_error_is_not_a_correctness_verdict():
    import loop
    parent = {'throughput_gbps': 2.0, 'bytes_per_cycle': 8.0, 'f_max_mhz': 250.0,
              'area': 15000, 'area_unit': 'LUTs', 'synth_backend': 'hacc'}
    ev = loop.evaluate({'oracle_pass': False, 'measure_error': 'killed on held-part'},
                       parent, {'metric': 'throughput', 'target_pct': None, 'text': ''})
    check('a killed measurement scores as measure_error, not failed_correctness',
          ev['outcome'] == 'measure_error' and not ev['adoptable'], str(ev))
    data = seed()
    sk = skills_mod.by_id(data, 'raise-bytes-per-command')
    before = (sk.get('tried') or 0, sk.get('passed') or 0)
    loop.record_outcomes(data, [{'commit': 'abc', 'outcome': 'measure_error',
                                 'truncated': False, 'primary_skill': sk['id'],
                                 'direction': {}, 'adopted': False, 'advantage': None,
                                 'score': None}])
    check('a killed measurement moves no skill counter',
          (sk.get('tried') or 0, sk.get('passed') or 0) == before)


# ---------------------------------------------------------------------------
# the stimulus is whole row groups, and the measurement overlaps synthesis

def test_real_draws_are_whole_row_groups():
    import stim
    draws = stim.all_draws()
    scored = [d.name for d in draws if d.scored]
    check('the score is taxi plus the six held-out TPC-H tables',
          scored == ['train-taxi'] + ['held-%s' % t for t in stim.HELD_OUT_TABLES]
          and [d.name for d in draws if d.scored and d.visible] == ['train-taxi'],
          str(scored))
    check('nation, region and the synthetic draws must pass but are not scored',
          all(not d.scored for d in draws
              if d.kind == 'synthetic' or d.table in stim.SMALL_TABLES))
    try:
        stim.table_path('supplier')
    except IOError:
        print('skip  no Parquet files here, so the page reader is not exercised')
        return
    chunks, info = stim.page_chunks('supplier')
    check('every page of the row group is fed, long ones included',
          info['pages'] == len(chunks) == 8 and info['long_pages'] == 6
          and info['largest_page_bytes'] > stim.HISTORY_BYTES, str(info))


def test_usable_cores_count_bytes_not_chunks():
    shape = {'chunks': 7, 'expanded_bytes': 18225504, 'largest_chunk_bytes': 15668358}
    check('a row group whose largest page holds 86% of it leaves cores 1.16x',
          analyse.usable_cores(4, shape) == 1.16, str(analyse.usable_cores(4, shape)))
    many = {'chunks': 36, 'expanded_bytes': 3588432, 'largest_chunk_bytes': 983048}
    check('spread-out bytes still cap at the core count, and one core is one',
          analyse.usable_cores(2, many) == 2.0 and analyse.usable_cores(1, shape) == 1.0)


def test_long_selftest_chunk_wraps_the_history():
    import stim
    chunks = stim.selftest_chunks('long')
    plain = stim.decompress_raw(chunks[0])
    far = 0
    comp = chunks[0]
    i = 0
    while comp[i] & 0x80:
        i += 1
    i += 1
    while i < len(comp):
        tag = comp[i]
        if tag & 3 == 0:
            n = tag >> 2
            if n < 60:
                i, n = i + 1, n + 1
            else:
                extra = n - 59
                n = int.from_bytes(comp[i + 1:i + 1 + extra], 'little') + 1
                i += 1 + extra
            i += n
        elif tag & 3 == 1:
            far, i = max(far, ((tag >> 5) << 8) | comp[i + 1]), i + 2
        else:
            far, i = max(far, int.from_bytes(comp[i + 1:i + 3], 'little')), i + 3
    check('the self-test has one chunk three times the history, copying from its far end',
          len(chunks) == 1 and len(plain) > 3 * stim.HISTORY_BYTES - 1000
          and far >= 65000 and stim.check_raw(chunks[0]) is None,
          'chunks %d, %d bytes, furthest copy %d' % (len(chunks), len(plain), far))


def test_synthesis_runs_alongside_the_long_draws():
    import tempfile
    import threading

    class Draw(object):
        kind, scored, visible = 'real', True, True

        def __init__(self, name):
            self.name = name

    draws = [(Draw('small'), 'quick', 1), (Draw('long'), 'slow', 1)]
    saved = (measure.simulate, measure.synthesize, measure.draw_bytes,
             measure.analyse.rtl_widths)

    def run(fail_on, host_lost=False):
        events, started = [], threading.Event()

        def fake_sim(rtl, build, ds, widths, timeout=None, jobs=None):
            names = [d.name for d, _p, _c in ds]
            events.append('sim ' + '+'.join(names))
            if jobs != 4:
                events.append('jobs %s, not the 4 asked for' % jobs)
            if 'long' in names:
                events.append('synthesis running' if started.wait(10)
                              else 'synthesis not running')
            return [{'name': n, 'kind': 'real', 'scored': True, 'visible': True,
                     'chunks': 1, 'oracle_pass': n != fail_on, 'bytes_per_cycle': 5.0,
                     'problem': 'wrong output: x' if n == fail_on else None}
                    for n in names]

        def fake_synth(rtl, out):
            started.set()
            events.append('synth')
            if host_lost:
                raise measure.HostUnreachable('ssh to host failed: could not resolve')
            return {'f_max_mhz': 250.0, 'wns_ns': 0.0, 'area': 1000,
                    'area_unit': 'LUTs', 'synth_backend': 'hacc'}

        measure.simulate, measure.synthesize = fake_sim, fake_synth
        measure.draw_bytes = lambda e: 10 if e[1] == 'quick' else 10 ** 8
        measure.analyse.rtl_widths = lambda rtl: {}
        try:
            out = measure.measure('rtl', tempfile.mkdtemp(prefix='kit-measure-'), draws,
                                  jobs=4)
        finally:
            (measure.simulate, measure.synthesize, measure.draw_bytes,
             measure.analyse.rtl_widths) = saved
        return out, events

    out, events = run(None)
    check('small draws first, then synthesis runs while the long draws simulate',
          events[0] == 'sim small' and 'synthesis running' in events
          and out.get('throughput_gbps') == 1.25
          and not any(e.startswith('jobs') for e in events),
          '%s %s' % (events, out.get('throughput_gbps')))
    out, events = run('small')
    check('a design failing a small draw gets no synthesis and no long draws',
          events == ['sim small'] and 'f_max_mhz' not in out and not out['oracle_pass'],
          str(events))
    out, events = run('long')
    check('a design failing a long draw keeps no synthesis numbers',
          'synth' in events and 'f_max_mhz' not in out and not out['oracle_pass'],
          str(events))
    out, events = run(None, host_lost=True)
    check('a correct design whose host stayed away is not measured, not failed',
          out.get('measure_error') and 'synth_error' not in out and out['oracle_pass'],
          str({k: out.get(k) for k in ('measure_error', 'synth_error', 'oracle_pass')}))


def test_a_retry_is_never_retried():
    import loop
    old = {'outcome': 'too_expensive', 'branch': 'agentic-cand/r/i9-c1',
           'id': 'thirtytwo-byte-line', 'measured': {'gain_pct': 5.76}}
    check('a rejected candidate with a large gain gets one retry',
          loop.retry_eligible(old, []) and not loop.retry_eligible(old, [old['branch']]))
    again = dict(old, branch='agentic-cand/r/i10-r1', id='retry-thirtytwo-byte-line')
    check('the retry itself is not retried after the next adoption',
          not loop.retry_eligible(again, []))
    small = dict(old, measured={'gain_pct': 3.0})
    check('a small gain is not retried', not loop.retry_eligible(small, []))


def test_a_lost_host_is_not_a_design_verdict():
    import loop
    import hacc
    # What iteration 16 of hacc-real200 recorded while the VPN was down.
    lost = {'outcome': 'failed_synth', 'branch': 'agentic-cand/r/i16-c2', 'id': 'port',
            'measured': {}, 'reason': 'ssh to vdixit@hacc-build-02 failed: ssh: Could '
                                      'not resolve hostname hacc-build-02: No such host is known.'}
    broken = dict(lost, reason='vivado failed on vdixit@hacc-build-02:\nERROR: [Synth 8-439] '
                               'module not found')
    check('a synthesis lost to the host is measured again; one Vivado rejected is not',
          loop.never_measured(lost) and loop.retry_eligible(lost, [])
          and not loop.never_measured(broken) and not loop.retry_eligible(broken, []))

    calls, saved = [], (hacc.run_remote, hacc.parse_reports, hacc.time.sleep,
                        hacc.ram_interface_problem, hacc.CONFIG.get('hacc_wait_min'))

    def flaky(rtl, out, top, limit):
        calls.append(1)
        if len(calls) < 3:
            raise hacc.Transient('ssh to host failed: could not resolve hostname')

    hacc.run_remote, hacc.parse_reports = flaky, lambda out: {'f_max_mhz': 250.0}
    hacc.time.sleep, hacc.ram_interface_problem = (lambda s: None), (lambda rtl: None)
    try:
        hacc.CONFIG['hacc_wait_min'] = 120
        got = hacc.synthesize('rtl', 'out', 'top')
        waited = got.get('synth_attempts') == 3
        calls[:] = []
        hacc.CONFIG['hacc_wait_min'] = 0
        try:
            hacc.synthesize('rtl', 'out', 'top')
            gave_up = False
        except hacc.Transient:
            gave_up = len(calls) == 1
    finally:
        (hacc.run_remote, hacc.parse_reports, hacc.time.sleep,
         hacc.ram_interface_problem, hacc.CONFIG['hacc_wait_min']) = saved
    check('synthesis waits out a lost host, and gives up only when the wait is over',
          waited and gave_up)

    parent = {'throughput_gbps': 2.0, 'bytes_per_cycle': 8.0, 'f_max_mhz': 250.0,
              'area': 2400, 'area_unit': 'LUTs', 'synth_backend': 'hacc'}
    ev = loop.evaluate({'oracle_pass': True, 'bytes_per_cycle': 9.0,
                        'measure_error': 'the synthesis host could not be reached'},
                       parent, {'metric': 'throughput', 'target_pct': None, 'text': ''})
    check('a design never synthesised is not measured, not failed',
          ev['outcome'] == 'measure_error' and not ev['adoptable'], str(ev))


def test_simulators_are_shared_out():
    import loop
    saved = loop.CONFIG['sim_slots']
    loop.CONFIG['sim_slots'] = 8
    try:
        got = [loop.sim_jobs(n) for n in (1, 2, 3)]
    finally:
        loop.CONFIG['sim_slots'] = saved
    check('eight simulator slots: 8 for the baseline, 4 each for two, never under 3',
          got == [8, 4, 3], str(got))


# ---------------------------------------------------------------------------
# loop v3, change 1: throughput alone decides; area is shown, never scored

RUN200 = os.path.join(ROOT, '.agentic', 'runs', 'hacc-real200')
KTEST = os.path.join(ROOT, '.agentic', 'ktest', 'v3')


def _run200_state():
    """A deep copy of hacc-real200's state, read only; None when absent."""
    path = os.path.join(RUN200, 'state.json')
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as fil:
        return json.load(fil)


def _run200_roadmap():
    """hacc-real200's v2 roadmap: roadmap.md, or the copy it was retired to
    when v3 resumed the run without it."""
    for name in ('roadmap.md', 'roadmap.md.v2-retired'):
        path = os.path.join(RUN200, name)
        if os.path.isfile(path):
            return path
    return os.path.join(RUN200, 'roadmap.md')


def _vivado(bpc, gbps, luts):
    return {'throughput_gbps': gbps, 'bytes_per_cycle': bpc, 'f_max_mhz': 250.0,
            'area': luts, 'area_unit': 'LUTs', 'synth_backend': 'hacc'}


def test_area_is_not_scored():
    parent = _vivado(8.0, 2.0, 3577)
    ev = _scored(_vivado(8.4, 2.1, 6833), parent)
    check('v3: +5% throughput at +91% area is an adoptable candidate',
          ev['outcome'] == 'candidate' and ev['adoptable'], str(ev))
    check('v3: the score is exactly -gain%',
          ev['score'] == round(-ev['measured']['gain_pct'], 4), str(ev))
    check('v3: the measured keys are the gains and nothing derived from area',
          set(ev['measured']) == {'throughput_gain_pct', 'bpc_gain_pct', 'fmax_gain_pct',
                                  'area_gain_pct', 'gain_pct'}, str(sorted(ev['measured'])))


def test_huge_area_small_gain_still_adoptable():
    ev = _scored(_vivado(8.04, 2.01, 3577 * 8), _vivado(8.0, 2.0, 3577))
    check('v3: +0.5% (carried by bytes/cycle) at +700% area is still adoptable',
          ev['outcome'] == 'candidate' and ev['adoptable'], str(ev))


def test_old_area_keys_are_inert():
    import loop
    parent, cand = _vivado(8.0, 2.0, 3577), _vivado(8.4, 2.1, 6833)
    before = _scored(cand, parent)
    saved = dict((k, loop.CONFIG.get(k)) for k in ('area_weight', 'max_area_growth_pct',
                                                   'ei_floor'))
    loop.CONFIG.update(area_weight=9, max_area_growth_pct=1.0, ei_floor=100.0)
    try:
        after = _scored(cand, parent)
    finally:
        for k, v in saved.items():
            if v is None:
                loop.CONFIG.pop(k, None)
            else:
                loop.CONFIG[k] = v
    check('v3: old area keys in a config change nothing',
          after['score'] == before['score'] and after['outcome'] == before['outcome'],
          '%s vs %s' % (before, after))


def test_no_efficiency_or_per_lut_key():
    import re as _re
    import loop
    import tools
    bad = []
    for name in ('loop.py', 'propose.py', 'report.py', 'gui.py', 'learn.py', 'analyse.py',
                 'probe.py', 'packmodel.py'):
        path = os.path.join(KIT, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as fil:
            for no, line in enumerate(fil, 1):
                # The one allowed mention: the fixed note that tells the
                # planner how to read area-era skill notes.
                if 'AREA_ERA_NOTE' in line or 'written under the area rule' in line:
                    continue
                if _re.search(r'efficiency|per[_ -]?lut|ei_floor|EI floor', line, _re.I):
                    bad.append('%s:%d' % (name, no))
    check('v3: no efficiency, per-LUT or EI-floor term in the kit', not bad, ', '.join(bad))
    with open(os.path.join(KIT, 'loop.py'), encoding='utf-8') as fil:
        src = fil.read()
    check('v3: the loop never assigns the outcome too_expensive',
          not _re.search(r"""outcome['"]?\]?\s*[=:]\s*['"]too_expensive""", src)
          and "outcome='too_expensive'" not in src)
    check('v3: the area keys are gone from the defaults',
          not any(k in tools.DEFAULT_CONFIG
                  for k in ('area_weight', 'max_area_growth_pct', 'ei_floor')))
    check('v3: the note on area-era skills is the fixed line',
          'EI floor' in loop.AREA_ERA_NOTE and 'throughput alone' in loop.AREA_ERA_NOTE)


def test_area_era_records_are_relabelled():
    import copy
    import loop
    import propose
    state = _run200_state()
    if state is None:
        print('skip  area-era relabel (hacc-real200 not present)')
        return
    first = copy.deepcopy(state['iterations'][0])
    text = loop.history_text({'iterations': [first], 'retried': []})
    check('v3: an area-era too_expensive record is shown with its throughput gain',
          'area rule removed in v3' in text and '+11.3%' in text, text[:400])
    held = sorted(set(r['name'] for r in state['best']['metrics']['draws']
                      if r['name'].startswith('held-')))
    hist = loop.history_text(state)
    check('v3: the history never names a held-out draw or quotes page bytes',
          not [n for n in held if n in hist] and 'got 6865' not in hist
          and '<bytes withheld>' in loop.withheld('held-x: got 6865207265672400646920717569636b')
          and 'held-x' not in loop.withheld('held-x: wrong output'))
    skills = skills_mod.load(os.path.join(RUN200, 'skills.json'))
    ctx = loop.build_ctx(state, skills, len(state['iterations']) + 1)
    plan = propose.build_plan_prompt(ctx, 2)
    brief = propose.build_user_prompt(ctx, dict(propose.FALLBACK_DIRECTIONS[0]), [])
    check('v3: the planner and the session read the area-era note before the skills',
          loop.AREA_ERA_NOTE in plan and loop.AREA_ERA_NOTE in brief
          and plan.index(loop.AREA_ERA_NOTE) < plan.index(ctx['skills_text'][-40:]))


def test_a_trailing_comma_does_not_lose_the_plan():
    import propose
    # The shape of hacc-real200 i69's planner reply, which fell back to the
    # generic directions.
    text = ('Here is the plan:\n{"directions": [\n  {"focus": "a, b", "x": "],}",\n'
            '   "modelled_gain_pct": 98},\n  ],\n "note": "n"}')
    data = propose.extract_json(text)
    check('a trailing comma in a reply is tolerated',
          isinstance(data, dict) and len(data.get('directions', [])) == 1, str(data)[:200])
    check('commas inside strings are left alone',
          data is not None and data['directions'][0]['focus'] == 'a, b'
          and data['directions'][0]['x'] == '],}', str(data)[:200])
    check('valid JSON is unchanged and garbage is still None',
          propose.extract_json('{"a": [1, 2]}') == {'a': [1, 2]}
          and propose.extract_json('no json here') is None)


def test_plan_prompt_has_no_override_rule():
    import propose
    ctx = {'goal_text': 'g', 'iteration': 1, 'max_iters': 2, 'progress_text': 'p',
           'state_text': 's', 'profile_text': 'pr', 'lever': {'lever': 'width', 'reason': 'r'},
           'roadmap_text': 'STEP 1 [open; unmodelled]: x', 'skills_text': 'sk',
           'facts_text': '', 'history_text': '', 'packmodel_text': 'PMTEXT 19.3 B/cycle'}
    plan = propose.build_plan_prompt(ctx, 2)
    gone = [p for p in ('MUST carry out', 'overrides the library', 'Area is priced',
                        'area_growth') if p in plan or p in propose.SYSTEM_BRIEF]
    check('v3: no compulsory-roadmap rule and no area price in the prompts', not gone,
          ', '.join(gone))
    check('v3: the roadmap is advice and dropped steps are not assigned',
          'not an\n  order' in plan and 'DROPPED steps must not be assigned' in plan
          and 'AVOID marks hold for roadmap steps too' in plan)
    check('v3: the plan prompt has the packing model after LEVER',
          plan.index('LEVER:') < plan.index(propose.PACKMODEL_HEADER)
          < plan.index('PMTEXT') < plan.index('ROADMAP FROM THE ENGINEER'))
    check('v3: directions carry roadmap_step and modelled_gain_pct',
          '"roadmap_step"' in plan and '"modelled_gain_pct"' in plan)
    check('v3: the session brief says area is shown, not scored',
          'Area is measured and shown, not scored' in propose.SYSTEM_BRIEF
          and '"dispute"' in propose.SYSTEM_BRIEF)


# ---------------------------------------------------------------------------
# loop v3, change 6: the roadmap is advice, disputes and zero results drop a step

ROADMAP = """\
Target: +200%. Engineer's preamble.

STEP 1: speculative decode
  body of step one.

STEP 2: two copies per cycle [model 14.06 B/cycle]
  body of step two.

STEP 3: 16-byte input, on top of step 2 [model K4/L32/N32]

STEP 4, if step 2-3 fall short:
  a) three elements per cycle.
"""


class _FakeRun(object):
    def __init__(self, state, tmp=None):
        self.state = state
        self.lines = []
        self.saves = 0
        self.dir = tmp or KTEST
        self.base_dir = os.path.join(self.dir, 'base')
        self.name = 'ktest-v3'
        self.packmodel_cache = None

        class _St(object):
            def set(self_, **kw):
                pass
        self.status = _St()

    def log(self, msg):
        self.lines.append(str(msg))

    def save(self):
        self.saves += 1


def _roadmap_state(text=ROADMAP):
    import loop
    state = loop.state_defaults({'iterations': []})
    pre, steps = loop.parse_roadmap(text)
    loop.sync_roadmap_state(state, steps, 10.0, {'K4/L32/N32': {'all': 19.3, 'calibrated': True}})
    return state, pre, steps


def _decline(sid, label='c1', proof='the decoder already emits one element per cycle',
             status='ok', step=None, truncated=False):
    return {'label': label, 'id': 'none', 'outcome': 'no_proposal',
            'direction': {'roadmap_step': sid}, 'dispute': proof,
            'dispute_step': sid if step is None else step, 'rationale': 'build X instead',
            'notes': '## Why it worked or failed\nprobe shows el moves 99%',
            'session': {'status': status}, 'truncated': truncated}


def _measured(sid, gain, outcome='no_gain', cid='two-copies', truncated=False):
    return {'label': 'c2', 'id': cid, 'outcome': outcome, 'direction': {'roadmap_step': sid},
            'measured': {'gain_pct': gain}, 'session': {'status': 'ok'}, 'truncated': truncated}


def test_roadmap_parses_steps_and_models():
    import loop
    state, pre, steps = _roadmap_state()
    check('v3 roadmap: steps 1-4 parsed, the comma heading of step 4 included',
          [s['id'] for s in steps] == ['1', '2', '3', '4'] and pre.startswith('Target'),
          str([s['id'] for s in steps]))
    rec = state['roadmap']['steps']
    check('v3 roadmap: B/cycle and K/L/N tags give a modelled gain, untagged steps none',
          rec['2']['modelled_gain_pct'] == 40.6 and rec['3']['modelled_bpc'] == 19.3
          and rec['1']['modelled_gain_pct'] is None, str(rec))
    brief = loop.roadmap_brief(state, pre, steps)
    order = [ln.split()[1] for ln in brief.splitlines() if ln.startswith('STEP ')]
    check('v3 roadmap: ranked by modelled gain, unmodelled last in file order',
          order == ['3', '2', '1', '4'], str(order))
    check('v3 roadmap: a modelled step says so',
          'STEP 3 [open; modelled 19.3 B/cycle = +93% bytes/cycle]' in brief, brief[:300])
    check('v3 roadmap: percent and B/cycle tags are literal',
          loop.model_step('+12%', 10.0, {}) == (11.2, 12.0)
          and loop.model_step('15 B/cycle', 10.0, {}) == (15.0, 50.0)
          and loop.model_step('K4/L32/N32', 10.0,
                              {'K4/L32/N32': {'all': 19.3, 'calibrated': False}})
          == (None, None))
    tagged = [{'tag': t} for t in ('K4/L32/N32', 'K4/L33/N32', '+12%', '14 B/cycle',
                                   'garbage', 'K4/B32/N32 rules=dsw4', 'K4/L32/N32')]
    specs = loop.roadmap_specs(tagged)
    check('v3 roadmap: only tags the packing model accepts are sent to it, once each',
          specs == ['K4/L32/N32', 'K4/B32/N32 rules=dsw4'], str(specs))
    real = _run200_roadmap()
    if os.path.isfile(real):
        with open(real, encoding='utf-8') as fil:
            _pre, got = loop.parse_roadmap(fil.read())
        check('v3 roadmap: hacc-real200 gives steps 1, 2, 3 and 4, all unmodelled',
              [s['id'] for s in got] == ['1', '2', '3', '4']
              and all(s['tag'] is None for s in got), str([s['id'] for s in got]))


def test_roadmap_step_ids_normalise():
    import loop
    import propose
    same = [propose.norm_step_id(v) for v in ('STEP 2', '2', 2, ' step 2: ', 'Step 2')]
    check('v3 roadmap: "STEP 2", "2" and 2 are one id', set(same) == {'2'}, str(same))
    check('v3 roadmap: null and booleans are no id',
          propose.norm_step_id(None) == '' and propose.norm_step_id('null') == ''
          and propose.norm_step_id(True) == '')
    state, _pre, _steps = _roadmap_state()
    state['roadmap']['steps']['3']['status'] = 'dropped'
    run = _FakeRun(state)
    dirs = [{'focus': 'a', 'roadmap_step': 'STEP 2'}, {'focus': 'b', 'roadmap_step': '9'},
            {'focus': 'c', 'roadmap_step': 3}, {'focus': 'd', 'roadmap_step': None}]
    loop.vet_directions(state, dirs, run.log)
    check('v3 roadmap: unknown and dropped steps are cleared and logged, the direction kept',
          [d['roadmap_step'] for d in dirs] == ['2', None, None, None]
          and any('unknown roadmap step 9' in ln for ln in run.lines)
          and any('3, which is dropped' in ln for ln in run.lines), str(run.lines))


def test_dispute_is_recorded_and_two_drop():
    import loop
    state, pre, steps = _roadmap_state()
    run = _FakeRun(state)
    ev = loop.roadmap_events([_decline('1', step='STEP 1')], 39)
    check('v3 roadmap: a decline with a matching dispute is one dispute event',
          len(ev) == 1 and ev[0]['kind'] == 'roadmap_dispute' and ev[0]['step'] == '1'
          and 'el moves 99%' in ev[0]['proof'] and 'build X' in ev[0]['proof'], str(ev))
    loop.apply_roadmap_events(state, ev, run.log)
    rec = state['roadmap']['steps']['1']
    check('v3 roadmap: one dispute marks the step disputed',
          rec['status'] == 'disputed' and len(rec['disputes']) == 1
          and any('ROADMAP STEP 1 DISPUTED by i39 c1' in ln for ln in run.lines))
    brief = loop.roadmap_brief(state, pre, steps)
    check('v3 roadmap: the planner sees the dispute and its proof',
          'STEP 1 [DISPUTED once; unmodelled]' in brief and 'proof from i39 c1' in brief, brief)
    drops = loop.apply_roadmap_events(state, loop.roadmap_events([_decline('1', 'c2')], 41),
                                      run.log)
    banner = [i for i, ln in enumerate(run.lines) if ln.startswith('ROADMAP STEP 1 DROPPED')]
    check('v3 roadmap: a second dispute drops the step, between banner lines',
          rec['status'] == 'dropped' and rec['dropped_at'] == 41 and drops and banner
          and run.lines[banner[0] - 1].startswith('====')
          and run.lines[banner[0] + 1].startswith('===='), str(run.lines[-4:]))
    brief = loop.roadmap_brief(state, pre, steps)
    check('v3 roadmap: a dropped step is one line at the end, with its reason',
          brief.strip().splitlines()[-1].startswith('STEP 1 [DROPPED at i41 after 2 disputes'),
          brief.strip().splitlines()[-1])


def test_rationale_alone_is_not_a_dispute():
    import loop
    no_dispute = _decline('1', proof='')
    other_step = _decline('1', step='2')
    no_step = dict(_decline('1'), direction={'roadmap_step': None})
    check('v3 roadmap: a bare rationale, a dispute of another step, or no step: no event',
          loop.roadmap_events([no_dispute, other_step, no_step], 5) == [])


def test_truncated_session_is_not_a_dispute():
    import loop
    cut = [_decline('1', status='budget', truncated=True), _decline('1', status='timeout'),
           _decline('1', status='limit'), _decline('1', status='error')]
    check('v3 roadmap: a session cut off or failed does not dispute its step',
          loop.roadmap_events(cut, 5) == [])


def test_two_zero_results_drop():
    import loop
    state, _pre, _steps = _roadmap_state()
    run = _FakeRun(state)
    ignored = [_measured('2', 0.1, outcome='failed_correctness'),
               _measured('2', 0.2, truncated=True),
               _measured('2', 0.3, cid='retry-two-copies'),
               _measured('2', 4.0, outcome='candidate'),
               _measured('2', None, outcome='measure_error')]
    check('v3 roadmap: failures, truncated sessions, retries and real gains are not zero results',
          loop.roadmap_events(ignored, 7) == [])
    loop.apply_roadmap_events(state, loop.roadmap_events([_measured('2', 0.3)], 40), run.log)
    rec = state['roadmap']['steps']['2']
    first = rec['status'] == 'open' and len(rec['zero_results']) == 1
    loop.apply_roadmap_events(state, loop.roadmap_events(
        [_measured('2', -0.4, outcome='regressed')], 42), run.log)
    check('v3 roadmap: two results within 1% of zero drop the step',
          first and rec['status'] == 'dropped' and '-0.4%' in rec['drop_reason']
          and any('ROADMAP STEP 2 DROPPED' in ln for ln in run.lines), str(rec))


def test_interrupt_does_not_double_count():
    import copy
    import loop
    state, _pre, _steps = _roadmap_state()
    before = copy.deepcopy(state)
    events = loop.roadmap_events([_decline('1')], 39)
    # An interrupt here: main() saves whatever is in state. Nothing was applied.
    check('v3 roadmap: computing an iteration\'s events changes nothing in state',
          state == before and events)
    run = _FakeRun(state)
    record = {'iteration': 39, 'tokens': {}, 'tokens_cost_estimate': 0.0, 'cost_usd': 0.0,
              'timing': {}}
    loop.append_record(run, record, run.log, events=loop.roadmap_events([_decline('1')], 39))
    rec = state['roadmap']['steps']['1']
    check('v3 roadmap: the redone iteration counts its dispute once, in the same save',
          len(rec['disputes']) == 1 and run.saves == 1 and len(state['iterations']) == 1
          and state['iterations'][0]['roadmap_events'][0]['kind'] == 'roadmap_dispute')


def test_edited_dropped_step_reopens():
    import loop
    state, _pre, steps = _roadmap_state()
    run = _FakeRun(state)
    for k in (1, 2):
        loop.apply_roadmap_events(state, loop.roadmap_events([_decline('1')], k), run.log)
    dropped = state['roadmap']['steps']['1']['status'] == 'dropped'
    _pre, steps = loop.parse_roadmap(ROADMAP)
    loop.sync_roadmap_state(state, steps, 10.0, {}, run.log)
    still = state['roadmap']['steps']['1']['status'] == 'dropped'
    _pre, steps = loop.parse_roadmap(ROADMAP.replace('body of step one.', 'a better step one.'))
    loop.sync_roadmap_state(state, steps, 10.0, {}, run.log)
    rec = state['roadmap']['steps']['1']
    check('v3 roadmap: a dropped step stays dropped until the engineer edits it, then reopens',
          dropped and still and rec['status'] == 'open' and rec['disputes'] == []
          and any('open again' in ln for ln in run.lines), str(rec))
    _pre, steps = loop.parse_roadmap(ROADMAP.split('STEP 4')[0])
    loop.sync_roadmap_state(state, steps, 10.0, {}, run.log)
    check('v3 roadmap: a step removed from the file stays in state, hidden',
          '4' in state['roadmap']['steps'] and not state['roadmap']['steps']['4']['in_file'])


def test_read_proposal_keeps_dispute_fields():
    import propose
    tmp = os.path.join(KTEST, 'proposal')
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, 'PROPOSAL.json'), 'w', encoding='utf-8') as fil:
        json.dump({'id': 'none', 'roadmap_step': 'STEP 1', 'dispute': 'x' * 3000,
                   'rationale': 'build the pair issue instead'}, fil)
    prop = propose.read_proposal(tmp)
    check('v3 roadmap: read_proposal keeps roadmap_step (normalised) and dispute (capped)',
          prop and prop['roadmap_step'] == '1' and len(prop['dispute']) == 2000, str(prop)[:200])


def test_old_state_without_roadmap_or_tracks_loads():
    import shutil
    import loop
    src = os.path.join(RUN200, 'state.json')
    if not os.path.isfile(src):
        print('skip  old state load (hacc-real200 not present)')
        return
    mtime = os.path.getmtime(src)
    os.makedirs(KTEST, exist_ok=True)
    dst = os.path.join(KTEST, 'state.json')
    shutil.copyfile(src, dst)
    state = loop.read_json(dst)
    # The live run has had a roadmap record since v3 resumed it; strip it to
    # stand for the pre-v3 state this check is about.
    state.pop('roadmap', None)
    had = 'roadmap' in state
    loop.state_defaults(state)
    with open(_run200_roadmap(), encoding='utf-8') as fil:
        _pre, steps = loop.parse_roadmap(fil.read())
    loop.sync_roadmap_state(state, steps, state['best']['metrics'].get('bytes_per_cycle'), {})
    recorded = sorted(r['name'] for r in state['baseline']['draws'])

    class _D(object):
        def __init__(self, name):
            self.name = name
    draws = [(_D(n), None, None) for n in recorded]
    _same, problem = loop.corpus_guard(state, draws, False, False)
    _less, fewer = loop.corpus_guard(state, draws[1:], False, False)
    check('v3: hacc-real200\'s state loads with a roadmap of steps 1-4 added',
          not had and sorted(state['roadmap']['steps']) == ['1', '2', '3', '4']
          and all(r['status'] == 'open' for r in state['roadmap']['steps'].values()))
    check('v3: its draw names come from the baseline; another corpus is refused',
          problem is None and fewer and 'scored on draws' in fewer, str(fewer)[:200])
    check('v3: the original state.json was not touched', os.path.getmtime(src) == mtime)


def test_test_only_draws_need_a_fake_new_run():
    import loop

    class _D(object):
        def __init__(self, name):
            self.name = name
            self.scored = name.startswith('train-')
    draws = [(_D(n), None, None) for n in ('synth-512B', 'synth-8192B', 'train-taxi')]
    saved = os.environ.get('AGENTIC_DRAWS')

    def guard(state, fresh, fake, env):
        if env is None:
            os.environ.pop('AGENTIC_DRAWS', None)
        else:
            os.environ['AGENTIC_DRAWS'] = env
        return loop.corpus_guard(state, draws, fresh, fake)
    pair = 'synth-512B,synth-8192B'
    try:
        _d, real = guard({}, True, False, pair)
        _d, resumed = guard({'draw_names': ['synth-512B', 'synth-8192B']}, False, True, pair)
        new_state = {}
        cut, fine = guard(new_state, True, True, pair)
        test_run = dict(new_state, draw_names=['synth-512B', 'synth-8192B'])
        again, ok_same = guard(dict(test_run), False, True, pair)
        unset, ok_unset = guard(dict(test_run), False, True, None)
        _d, other = guard(dict(test_run), False, True, 'synth-512B')
        _d, not_fake = guard(dict(test_run), False, False, None)
        full, real_resume = guard({'draw_names': ['synth-512B', 'synth-8192B', 'train-taxi']},
                                  False, False, None)
    finally:
        if saved is None:
            os.environ.pop('AGENTIC_DRAWS', None)
        else:
            os.environ['AGENTIC_DRAWS'] = saved
    check('v3: AGENTIC_DRAWS is refused on a real run and on a resume of a run not cut by it',
          real and resumed and 'test-only' in real and 'test-only' in resumed)
    check('v3: AGENTIC_DRAWS cuts the corpus of a new --fake run, scores the draws it keeps '
          'and records the cut',
          fine is None and [d[0].name for d in cut] == ['synth-512B', 'synth-8192B']
          and all(d[0].scored for d in cut) and not draws[0][0].scored
          and new_state.get('test_draws') == ['synth-512B', 'synth-8192B'])
    check('v3: a --fake test run resumes on its own cut (AGENTIC_DRAWS the same or unset)',
          ok_same is None and ok_unset is None
          and [d[0].name for d in unset] == ['synth-512B', 'synth-8192B']
          and all(d[0].scored for d in again), '%s | %s' % (ok_same, ok_unset))
    check('v3: a test run is not resumed on other draws or without --fake; a real run is '
          'never cut',
          other and not_fake and '--fake' in not_fake and real_resume is None
          and len(full) == 3 and not full[0][0].scored, '%s | %s' % (other, not_fake))


# ---------------------------------------------------------------------------
# loop v3: the probe and the packing model in the prompts

def _probed_best():
    """hacc-real200's best with probe verdicts put in: taxi's, a held-out
    draw's (with a name only it carries) and the aggregate."""
    import copy
    import kitcheck_probe as kp
    import probe
    state = _run200_state()
    if state is None:
        return None, None
    tmp = kp._fresh('v3-prompts')
    kp._i35_like(tmp)
    verdict = analyse.probe_verdict(probe.summarise(probe.read(tmp)))
    metrics = copy.deepcopy(state['best']['metrics'])
    secret = copy.deepcopy(verdict)
    for h in secret['handshakes']:
        h['key'] = 'heldsecret_' + h['key'].replace('/', '_')
    secret['window_limit_share'] = {'datapath_inst/main_dec_inst': 0.69}
    for rec in metrics['draws']:
        if rec['name'].startswith('train-'):
            rec['analysis']['probe'] = verdict
            rec['probe'] = {'handshakes': [], 'windows': [[1, 2, 3]]}
        elif rec['name'].startswith('held-') and rec.get('analysis'):
            rec['analysis']['probe'] = secret
            rec['probe'] = {'handshakes': secret['handshakes']}
    metrics['profile']['probe'] = dict(verdict, window_limit_share={'x': 0.5})
    metrics['probe'] = {'handshakes': []}
    metrics['probe_status'] = 'on'
    return state, metrics


def test_probe_text_hides_held_out():
    import loop
    state, metrics = _probed_best()
    if metrics is None:
        print('skip  probe in the prompts (hacc-real200 not present)')
        return
    held = [r['name'] for r in metrics['draws'] if r['name'].startswith('held-')]
    for label, m in (('as measured', metrics), ('as kept for the best', loop.best_view(metrics))):
        text = loop.profile_text(m) + '\n' + loop.state_text(m, state['baseline'])
        leaks = [n for n in held if n in text] + (['heldsecret'] if 'heldsecret' in text else [])
        check('v3 probe: no held-out name or held-out probe line in the profile (%s)' % label,
              not leaks, ', '.join(leaks))
        check('v3 probe: the aggregate has no window lines (%s)' % label,
              text.count('share of 4096-cycle windows') == 0, text[-600:])
        check('v3 probe: taxi and the aggregate name the decoder (%s)' % label,
              'probe over all scored real draws' in text and 'main_dec_inst' in text
              and 'limit: datapath_inst/main_dec_inst' in text)
    kept = loop.best_view(metrics)
    hidden = [r for r in kept['draws'] if not r.get('visible')]
    taxi = [r for r in kept['draws'] if r['name'].startswith('train-')][0]
    check('v3 probe: the best keeps no held-out probe and no windows',
          all('probe' not in r and 'probe' not in (r.get('analysis') or {}) for r in hidden)
          and 'windows' not in taxi['probe'] and kept['probe_status'] == 'on')


def test_probe_is_stripped_from_records():
    import loop
    c = {'label': 'c1', 'id': 'x'}
    asg = {'worktree': os.path.join(KTEST, 'nowhere')}
    metrics = {'oracle_pass': True, 'throughput_gbps': 2.1, 'bytes_per_cycle': 8.4,
               'f_max_mhz': 250.0, 'area': 4000, 'area_unit': 'LUTs', 'synth_backend': 'hacc',
               'probe_status': 'partial (skipped x: why)',
               'draws': [{'name': 'held-x', 'probe': {'h': 1}, 'analysis': {}, 'counters': {},
                          'bytes_per_cycle': 8.0}]}
    lines = []
    loop.score_candidate(c, asg, metrics, _vivado(8.0, 2.0, 3577),
                         {'metric': 'throughput', 'target_pct': None, 'text': ''}, '',
                         os.path.join(KTEST, 'm'), lines.append)
    check('v3 probe: a candidate\'s saved draws carry no probe, analysis or counters',
          c['draws'] == [{'name': 'held-x', 'bytes_per_cycle': 8.0}], str(c['draws']))
    check('v3 probe: a probe status other than on is logged',
          any('probe partial' in ln for ln in lines), str(lines))


def test_measure_crash_is_measure_error():
    import loop
    saved = loop.measure.measure

    def boom(*a, **kw):
        raise RuntimeError('a kit bug')
    loop.measure.measure = boom
    try:
        got = loop.measure_one(('rtl', 'work', [], 1))
    finally:
        loop.measure.measure = saved
    ev = loop.evaluate(got, _vivado(8.0, 2.0, 3577),
                       {'metric': 'throughput', 'target_pct': None, 'text': ''})
    check('v3 probe: a crash inside measurement is never measured, not failed_compile',
          got.get('measure_error') and 'error' not in got and ev['outcome'] == 'measure_error'
          and loop.never_measured(dict(ev, outcome=ev['outcome'])), str(got))


class _Draw(object):
    def __init__(self, name, visible=True, scored=False):
        self.name, self.visible, self.scored = name, visible, scored


def test_probe_mismatch_blocks_adoption():
    import loop
    counters = {'cycles': 100, 'bytes_out': 800, 'co_beats': 10, 'de_beats': 50,
                'co_stall': 0, 'de_bubble': 50}
    metrics = {'probe_status': 'on', 'widths': {}, 'draws': [
        {'name': 'train-taxi', 'probe': {'h': 1}, 'counters': dict(counters)},
        {'name': 'held-x', 'counters': dict(counters)}]}
    draws = [(_Draw('train-taxi'), 'p', 'c'), (_Draw('held-x', False, True), 'p', 'c')]
    seen = []

    def clean(rtl, bdir, ds, widths, jobs=None, probe_rtl=None):
        seen.append(([d[0].name for d in ds], probe_rtl.get('probed')))
        return [{'name': 'train-taxi', 'oracle_pass': True,
                 'counters': dict(counters, de_bubble=51 if bump else 50)}]
    saved = loop.measure.simulate
    loop.measure.simulate = clean
    lines = []
    try:
        bump = False
        same = loop.probe_clean_check('rtl', os.path.join(KTEST, 'clean'), metrics, draws, 1,
                                      lines.append)
        bump = True
        differ = loop.probe_clean_check('rtl', os.path.join(KTEST, 'clean'), metrics, draws, 1,
                                        lines.append)
    finally:
        loop.measure.simulate = saved
    check('v3 probe: the clean re-run covers the visible probed draws only, without the probe',
          seen and all(s == (['train-taxi'], False) for s in seen), str(seen))
    check('v3 probe: equal counters pass, one differing counter is reported',
          same is None and differ and 'de_bubble' in differ, str(differ))
    run = _FakeRun({'iterations': []})
    cands = [{'label': 'c1', 'commit': 'a', 'outcome': 'candidate', 'adoptable': True,
              'score': -5.0},
             {'label': 'c2', 'commit': None, 'outcome': 'no_proposal'}]
    saved_off = loop.measure.PROBE_OFF_REASON
    try:
        loop.probe_disagreed(run, cands, differ, run.log)
        off = loop.measure.PROBE_OFF_REASON
    finally:
        loop.measure.PROBE_OFF_REASON = saved_off
    check('v3 probe: a mismatch switches the probe off for the run and adopts nothing',
          run.state.get('probe_disabled') and off and cands[0]['outcome'] == 'measure_error'
          and not cands[0]['adoptable'] and cands[1]['outcome'] == 'no_proposal'
          and loop.never_measured(cands[0])
          and any('PROBE DISABLED' in ln for ln in run.lines))


def test_probe_backfill_merges_only_on_equal_bpc():
    import copy
    import loop
    draws = [(_Draw('train-taxi', True, True), 'p', 'c'),
             (_Draw('held-x', False, True), 'p', 'c'),
             (_Draw('synth-512B', True, False), 'p', 'c')]
    best = {'bytes_per_cycle': 9.0, 'draws': [
        {'name': 'train-taxi', 'scored': True, 'visible': True, 'bytes_per_cycle': 8.6843,
         'analysis': {'old': 1}},
        {'name': 'held-x', 'scored': True, 'visible': False, 'bytes_per_cycle': 9.5,
         'analysis': {'old': 1}}]}
    verdict = {'kind': 'rate', 'limit': 'x', 'handshakes': [], 'text': 't'}

    def fake(bpc_held):
        def measure_(rtl, work, ds, synth=True, jobs=None):
            assert not synth and [d[0].name for d in ds] == ['train-taxi', 'held-x']
            return {'oracle_pass': True, 'probe': {'handshakes': []}, 'probe_status': 'on',
                    'profile': {'binding': 'b', 'tightest': 'b', 'tightest_utilisation': 0.5,
                                'stale_ceilings': [], 'mean_output_idle_pct': 40.0,
                                'probe': verdict},
                    'draws': [{'name': 'train-taxi', 'visible': True, 'bytes_per_cycle': 8.6843,
                               'analysis': {'new': 1, 'probe': verdict},
                               'probe': {'handshakes': [], 'windows': [1]}},
                              {'name': 'held-x', 'visible': False, 'bytes_per_cycle': bpc_held,
                               'analysis': {'new': 1, 'probe': verdict},
                               'probe': {'handshakes': []}}]}
        return measure_

    out = []
    saved = loop.measure.measure
    try:
        for held in (9.5000001, 9.5):
            state = {'best': {'commit': 'abc', 'metrics': copy.deepcopy(best)}}
            run = _FakeRun(state, os.path.join(KTEST, 'backfill'))
            loop.measure.measure = fake(held)
            rec = loop.probe_backfill(run, draws, run.log)
            again = loop.probe_backfill(run, draws, run.log)
            out.append((rec, state['best']['metrics'], again, run.saves))
    finally:
        loop.measure.measure = saved
    (rec1, m1, again1, _s1), (rec2, m2, _again2, saves2) = out
    check('v3 probe: a backfill whose bytes/cycle differs merges nothing, and is not retried',
          rec1 and not rec1['merged'] and 'probe' not in m1 and m1 == best and again1 is None,
          str(rec1))
    held2 = [r for r in m2['draws'] if r['name'] == 'held-x'][0]
    taxi2 = [r for r in m2['draws'] if r['name'] == 'train-taxi'][0]
    check('v3 probe: equal bytes/cycle merges the probe, never a held-out draw\'s own',
          rec2['merged'] and m2.get('probe') and m2['profile']['probe'] == verdict
          and m2['lever']['lever'] == 'width' and 'probe' not in held2
          and 'probe' not in held2['analysis'] and 'windows' not in taxi2['probe']
          and m2['bytes_per_cycle'] == 9.0 and saves2 == 1, str(m2)[:400])


def test_prompts_carry_the_packing_model():
    import copy
    import loop
    import propose
    state = _run200_state()
    if state is None:
        print('skip  packing model in the prompts (hacc-real200 not present)')
        return
    skills = skills_mod.load(os.path.join(RUN200, 'skills.json'))
    ctx = loop.build_ctx(copy.deepcopy(state), skills, 55, roadmap_text='STEP 2 [open]',
                         packmodel_text='packing model, calibrated to c0757e673f\nPMBLOCK')
    d = dict(propose.FALLBACK_DIRECTIONS[0], roadmap_step='2')
    brief = propose.build_user_prompt(ctx, d, [])
    check('v3: the session brief has the packing model after the stage profile',
          brief.index('STAGE PROFILE') < brief.index(propose.PACKMODEL_HEADER)
          < brief.index('PMBLOCK'))
    check('v3: the session brief names its roadmap step and calls the roadmap advice',
          'roadmap:    STEP 2' in brief and 'advice ranked by modelled gain' in brief)
    check('v3: the context carries the best commit for the session\'s calibration',
          ctx['best_commit'] == state['best']['commit'])


def test_packmodel_runs_once_per_best_commit():
    import loop
    tmp = os.path.join(KTEST, 'pmcache')
    import shutil
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    calls = []

    def fake(run_name, commit, specs):
        calls.append((commit, tuple(specs)))
        return {'ok': False, 'text': '(packing model unavailable: x)', 'extra': {},
                'commit': commit, 'specs': sorted(specs)}
    saved_env = os.environ.get('AGENTIC_PACKMODEL_DIR')
    saved = loop.run_packmodel
    os.environ['AGENTIC_PACKMODEL_DIR'] = tmp
    loop.run_packmodel = fake
    try:
        run = _FakeRun({'best': {'commit': 'c' * 40}})
        timing = {}
        a = loop.packmodel_context(run, ['K4/L32/N32'], run.log, timing)
        b = loop.packmodel_context(run, ['K4/L32/N32'], run.log, {})
        fresh = _FakeRun({'best': {'commit': 'c' * 40}})
        c = loop.packmodel_context(fresh, [], fresh.log, {})
        fresh.state['best']['commit'] = 'd' * 40
        loop.packmodel_context(fresh, [], fresh.log, {})
    finally:
        loop.run_packmodel = saved
        if saved_env is None:
            os.environ.pop('AGENTIC_PACKMODEL_DIR', None)
        else:
            os.environ['AGENTIC_PACKMODEL_DIR'] = saved_env
    check('v3: a failed packing model is tried once per best commit (memory and disk)',
          len(calls) == 2 and a == b == c and 'unavailable' in a[0]
          and 'packmodel_s' in timing, str(calls))


def test_save_survives_report_error():
    import shutil
    import loop
    name = 'ktest-v3-save'
    saved = loop.report.write_report

    def boom(*a):
        raise RuntimeError('cannot draw')
    loop.report.write_report = boom
    run = loop.Run(name)
    try:
        run.state = {'x': 1}
        run.save()
        ok = loop.read_json(run.state_path).get('x') == 1
    finally:
        loop.report.write_report = saved
        run.log.close() if hasattr(run.log, 'close') else None
        shutil.rmtree(run.dir, ignore_errors=True)
    check('v3: a report that cannot be drawn does not stop a save', ok)


# ---------------------------------------------------------------------------
# loop v3, changes 4 and 5: build tracks and their step sessions

TRACKREPO = os.path.join(KTEST, 'trackrepo')
RAM_TEXT = """\
-- the simulation RAM
entity vhsnunzip_ram is
  generic (CMD_STAGES : natural := 1; RESP_STAGES : natural := 2);
  port (clk : in std_logic; a_cmd : in ram_command; a_resp : out ram_response);
end vhsnunzip_ram;
architecture behav of vhsnunzip_ram is begin end behav;
"""


def _git(args, cwd=TRACKREPO):
    import subprocess
    res = subprocess.run(['git'] + args, cwd=cwd, capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError('git %s: %s' % (' '.join(args), res.stderr))
    return res.stdout.strip()


def _write(rel, text, root=TRACKREPO):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fil:
        fil.write(text)


def _commit(msg, root=TRACKREPO):
    _git(['add', '-A'], root)
    _git(['-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-q', '-m', msg], root)
    return _git(['rev-parse', 'HEAD'], root)


def _trackrepo():
    """A throwaway git repo (never the kit's own) with a tiny rtl/."""
    import shutil
    import stat

    def unlock(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    if os.path.isdir(TRACKREPO):
        _git(['worktree', 'prune'])
        shutil.rmtree(TRACKREPO, onerror=unlock)
    os.makedirs(TRACKREPO)
    _git(['init', '-q', '-b', 'main'])
    _write('rtl/top.vhd', 'entity top is end top;\n')
    _write('rtl/vhsnunzip_ram.sim.vhd', RAM_TEXT)
    return _commit('base')


class _InRepo(object):
    """Point the kit's git helpers (tools.ROOT) at the throwaway repo."""

    def __enter__(self):
        import tools
        self.saved = tools.ROOT
        tools.ROOT = TRACKREPO
        return self

    def __exit__(self, *exc):
        import tools
        tools.ROOT = self.saved


def _track(steps=None, current=1, **kw):
    """A track object as the loop keeps it, past its design step."""
    import loop
    trk = loop.new_track({'id': 'wide', 'goal': 'K4/L32/N32', 'why': 'model says +99%',
                          'steps': []},
                         {'run': 'ktest', 'tracks': [],
                          'best': {'commit': kw.pop('base', 'b' * 40),
                                   'metrics': {'bytes_per_cycle': 10.0}}}, 3)
    design = trk['steps'][0]
    if steps is None:
        steps = [('golden', None), ('ports', None), ('throughput', 13.0), ('throughput', 19.0)]
    if current:
        design['status'] = 'passed'
    trk['steps'] = [design] + [
        {'n': j, 'kind': kind, 'text': 'step %d' % j, 'predicted_bpc': pred,
         'widths': {'K': 4, 'L': 32, 'N': 32, 'rules': 'ideal'} if pred else None,
         'source': 'spec', 'status': 'pending', 'ref': None, 'attempts': [], 'notes': ''}
        for j, (kind, pred) in enumerate(steps, 1)]
    trk['current'] = current
    trk.update(kw)
    return trk


def _tc(label='t1a1', commit='c' * 40, outcome='', gain=None, status='ok', notes=''):
    return {'label': label, 'branch': 'agentic-cand/ktest/i5-' + label, 'commit': commit,
            'outcome': outcome, 'reason': '', 'measured': {'gain_pct': gain} if gain else {},
            'session': {'status': status, 'cost_usd': 1.0}, 'track': {'id': 'wide'},
            'notes': notes, 'files_changed': ['rtl/x.vhd']}


def _asg(label='t1a1'):
    return {'label': label, 'worktree': os.path.join(KTEST, 'wt-' + label),
            'branch': 'agentic-cand/ktest/i5-' + label, 'start': 'b' * 40}


def _quiet_git(loop):
    """Replace loop.git with a recorder (step refs, branch moves)."""
    calls = []

    def fake(args, cwd=None, check=True, timeout=600):
        calls.append(list(args))
        return ''
    saved = loop.git
    loop.git = fake
    return calls, saved


def test_track_state_defaults():
    import loop
    old = {'iterations': []}
    loop.state_defaults(old)
    bad = loop.state_defaults({'tracks': None})
    run = _FakeRun(dict(old, run='ktest'))
    check('v3 tracks: an old state gets tracks [] and nothing is live',
          old['tracks'] == [] and bad['tracks'] == [] and loop.open_track(old) is None
          and loop.tracks_text(old) == 'none')
    trk = _track()
    state = {'tracks': [trk]}
    text = json.loads(json.dumps(state))
    check('v3 tracks: a track survives a JSON round trip unchanged and is live',
          text == state and loop.open_track(text)['id'] == 'wide'
          and 'open track wide' in loop.tracks_text(text)
          and 'step 3 [throughput] pending' in loop.tracks_text(text), loop.tracks_text(text))
    loop.track_load_check(run.state, run.log)
    check('v3 tracks: the load check leaves an old state alone', run.lines == [])


def test_old_state_loads_with_tracks():
    import copy
    import loop
    state = _run200_state()
    if state is None:
        print('skip  old state with tracks (hacc-real200 not present)')
        return
    src = os.path.join(RUN200, 'state.json')
    mtime = os.path.getmtime(src)
    state = copy.deepcopy(state)
    # The live run has had tracks since v3 resumed it; strip them to stand for
    # the pre-v3 state this check is about.
    state.pop('tracks', None)
    had = 'tracks' in state
    run = _FakeRun(state)
    loop.state_defaults(state)
    loop.track_load_check(state, run.log)
    trk = loop.working_track(state, {}, run.log)
    check('v3 tracks: hacc-real200\'s state loads with no tracks key, gets [] and no live track',
          not had and state['tracks'] == [] and trk is None
          and loop.prewait_s(state) == 60 * 60
          and 'none' in loop.tracks_text(state))
    check('v3 tracks: the original state.json was not touched', os.path.getmtime(src) == mtime)


def test_track_load_check_abandons_bad_index():
    import loop
    bad = _track(current=9)
    pend = _track(status='final_pending')
    fine = _track()
    state = {'tracks': [bad]}
    run = _FakeRun(state)
    loop.track_load_check(state, run.log)
    state2 = {'tracks': [pend]}
    loop.track_load_check(state2, run.log)
    state3 = {'tracks': [fine]}
    loop.track_load_check(state3, run.log)
    check('v3 tracks: a bad step index or a final_pending without a commit is abandoned, '
          'with a banner',
          bad['status'] == 'abandoned' and 'current=9, steps=5' in bad['reason']
          and pend['status'] == 'abandoned' and fine['status'] == 'open'
          and any(ln.startswith('TRACK wide ABANDONED') for ln in run.lines)
          and run.lines[0].startswith('===='), str(run.lines))


def test_open_track_from_plan():
    import loop
    import propose
    good = {'id': 'K4 / L32 wide-s3', 'goal': 'g', 'why': 'w',
            'steps': [{'text': 'golden', 'kind': 'golden'},
                      {'text': 'go', 'kind': 'throughput', 'predicted_bpc': '19.3'}]}
    req, why = propose.sanitize_track_request(good)
    bad_kind, why_kind = propose.sanitize_track_request(
        dict(good, steps=[{'text': 'x', 'kind': 'speed'}]))
    bad_last, why_last = propose.sanitize_track_request(
        dict(good, steps=[{'text': 'x', 'kind': 'golden'}]))
    too_many, _w = propose.sanitize_track_request(
        dict(good, steps=[{'text': 'x', 'kind': 'throughput'}] * 7))
    check('v3 tracks: the planner\'s request is sanitised (kebab id, no -s<n>, kinds, '
          'last step throughput, at most track_max_steps)',
          req and req['id'] == 'k4-l32-wide' and req['steps'][1]['predicted_bpc'] == 19.3
          and bad_kind is None and 'speed' in why_kind and bad_last is None
          and 'last step' in why_last and too_many is None, '%s %s' % (req, why))
    state = {'run': 'ktest', 'tracks': [{'id': 'k4-l32-wide', 'status': 'abandoned'}],
             'best': {'commit': 'a' * 40, 'metrics': {'bytes_per_cycle': 9.7}}}
    run = _FakeRun(state)
    calls, saved = _quiet_git(loop)
    try:
        events = []
        trk = loop.apply_track_command(run, None, {'open': good}, 7, events, run.log)
    finally:
        loop.git = saved
    check('v3 tracks: opening makes a unique id on the best, forces its branch, logs a banner '
          'and an event, and changes nothing in state',
          trk['id'] == 'k4-l32-wide-2' and trk['head'] == 'a' * 40
          and trk['calib_commit'] == 'a' * 40 and trk['base_bpc'] == 9.7
          and trk['steps'][0]['kind'] == 'design' and trk['current'] == 0
          and calls == [['branch', '-f', 'agentic-track/ktest/k4-l32-wide-2', 'a' * 40]]
          and events[0]['kind'] == 'track_open' and len(state['tracks']) == 1
          and any('TRACK k4-l32-wide-2 OPENED' in ln for ln in run.lines), str(calls))


def test_dry_run_creates_no_track():
    import loop
    state = {'run': 'ktest', 'tracks': [],
             'best': {'commit': 'a' * 40, 'metrics': {'bytes_per_cycle': 9.7}}}
    run = _FakeRun(state)
    calls, saved = _quiet_git(loop)
    try:
        events = []
        _d, _r, cmd = loop.propose.fake_plan_directions(2, 1, track_open=False)
        trk = loop.apply_track_command(run, None, cmd, 1, events, run.log, dry_run=True)
    finally:
        loop.git = saved
    check('v3 tracks: --dry-run opens no track and says what it would open',
          trk is None and not calls and not events and state['tracks'] == []
          and any('dry run: would open track fake-wide' in ln for ln in run.lines))


def test_planner_always_gives_n_directions():
    import propose
    dirs, _res, cmd = propose.fake_plan_directions(3, 1, track_open=False)
    dirs2, _res, cmd2 = propose.fake_plan_directions(3, 1, track_open=True)
    _d, _r, cmd3 = propose.fake_plan_directions(3, 2, track_open=False)
    saved = propose.ask, propose.build_plan_prompt
    propose.ask = lambda *a, **kw: {'status': 'ok', 'text': json.dumps({
        'directions': [{'focus': 'one'}], 'open_track': {'id': 'x'},
        'abandon_track': 'no longer pays'})}
    propose.build_plan_prompt = lambda ctx, n: 'prompt'
    try:
        d_open, _r, c_open = propose.plan_directions({}, 2, lambda m: None, track_open=True)
        d_none, _r, c_none = propose.plan_directions({}, 2, lambda m: None, track_open=False)
    finally:
        propose.ask, propose.build_plan_prompt = saved
    check('v3 tracks: the scripted planner gives n directions, and opens a track on '
          'iteration 1 only when none is open',
          len(dirs) == 3 and len(dirs2) == 3 and cmd['open']['id'] == 'fake-wide'
          and cmd2 == {} and cmd3 == {})
    check('v3 tracks: the planner always returns n directions; abandon only with a track '
          'open, open only without one',
          len(d_open) == 2 and len(d_none) == 2 and c_open == {'abandon': 'no longer pays'}
          and set(c_none) == {'open'}, '%s %s' % (c_open, c_none))


def _design_docs(root, tid='wide', spec_chars=1600, steps=None):
    import shutil
    if os.path.isdir(root):
        shutil.rmtree(root)
    docs = os.path.join(root, 'docs', 'track-' + tid)
    os.makedirs(docs)
    with open(os.path.join(docs, 'SPEC.md'), 'w', encoding='utf-8') as fil:
        fil.write('s' * spec_chars)
    if steps is not None:
        with open(os.path.join(docs, 'steps.json'), 'w', encoding='utf-8') as fil:
            json.dump(steps, fil)


def test_judge_design():
    import loop
    trk = _track(current=0)
    root = os.path.join(KTEST, 'design')
    good = [{'text': 'golden', 'kind': 'golden', 'predicted_bpc': None},
            {'text': 'wide', 'kind': 'throughput', 'predicted_bpc': 18.0,
             'widths': {'K': 4, 'L': 32, 'N': 32, 'rules': 'ideal'}}]
    asked = []

    def model(specs):
        asked.append(list(specs))
        return {'K4/L32/N32': {'all': 19.28, 'calibrated': True}}

    def judged(steps, chars=1600, fn=model):
        _design_docs(root, steps=steps, spec_chars=chars)
        return loop.judge_design(root, trk, fn)
    ok, note, steps = judged(good)
    check('v3 tracks: a good design passes; its steps become n=1.. with the model\'s number',
          ok and [s['n'] for s in steps] == [1, 2] and steps[1]['model_bpc'] == 19.28
          and steps[0]['status'] == 'pending' and asked == [['K4/L32/N32']], note)
    no_widths = [good[0], dict(good[1], widths=None)]
    sandbag = [good[0], dict(good[1], predicted_bpc=12.0)]
    flat = [good[0], dict(good[1], predicted_bpc=10.0)]
    results = [judged(no_widths), judged(sandbag), judged(flat), judged(good, chars=200),
               judged([dict(good[1], kind='ports')])]
    whys = [r[1] for r in results]
    check('v3 tracks: missing widths, a sandbagged prediction, a final step not above the '
          'base, a short spec and a list not ending in throughput all fail',
          not any(r[0] for r in results) and 'widths' in whys[0]
          and 'under 90%' in whys[1] and 'not above' in whys[2]
          and 'at least 1500' in whys[3] and 'last step' in whys[4], str(whys))
    ok2, note2, _s = judged(good, fn=lambda specs: {})
    check('v3 tracks: with no calibrated model the prediction is accepted and the note says so',
          ok2 and 'not checked' in note2, note2)


def test_judge_session_table():
    import loop
    js = loop.judge_session
    rows = [js({'status': 'limit'}, False, 'golden'), js({'status': 'error'}, False, 'ports'),
            js({'status': 'timeout'}, False, 'throughput'),
            js({'status': 'budget'}, False, 'throughput'),
            js({'status': 'budget'}, True, 'throughput'), js({'status': 'ok'}, False, 'ports'),
            js({'status': 'ok'}, False, 'design')]
    check('v3 tracks: session-level results follow the table',
          rows[0][0] == 'track_pending' and rows[0][2:] == (False, False)
          and all(r[0] == 'track_session_failed' and r[2:] == (False, True) for r in rows[1:4])
          and rows[4] == (None, '', True, True)
          and rows[5][0] == 'track_step_failed' and rows[5][1] == 'no change made'
          and rows[6][0] == 'track_design_invalid', str(rows))


def test_judge_step_rules():
    import loop
    golden = {'kind': 'golden', 'predicted_bpc': None}
    thr = {'kind': 'throughput', 'predicted_bpc': 10.0}
    ok = {'oracle_pass': True, 'bytes_per_cycle': 9.2}
    got = [loop.judge_step(golden, ok)[0],
           loop.judge_step(thr, ok)[0],
           loop.judge_step(thr, dict(ok, bytes_per_cycle=8.5))[0],
           loop.judge_step(thr, {'oracle_pass': False, 'first_problem': 'taxi: wrong'})[0],
           loop.judge_step(thr, {'measure_error': 'killed'})[0],
           loop.judge_step(thr, dict(ok, synth_error='Vivado: no'))[0],
           loop.judge_step(thr, {'error': 'does not compile'})[0]]
    pess = loop.judge_step(thr, dict(ok, bytes_per_cycle=11.5))
    check('v3 tracks: golden passes; 0.92x passes; 0.85x is short; an oracle failure fails; '
          'measure_error is not measured; a synthesis failure does not change the verdict',
          got == ['track_step_passed', 'track_step_passed', 'track_step_short',
                  'track_step_failed', 'track_never_measured', 'track_step_passed',
                  'track_step_failed'] and 'model pessimistic' in pess[1], str(got))


def test_unit_only_step_is_not_measured():
    import loop
    base = _trackrepo()
    _write('rtl/unit/golden_tb.vhd', 'entity golden_tb is end;\n')
    unit_only = _commit('unit test only')
    _write('rtl/top.vhd', 'entity top is end top; -- wider\n')
    top_change = _commit('top changed')
    trk = _track(steps=[('golden', None), ('ports', None), ('throughput', 19.0)], base=base,
                 head=base)
    asg = {'worktree': TRACKREPO, 'label': 't1a1'}
    seen = []

    def unit_fn(worktree, name):
        seen.append(name)
        return True, 'UNIT PASS %s' % name
    a = loop.track_pre_measure(trk, _tc(commit=unit_only), asg, None, unit_fn)
    failing = loop.track_pre_measure(trk, _tc(commit=unit_only), asg, None,
                                     lambda w, n: (False, 'UNIT FAIL ' + n))
    trk['current'] = 2
    b = loop.track_pre_measure(trk, _tc(commit=top_change), asg, None, unit_fn)
    trk['current'] = 3
    c = loop.track_pre_measure(trk, _tc(commit=top_change), asg, None, unit_fn)
    check('v3 tracks: a golden step that changes only rtl/unit/ is judged by its unit test, '
          'never measured',
          a.get('judged') == 'track_step_passed' and seen == ['golden_tb']
          and 'measure' not in a and failing.get('judged') == 'track_step_failed', str(a))
    check('v3 tracks: a step that changes top-level rtl is simulated; only the final step '
          'is synthesised',
          b == {'measure': True, 'synth': False} and c == {'measure': True, 'synth': True},
          '%s %s' % (b, c))


def _settle(trk, outcome, label='t1a1', commit='c' * 40, reason='r', metrics=None,
            used=True, events=None, notes='', spec_steps=None):
    import loop
    run = _FakeRun({})
    calls, saved = _quiet_git(loop)
    c = _tc(label=label, commit=commit, notes=notes)
    try:
        loop.settle_track(trk, c, _asg(label), outcome, reason, 5,
                          events if events is not None else [], run.log, used=used,
                          metrics=metrics, spec_steps=spec_steps)
    finally:
        loop.git = saved
    return c, run, calls


def test_step_pass_advances():
    import loop
    trk = _track()
    c, run, calls = _settle(trk, 'track_step_passed', commit='d' * 40,
                            notes='## How it works\nthe golden model\n## What I tried\nx\n'
                                  '## Why it worked or failed\ny')
    st = trk['steps'][1]
    check('v3 tracks: a pass marks the step passed, keeps its notes, sets its ref, moves head '
          'and current, and counts one iteration',
          st['status'] == 'passed' and 'the golden model' in st['notes']
          and st['ref'] == 'agentic-track/ktest/wide-s1' and trk['head'] == 'd' * 40
          and trk['current'] == 2 and trk['iterations_used'] == 1
          and calls == [['branch', '-f', 'agentic-track/ktest/wide-s1', 'd' * 40]]
          and c['outcome'] == 'track_step_passed' and not c['adoptable'], str(st))
    design = _track(current=0)
    spec = [{'n': 1, 'kind': 'throughput', 'text': 'all at once', 'predicted_bpc': 19.0,
             'status': 'pending', 'attempts': [], 'notes': ''}]
    _settle(design, 'track_design_done', spec_steps=spec)
    check('v3 tracks: an accepted design replaces the planner\'s steps with the spec\'s',
          [s['kind'] for s in design['steps']] == ['design', 'throughput']
          and design['current'] == 1 and design['steps'][0]['status'] == 'passed')
    check('v3 tracks: the brief for the next step names it and carries the notes',
          'step 2 of 4 (ports)' in loop.track_direction(trk, 'SPEC', 'NOTES')['focus']
          and loop.track_direction(trk, 'SPEC', 'NOTES')['track']['spec'] == 'SPEC')


def test_never_measured_streak():
    trk = _track()
    for _ in range(3):
        _settle(trk, 'track_never_measured')
    st = trk['steps'][1]
    check('v3 tracks: the third never-measured result in a row is a failed attempt; each '
          'counts an iteration',
          [bool(a.get('counted')) for a in st['attempts']] == [False, False, True]
          and trk['iterations_used'] == 3 and trk['status'] == 'open'
          and trk['never_measured_streak'] == 0, str(st['attempts']))
    _settle(trk, 'track_session_failed')
    _settle(trk, 'track_pending', used=False)
    check('v3 tracks: a failed session is no attempt but an iteration; a limit is neither',
          trk['iterations_used'] == 4 and len(st['attempts']) == 4
          and trk['pending_slot']['step'] == 1 and trk['status'] == 'open')


def test_track_abandons_after_two_failures():
    trk = _track()
    trk['current'] = 3
    events = []
    _settle(trk, 'track_step_short', metrics={'bytes_per_cycle': 12.1}, events=events)
    first = trk['status']
    _c, run, _calls = _settle(trk, 'track_step_short', metrics={'bytes_per_cycle': 12.6},
                              events=events)
    check('v3 tracks: a step short twice abandons the track with both numbers, a banner and '
          'an event',
          first == 'open' and trk['status'] == 'abandoned'
          and trk['reason'] == 'step 3 (throughput) short 2 times: 12.10, 12.60 vs 13.00 predicted'
          and any('TRACK wide ABANDONED' in ln for ln in run.lines)
          and events[-1]['kind'] == 'track_abandon', trk['reason'])
    design = _track(current=0)
    _settle(design, 'track_design_invalid', reason='no spec')
    _settle(design, 'track_design_invalid', reason='short spec')
    check('v3 tracks: a design judged invalid twice abandons the track',
          design['status'] == 'abandoned' and 'invalid 2 times' in design['reason'])


def test_track_adoption_names_what_it_replaces():
    import loop
    trk = _track(base='b' * 40, opened_iteration=3)
    state = {'best': {'commit': 'e' * 40}, 'iterations': [
        {'iteration': 2, 'winner': {'id': 'old', 'commit': 'b' * 40}},
        {'iteration': 4, 'winner': {'id': 'pair-issue', 'commit': 'e' * 40}},
        {'iteration': 5, 'winner': None}]}
    lines = []
    gone = loop.warn_replaced(state, trk, lines.append)
    same = loop.replaced_adoptions({'best': {'commit': 'b' * 40}, 'iterations': []}, trk)
    check('v3 tracks: adopting a track after the best moved names the adoptions it replaces',
          gone == ['i4 pair-issue'] and same == []
          and any('replaces the adoptions made while it was open: i4 pair-issue' in ln
                  for ln in lines), str(lines))


def test_tracks_text_withholds_held_out():
    import loop
    secret = 'held-nation: wrong output at byte 12: got 6865207265672400646920717569636b'
    trk = _track()
    _settle(trk, 'track_step_failed', reason=secret)
    live = loop.tracks_text({'tracks': [trk]})
    _settle(trk, 'track_step_failed', reason=secret)
    closed = loop.tracks_text({'tracks': [trk]})
    check('v3 tracks: the TRACKS section the planner reads never names a held-out table or quotes '
          'its bytes',
          trk['status'] == 'abandoned' and 'held-nation' not in live + closed
          and '6865207265' not in live + closed and 'a held-out table' in live
          and 'a held-out table' in closed, live + closed)


def test_track_abandons_after_eight_iterations():
    trk = _track(iterations_used=7)
    _settle(trk, 'track_step_passed')
    capped = dict(trk)
    last = _track(iterations_used=7)
    last['current'] = 4
    c, _run, _calls = _settle(last, 'track_final_pending', metrics={'bytes_per_cycle': 19.5})
    check('v3 tracks: the eighth iteration abandons an open track after its slot is judged',
          capped['status'] == 'abandoned' and 'used its 8 iterations' in capped['reason']
          and trk['steps'][1]['status'] == 'passed', capped.get('reason'))
    check('v3 tracks: a final step judged in the eighth iteration still counts',
          last['status'] == 'final_pending' and last['final']['commit'] == 'c' * 40
          and last['iterations_used'] == 8)


def _final_cands(track_gain, normal_gain):
    import loop
    goal = {'metric': 'throughput', 'target_pct': None, 'text': ''}
    parent = _vivado(10.0, 2.5, 3577)
    trk = _track()
    trk['current'] = 4
    tc = dict(_tc(), measured={})
    normal = {'label': 'c1', 'track': None, 'outcome': ''}
    for c, gain in ((tc, track_gain), (normal, normal_gain)):
        m = _vivado(10.0 * (1 + gain / 100.0), 2.5 * (1 + gain / 100.0), 29050)
        ev = loop.evaluate(dict(m, oracle_pass=True), parent, goal)
        c.update(measured=ev['measured'], score=ev['score'], outcome=ev['outcome'],
                 adoptable=ev['adoptable'], reason=ev['reason'])
    loop.admit_track_candidate(trk, tc)
    return trk, tc, normal


def test_final_step_scored_against_best():
    import loop
    trk, tc, normal = _final_cands(12.0, 3.0)
    win = loop.select_winner([normal, tc])
    verdict = loop.track_after_measure(trk, tc, {'oracle_pass': True, 'bytes_per_cycle': 11.2},
                                       'nowhere', '', None)
    check('v3 tracks: a final step at +12% wins over a +3% normal candidate',
          win is tc and verdict[0] == 'final_ok' and not tc['adoptable'], str(verdict))
    trk, tc, normal = _final_cands(-5.0, 3.0)
    win = loop.select_winner([normal, tc])
    verdict = loop.track_after_measure(trk, tc, {'oracle_pass': True, 'bytes_per_cycle': 9.5},
                                       'nowhere', '', None)
    _settle(trk, verdict[0], reason=verdict[1])
    check('v3 tracks: at -5% the normal candidate wins and the track is finished',
          win is normal and verdict[0] == 'track_final_not_better'
          and trk['status'] == 'finished', str(verdict))
    trk, tc, _n = _final_cands(20.0, 0.0)
    lines = []
    verdict = loop.track_after_measure(trk, tc, {'oracle_pass': True, 'bytes_per_cycle': 12.0},
                                       'nowhere', '', lines.append)
    check('v3 tracks: at 0.63 of its prediction but +20% over the best it is still adoptable',
          verdict[0] == 'final_ok' and loop.select_winner([tc]) is tc
          and any('0.63 of its prediction' in ln for ln in lines), str(lines))


def test_non_final_step_ignores_synthesis_failure():
    # track_synth_steps on: a non-final step is synthesised as information
    # only. An unreachable host or a synthesis error must leave its
    # simulation verdict alone (plan 5.4), not make it never measured.
    import loop
    goal = {'metric': 'throughput', 'target_pct': None, 'text': ''}
    parent = _vivado(10.0, 2.5, 3577)
    host = 'ssh to vdixit@hacc-build-02 failed: timed out'
    cases = (
        ('a synthesis error naming the host', {'synth_error': host}, 'track_step_passed'),
        ('a synthesis error', {'synth_error': 'Vivado: 3 critical warnings'},
         'track_step_passed'),
        ('an unreachable host (measure_error)',
         {'measure_error': 'the synthesis host could not be reached for 60 min (%s); the '
                           'design was never synthesised' % host}, 'track_step_passed'),
        ('a killed simulator',
         {'measure_error': 'the simulator was killed by the operating system (most likely '
                           'out of memory) on held-1'}, 'track_never_measured'),
    )
    for name, extra, want in cases:
        trk = _track()
        trk['current'] = 3                      # throughput, predicts 13.0
        metrics = dict({'oracle_pass': True, 'bytes_per_cycle': 13.5}, **extra)
        ev = loop.evaluate(metrics, parent, goal)
        c = dict(_tc(), outcome=ev['outcome'], reason=ev['reason'],
                 adoptable=ev['adoptable'], measured=ev['measured'])
        loop.admit_track_candidate(trk, c)
        got = loop.track_after_measure(trk, c, metrics, 'nowhere', '', None)
        check('v3 tracks: a non-final step with %s is judged %s' % (name, want),
              got[0] == want and not c['track_final_ok'], str(got))
    trk = _track()
    trk['current'] = 4                          # the final step must synthesise
    metrics = {'oracle_pass': True, 'bytes_per_cycle': 19.5, 'synth_error': host}
    ev = loop.evaluate(metrics, parent, goal)
    c = dict(_tc(), outcome=ev['outcome'], reason=ev['reason'], adoptable=ev['adoptable'],
             measured=ev['measured'])
    loop.admit_track_candidate(trk, c)
    got = loop.track_after_measure(trk, c, metrics, 'nowhere', '', None)
    check('v3 tracks: the final step with an unreachable host is still never measured',
          got[0] == 'track_never_measured', str(got))


def test_non_final_step_never_adopted():
    import loop
    trk, tc, normal = _final_cands(50.0, 0.0)
    trk['current'] = 3
    tc['adoptable'] = True
    loop.admit_track_candidate(trk, tc)
    check('v3 tracks: a non-final step +50% over the best is never adoptable',
          not tc['track_final_ok'] and not tc['adoptable']
          and loop.select_winner([tc, normal]) is None)
    stray = dict(tc, adoptable=True, track_final_ok=False, score=-99)
    check('v3 tracks: selection ignores a track candidate\'s own adoptable flag',
          loop.select_winner([stray]) is None)


def test_final_pending_rescored():
    import loop
    goal = {'metric': 'throughput', 'target_pct': None, 'text': ''}
    out = []
    for best_gbps in (2.5, 3.5):
        trk = _track(status='final_pending')
        trk['current'] = 4
        trk['final'] = {'commit': 'f' * 40, 'branch': 'agentic-cand/ktest/i5-t4a1',
                        'metrics': dict(_vivado(12.0, 3.0, 29050), oracle_pass=True),
                        'iteration': 5, 'notes': ''}
        state = {'run': 'ktest', 'goal': goal, 'tracks': [],
                 'best': {'commit': 'b' * 40, 'metrics': _vivado(10.0, best_gbps, 3577)}}
        run = _FakeRun(state)
        adopted = []
        saved = (loop.adopt, loop.probe_clean_check, loop.git, loop.remove_worktree,
                 loop.ram_latency, loop.ram_latency_at)
        loop.adopt = lambda r, c, m, k, log: (adopted.append(c['commit']),
                                              c.update(adopted=True, outcome='adopted'))
        loop.probe_clean_check = lambda *a: None
        loop.git = lambda *a, **kw: ''
        loop.remove_worktree = lambda p: None
        loop.ram_latency = lambda d: 'R'
        loop.ram_latency_at = lambda c, cwd=None: 'R'
        events = []
        try:
            got = loop.rescore_final_pending(run, trk, 6, [], events, run.log)
        finally:
            (loop.adopt, loop.probe_clean_check, loop.git, loop.remove_worktree,
             loop.ram_latency, loop.ram_latency_at) = saved
        out.append((got, trk, adopted, events))
    (g1, t1, a1, e1), (g2, t2, a2, e2) = out
    check('v3 tracks: a waiting final step that still beats the best is adopted next iteration',
          g1 and g1['adopted'] and a1 == ['f' * 40] and t1['status'] == 'adopted'
          and e1[0]['outcome'] == 'adopted', str(t1.get('reason')))
    check('v3 tracks: one the new best has overtaken is finished, not adopted',
          g2 is None and not a2 and t2['status'] == 'finished'
          and 'no longer beats' in t2['reason'], str(t2.get('reason')))


def test_adopted_track_closed_when_redone():
    # Ctrl-C after a track's final step was adopted, before the iteration's
    # save: state.json holds the new best but the track is still open at its
    # final step. The redone iteration must close it as adopted, not build
    # the final step again and score it against itself (+0.00%).
    import loop
    goal = {'metric': 'throughput', 'target_pct': None, 'text': ''}
    trk = _track()
    trk['current'] = 4
    state = {'run': 'ktest', 'goal': goal, 'tracks': [trk], 'iterations': [],
             'best': {'commit': 'b' * 40, 'metrics': _vivado(10.0, 2.5, 3577)}}

    class _Run(_FakeRun):
        def set_progress(self):
            pass

        def write_guide(self, **kw):
            pass
    run = _Run(state)
    calls, saved = _quiet_git(loop)
    saved_ok, saved_cal = loop.git_ok, loop.calibrate_packmodel
    loop.git_ok = lambda *a, **kw: True
    loop.calibrate_packmodel = lambda r, log: None
    try:
        win = dict(_tc(label='t4a1', commit='f' * 40), id='track-wide-step-4',
                   track={'id': 'wide', 'step': 4, 'kind': 'throughput'})
        loop.adopt(run, win, dict(_vivado(12.0, 3.0, 29050), oracle_pass=True), 7, run.log)
        marked = state['best'].get('track') or {}
        # ... the interrupt: nothing else of i7 was saved. Resume, redo i7.
        work = loop.working_track(state, {}, run.log)
        events = []
        got = loop.adopted_unsaved(run, work, 7, events, run.log)
        again = loop.adopted_unsaved(run, loop.working_track(state, {}, run.log), 7, [],
                                     run.log)
        other = _track()
        other['current'] = 4
        other['id'] = 'other'
        stranger = loop.adopted_unsaved(run, other, 7, [], run.log)
    finally:
        loop.git, loop.git_ok, loop.calibrate_packmodel = saved, saved_ok, saved_cal
    st = work['steps'][4]
    check('v3 tracks: adopt() marks a best that came from a track (id, step, iteration)',
          marked.get('id') == 'wide' and marked.get('step') == 4
          and marked.get('iteration') == 7, str(marked))
    check('v3 tracks: the redone iteration closes the track as adopted with no session',
          got and got['adopted'] and work['status'] == 'adopted'
          and work['head'] == 'f' * 40 and st['status'] == 'passed'
          and [a['outcome'] for a in st['attempts']] == ['adopted']
          and events and events[0]['outcome'] == 'adopted', str(work.get('reason')))
    check('v3 tracks: the step ref is set to the adopted commit',
          ['branch', '-f', 'agentic-track/ktest/wide-s4', 'f' * 40] in calls, str(calls))
    check('v3 tracks: state is not changed before the save; another track is left alone',
          state['tracks'][0]['status'] == 'open' and again is not None and stranger is None)


def test_track_events_only_at_save():
    import copy
    import loop
    state = {'run': 'ktest', 'iterations': [], 'tracks': [],
             'best': {'commit': 'a' * 40, 'metrics': {'bytes_per_cycle': 9.7}}}
    run = _FakeRun(state)
    calls, saved = _quiet_git(loop)
    try:
        events = []
        _d, _r, cmd = loop.propose.fake_plan_directions(2, 1)
        trk = loop.apply_track_command(run, None, cmd, 1, events, run.log)
        trk['steps'] = trk['steps'] + _track()['steps'][1:]
        loop.settle_track(trk, _tc(commit='d' * 40), _asg(), 'track_design_done', 'ok', 1,
                          events, run.log, spec_steps=_track()['steps'][1:])
        before = copy.deepcopy(state)
        # An interrupt here: main() saves state as it is. Nothing was applied.
        untouched = state == before and state['tracks'] == []
        events.append({'kind': 'track_state', 'track': copy.deepcopy(trk)})
        record = {'iteration': 1, 'tokens': {}, 'tokens_cost_estimate': 0.0, 'cost_usd': 0.0,
                  'timing': {}}
        loop.append_record(run, record, run.log, events=events)
    finally:
        loop.git = saved
    rec = state['tracks'][0] if state['tracks'] else {}
    check('v3 tracks: an open and a step pass change nothing in state until the record is '
          'saved; then both land in the one save',
          untouched and run.saves == 1 and rec.get('id') == 'fake-wide'
          and rec.get('current') == 1 and rec.get('head') == 'd' * 40
          and [e['kind'] for e in state['iterations'][0]['track_events']]
          == ['track_open', 'track_pass']
          and calls[-1] == ['branch', '-f', 'agentic-track/ktest/fake-wide', 'd' * 40],
          str(calls))


def test_step_ref_survives_branch_delete():
    import loop
    base = _trackrepo()
    _git(['branch', 'agentic-cand/ktest/i5-t1a1', base])
    _write('rtl/top.vhd', 'entity top is end top; -- step 1\n')
    step = _commit('step 1')
    _git(['branch', '-f', 'agentic-cand/ktest/i5-t1a1', step])
    trk = _track(base=base, head=base)
    ref = loop.make_step_ref(trk, 1, step, cwd=TRACKREPO)
    _git(['branch', '-D', 'agentic-cand/ktest/i5-t1a1'])
    _git(['checkout', '-q', base])
    _git(['branch', '-D', 'main'])
    check('v3 tracks: after a pass the -s<n> ref holds the commit when the candidate branch '
          'is deleted',
          ref == 'agentic-track/ktest/wide-s1'
          and _git(['rev-parse', ref]) == step)


def test_reconcile_tracks():
    import loop
    base = _trackrepo()
    _write('rtl/top.vhd', 'entity top is end top; -- step 1\n')
    step = _commit('step 1')
    trk = _track(base=base, head=step)
    trk['steps'][1].update(status='passed', ref='agentic-track/ktest/wide-s1',
                           attempts=[{'outcome': 'track_step_passed', 'commit': step}])
    # A crash between the save and the branch move: the branch is still at
    # the base, and the step ref was never written.
    _git(['branch', 'agentic-track/ktest/wide', base])
    state = {'tracks': [json.loads(json.dumps(trk))]}
    lines = []
    loop.reconcile_tracks(state, lines.append, cwd=TRACKREPO)
    check('v3 tracks: on resume the track branch and its step refs go back to the saved state',
          _git(['rev-parse', 'agentic-track/ktest/wide']) == step
          and _git(['rev-parse', 'agentic-track/ktest/wide-s1']) == step and not lines,
          str(lines))


def test_open_track_with_leftover_branch():
    import loop
    base = _trackrepo()
    _write('rtl/top.vhd', 'entity top is end top; -- crashed attempt\n')
    other = _commit('leftover')
    _git(['branch', 'agentic-track/ktest/wide', other])
    _git(['checkout', '-q', '--detach', base])
    state = {'run': 'ktest', 'tracks': [],
             'best': {'commit': base, 'metrics': {'bytes_per_cycle': 9.7}}}
    run = _FakeRun(state)
    with _InRepo():
        trk = loop.apply_track_command(run, None, {'open': {
            'id': 'wide', 'steps': [{'text': 'x', 'kind': 'throughput'}]}}, 2, [], run.log)
    check('v3 tracks: opening over a branch a crashed attempt left moves it to the best',
          trk and trk['id'] == 'wide'
          and _git(['rev-parse', 'agentic-track/ktest/wide']) == base, str(run.lines))


def test_slot_prep_git_error_suspends():
    import loop
    base = _trackrepo()
    state = {'run': 'ktest', 'tracks': [],
             'best': {'commit': base, 'metrics': {'bytes_per_cycle': 9.7}}}
    run = _FakeRun(state, os.path.join(KTEST, 'slotrun'))
    trk = _track(base=base, head=base)          # a build step, but no SPEC.md at head
    with _InRepo():
        first = loop.prepare_track_slot(run, trk, 5, os.path.join(KTEST, 'slotiter'), run.log)
        second = loop.prepare_track_slot(run, trk, 5, os.path.join(KTEST, 'slotiter'), run.log)
        _write('docs/track-wide/SPEC.md', 'the spec\n')
        trk['head'] = _commit('spec')
        third = loop.prepare_track_slot(run, trk, 5, os.path.join(KTEST, 'slotiter'), run.log)
        if third:
            loop.remove_worktree(third['asg']['worktree'])
    check('v3 tracks: a git error suspends the slot (no crash) and counts; a good slot resets it',
          first is None and second is None and any('slot suspended' in ln for ln in run.lines)
          and third and third['mode'] == 'session' and trk['suspended'] == 0
          and third['asg']['label'] == 't1a1' and third['asg']['track_id'] == 'wide'
          and third['asg']['direction']['track']['spec'] == 'the spec'
          and third['asg']['start'] == trk['head'], str(run.lines))
    trk['steps'][1]['attempts'].append({'outcome': 'track_never_measured', 'commit': base,
                                        'iteration': 4, 'files': ['rtl/top.vhd']})
    with _InRepo():
        again = loop.prepare_track_slot(run, trk, 6, os.path.join(KTEST, 'slotiter'), run.log)
        if again:
            loop.remove_worktree(again['asg']['worktree'])
    check('v3 tracks: a never-measured step is measured again with no session and no cost',
          again and again['mode'] == 'remeasure' and again['cand']['commit'] == base
          and again['cand']['session']['cost_usd'] == 0.0 and again['asg']['label'] == 't1a2')


def test_pending_slot_keeps_track_context():
    import loop
    trk = _track()
    direction = loop.track_direction(trk, 'S' * 5000, 'notes')
    asg = dict(_asg(), direction=direction, track_id='wide', step=1, kind='golden')
    slot = loop.pending_slot(asg)
    check('v3 tracks: a pending slot keeps its track, step and kind, not the spec',
          slot['track_id'] == 'wide' and slot['step'] == 1 and slot['kind'] == 'golden'
          and 'spec' not in slot['direction']['track'])
    opened = _track(current=0)
    state = {'tracks': []}
    run = _FakeRun(state)
    got = loop.working_track(state, {'iteration': 4, 'track': opened}, run.log)
    closed = {'tracks': [dict(_track(), status='abandoned')]}
    none = loop.working_track(closed, {'iteration': 4, 'track': _track()}, run.log)
    check('v3 tracks: a track opened by an interrupted attempt comes back from the pending '
          'dict; one closed meanwhile does not',
          got == opened and got is not opened and none is None)
    kept = loop.keep_track_slots([slot, {'label': 'c1', 'worktree': 'x', 'branch': 'b'}],
                                 None, run.log)
    check('v3 tracks: a pending track slot whose track is gone is dropped, the others kept',
          [s['label'] for s in kept] == ['c1'])


def test_ram_veto_hash():
    # The veto (what refuses a candidate) is the stage counts, exactly as
    # before v3: the brief lets only the area gate change scoring. The
    # whole-model hash is logged, never scored, and ignores comment, case
    # and whitespace edits, inside a line too.
    import loop
    variants = {
        'comment': RAM_TEXT.replace('-- the simulation RAM', '-- reworded comment\n  ').upper(),
        'inline space': RAM_TEXT.replace(' := ', ':=').replace(' : ', ':'),
        'stage': RAM_TEXT.replace('RESP_STAGES : natural := 2', 'RESP_STAGES : natural := 1'),
        'port': RAM_TEXT.replace('a_resp : out ram_response',
                                 'a_resp : out ram_response; b_cmd : in ram_command'),
        'body': RAM_TEXT.replace('begin end', 'begin null; end'),
    }
    sig = dict((k, loop.ram_signature(v)) for k, v in variants.items())
    mod = dict((k, loop.ram_model(v)) for k, v in variants.items())
    base, base_mod = loop.ram_signature(RAM_TEXT), loop.ram_model(RAM_TEXT)
    check('v3: the RAM veto is the stage counts alone, as before v3 (a port or body edit '
          'is not refused)',
          base == 'CMD_STAGES=1 RESP_STAGES=2' and sig['stage'] != base
          and sig['port'] == sig['body'] == sig['inline space'] == sig['comment'] == base
          and loop.ram_signature('') == '', str(sig))
    check('v3: the logged RAM model hash ignores comment, case and in-line whitespace edits; '
          'a port or body edit changes it',
          mod['comment'] == mod['inline space'] == base_mod
          and len({base_mod, mod['port'], mod['body']}) == 3, str(mod))
    real = os.path.join(os.path.dirname(ROOT), 'diag-i35', 'rtl', 'vhsnunzip_ram.sim.vhd')
    if os.path.exists(real):
        with open(real, encoding='utf-8') as fil:
            text = fil.read()
        check('v3: on i35\'s RAM file, "a := b" to "a:=b" changes neither the veto nor the hash',
              loop.ram_signature(text) == loop.ram_signature(text.replace(' := ', ':='))
              and loop.ram_model(text) == loop.ram_model(text.replace(' := ', ':=')))


def test_save_survives_report_with_tracks():
    import report
    trk = _track()
    trk['steps'][1]['attempts'] = [{'iteration': 5, 'label': 't1a1',
                                    'outcome': 'track_step_passed', 'measured_bpc': 9.7}]
    state = {'run': 'r', 'tracks': [trk], 'iterations': [{
        'iteration': 5, 'candidates': [dict(_tc(), outcome='track_step_passed',
                                            track={'id': 'wide', 'step': 1, 'kind': 'golden'},
                                            absolute={'area': 3000, 'bytes_per_cycle': 9.7})],
        'best_after': {}}]}
    page = report.render_html(state)
    check('v3 tracks: the report shows track steps in their own colour and a Tracks table',
          'class="cand track"' in page and '<h2>Tracks</h2>' in page
          and 'track_step_passed 9.700 B/cycle' in page)
    try:
        import gui
    except Exception as exc:                  # no tkinter here
        print('skip  gui track points (%s)' % str(exc)[:80])
        return
    store = gui.Store('ktest')
    store._state = state
    try:
        best, cands = store.series()
    except Exception as exc:
        check('v3 tracks: the gui reads track candidates', False, str(exc))
        return
    check('v3 tracks: the gui marks track points and never puts them in the best series',
          len(cands) == 1 and all(p['track'] for p in cands) and len(best) == 0)


def test_track_sessions_get_their_own_limits():
    import propose
    tr = {'id': 'wide', 'kind': 'throughput', 'calib_commit': 'c' * 40, 'n': 3, 'm': 4,
          'text': 'the back end', 'predicted_bpc': 13.0, 'branch': 'agentic-track/r/wide',
          'goal': 'g', 'spec': 'SPECTEXT', 'prev_notes': 'NOTESTEXT'}
    budget, minutes, calib = propose.session_limits(tr, {'best_commit': 'b' * 40})
    nb, nm, ncal = propose.session_limits(None, {'best_commit': 'b' * 40})
    check('v3 tracks: a track session gets the track budget, time and calibration commit',
          (budget, minutes, calib) == (40.0, 120, 'c' * 40)
          and (nb, nm, ncal) == (float(propose.CONFIG['session_budget_usd']),
                                 int(propose.CONFIG['session_timeout_min']), 'b' * 40))
    text = propose.track_system_text(tr)
    design = propose.track_system_text(dict(tr, kind='design', base_bpc=9.7, why='W',
                                            planned=[{'kind': 'golden', 'text': 'P1'}]))
    first = propose.build_user_prompt(
        {'goal_text': 'g', 'iteration': 1, 'max_iters': 2, 'progress_text': 'p',
         'state_text': 's', 'profile_text': 'pr', 'lever': {'lever': 'w', 'reason': 'r'},
         'skills_text': 'sk', 'history_text': ''},
        {'focus': 'f', 'hypothesis': 'h', 'track': tr}, [])
    check('v3 tracks: the step brief carries the step, its prediction and the unit-test '
          'command, the first message the spec and the notes; the design brief the '
          'planned steps',
          'step (3 of 4, kind throughput): the back end' in text
          and 'Predicted bytes/cycle after this step: 13.00' in text and '90%' in text
          and 'SPECTEXT' not in text and 'NOTESTEXT' not in text
          and 'SPECTEXT' in first and 'NOTESTEXT' in first and '@@' not in first
          and 'python agentic/check.py --unit <name>' in text and '@@' not in text
          and 'Do not change rtl/' in design and 'P1' in design and '9.70' in design
          and '@@' not in design, text[:300])
    big = dict(tr, spec='S' * 12000, prev_notes='N' * 6000)
    sysprompt = propose.SYSTEM_BRIEF + propose.track_system_text(big)
    check('v3 tracks: a full spec and full notes stay out of the system prompt, which '
          'the SDK passes on the command line (Windows caps it at 32,767 characters; '
          'i58 failed to launch at about 31k plus escapes)',
          len(sysprompt) < 16000 and 'S' * 100 not in sysprompt, str(len(sysprompt)))
    golden = propose.track_system_text(dict(tr, kind='golden'))
    check('v3 tracks: a golden step is told it is checked for correctness only',
          'correctness only' in golden and 'Predicted' not in golden)
    state = {'iterations': [{'candidates': [
        {'model': 'm', 'session': {'cost_usd': 40.0, 'seconds': 600}, 'track': {'id': 'w'}},
        {'model': 'm', 'session': {'cost_usd': 2.0, 'seconds': 120}}]}]}
    check('v3 tracks: the burn rate skips track sessions', propose.burn_rate(state, 'm') == 1.0)
    prompt = propose.build_user_prompt(
        {'goal_text': 'g', 'iteration': 1, 'max_iters': 2, 'progress_text': 'p',
         'state_text': 's', 'profile_text': 'pr', 'lever': {'lever': 'w', 'reason': 'r'},
         'skills_text': 'sk', 'history_text': ''},
        {'focus': 'f', 'hypothesis': 'h', 'track': dict(tr, kind='design')}, [], resumed=True)
    check('v3 tracks: a resumed design session is told its work is in docs/, not rtl/',
          prompt.startswith('YOU HAVE ALREADY STARTED THIS DESIGN') and 'docs/track-wide/' in prompt)


def test_git_revisions_confined_to_the_run():
    import shutil
    import subprocess
    import tempfile
    import propose
    check('gate: git_revs finds REV, REV:PATH and both ends of a range, never options or paths',
          propose.git_revs('git show abc123:rtl/x.vhd') == ['abc123']
          and propose.git_revs('git log --oneline -n 5 a..b') == ['a', 'b']
          and propose.git_revs('git diff --stat HEAD~2 -- rtl docs') == ['HEAD~2']
          and propose.git_revs('git status') == [])
    check('gate: the run is read from the worktree path',
          propose.run_of_worktree('C:/x/.agentic/runs/r7/iter-3/c1') == 'r7')
    tmp = tempfile.mkdtemp(prefix='ktest-revs-')
    try:
        def git(*args):
            return subprocess.run(['git', '-C', tmp] + list(args), capture_output=True,
                                  text=True, check=True).stdout.strip()
        git('init', '-q')
        git('config', 'user.email', 't@t')
        git('config', 'user.name', 't')
        git('commit', '-q', '--allow-empty', '-m', 'base')
        git('branch', '-m', 'agentic/r1')
        base = git('rev-parse', 'HEAD')
        git('checkout', '-q', '-b', 'elsewhere')
        git('commit', '-q', '--allow-empty', '-m', 'not this run')
        outside = git('rev-parse', 'HEAD')
        git('checkout', '-q', '-b', 'agentic-cand/r1/i2-c1', base)
        git('commit', '-q', '--allow-empty', '-m', 'a candidate')
        cand = git('rev-parse', 'HEAD')
        git('checkout', '-q', 'agentic/r1')
        check('gate: a commit outside the run is refused by id and by branch name; '
              'HEAD, its ancestors and the run\'s candidate branches are allowed '
              '(i59 copied a design from outside the run by its commit id)',
              not propose.rev_in_run(tmp, outside[:7], 'r1')
              and not propose.rev_in_run(tmp, 'elsewhere', 'r1')
              and propose.rev_in_run(tmp, 'HEAD', 'r1')
              and propose.rev_in_run(tmp, base, 'r1')
              and propose.rev_in_run(tmp, cand[:7], 'r1')
              and propose.rev_in_run(tmp, 'agentic-cand/r1/i2-c1', 'r1')
              and not propose.rev_in_run(tmp, cand, 'r2'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_writable_for_tracks():
    import asyncio
    import propose
    if not propose.SDK_OK:
        print('skip  track gate (claude_agent_sdk not importable)')
        return
    root = os.path.join(KTEST, 'gate')
    os.makedirs(root, exist_ok=True)

    def allowed(writable, rel, tool='Write', cmd=None):
        gate = propose.make_gate(root, None, writable)
        inp = {'command': cmd} if cmd else {'file_path': os.path.join(root, rel)}
        res = asyncio.run(gate('Bash' if cmd else tool, inp, None))
        return isinstance(res, propose.PermissionResultAllow)
    design = propose.track_writable('wide', 'design')
    build = propose.track_writable('wide', 'golden')
    check('v3 tracks: the design step writes only its docs; a build step rtl/ and its docs',
          not allowed(design, 'rtl/top.vhd') and allowed(design, 'docs/track-wide/SPEC.md')
          and allowed(design, 'PROPOSAL.json') and not allowed(design, 'docs/track-other/x.md')
          and allowed(build, 'rtl/unit/golden_tb.vhd') and allowed(build, 'docs/track-wide/STEP-1.md')
          and not allowed(propose.WRITABLE, 'docs/track-wide/SPEC.md'))
    check('v3 tracks: the unit-test command is allowed only in its exact form',
          propose.command_kind('python agentic/check.py --unit golden_tb') == 'check'
          and propose.command_kind('python agentic/check.py --unit a;rm') is None
          and propose.command_kind('python agentic/check.py --unit ' + 'x' * 41) is None)


def test_check_unit_arguments_and_golden_data():
    import check as check_mod
    tmp = os.path.join(KTEST, 'golden')
    n = check_mod.write_golden(tmp, shapes=[('medium', 1024)])
    with open(os.path.join(tmp, 'expected.hex')) as fil:
        hexes = fil.read().splitlines()
    with open(os.path.join(tmp, 'elements.tv')) as fil:
        els = fil.read().splitlines()
    plain = b''
    for line in hexes:
        if line != 'EOC':
            word, count = line.split()
            plain += bytes.fromhex(word)[:int(count)]
    import stim
    from snappy import decompress_raw
    want = b''.join(decompress_raw(c) for c in stim.selftest_chunks(1024))
    check('v3 tracks: the unit golden data is the reference output, chunk by chunk',
          plain == want and hexes.count('EOC') == n and els.count('EOC') == n
          and all(len(ln.split()) == 5 for ln in els if ln != 'EOC'))
    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        bad = check_mod.run_unit('a;b')
        missing = check_mod.run_unit('no_such_unit')
    check('v3 tracks: check.py --unit refuses a bad name and a missing file',
          bad == 2 and missing == 2 and 'does not exist' in out.getvalue())
    check('v3 tracks: the unit verdict needs a clean end, not a stop-time',
          check_mod.unit_verdict('UNIT_RC=0 STOPPED=0')[0]
          and not check_mod.unit_verdict('UNIT_RC=0 STOPPED=1')[0]
          and not check_mod.unit_verdict('UNIT_RC=1 STOPPED=0')[0]
          and not check_mod.unit_verdict('UNIT_ELAB_FAIL')[0])


UNIT_TB = """\
library ieee;
use ieee.std_logic_1164.all;
use std.textio.all;
entity %s is
end %s;
architecture tb of %s is
begin
  process
    file f : text;
    variable l : line;
    variable n : natural := 0;
  begin
    file_open(f, "elements.tv", read_mode);
    while not endfile(f) loop
      readline(f, l);
      n := n + 1;
    end loop;
    assert n > 100 report "too few elements" severity %s;
    report "elements lines: " & integer'image(n);
    std.env.finish;
  end process;
end tb;
"""


def test_check_unit_runs_under_ghdl():
    if os.environ.get('AGENTIC_FAST_TESTS'):
        print('skip  unit test under GHDL (AGENTIC_FAST_TESTS)')
        return
    import shutil
    import subprocess
    root = os.path.join(KTEST, 'unitroot')
    if os.path.isdir(root):
        shutil.rmtree(root)
    shutil.copytree(os.path.join(ROOT, 'rtl'), os.path.join(root, 'rtl'))
    os.makedirs(os.path.join(root, 'agentic'))
    for name in ('check.py', 'measure.py', 'analyse.py', 'probe.py', 'packmodel.py', 'stim.py',
                 'oracle.py', 'tools.py', 'hacc.py', 'freeze.py', 'config.json'):
        shutil.copy(os.path.join(KIT, name), os.path.join(root, 'agentic', name))
    for sub in ('ref', 'syn', 'tb'):
        shutil.copytree(os.path.join(KIT, sub), os.path.join(root, 'agentic', sub),
                        ignore=shutil.ignore_patterns('lib', '__pycache__'))
    os.makedirs(os.path.join(root, 'rtl', 'unit'))
    with open(os.path.join(root, 'rtl', 'unit', 'count_tb.vhd'), 'w', newline='\n') as fil:
        fil.write(UNIT_TB % ('count_tb', 'count_tb', 'count_tb', 'failure'))
    with open(os.path.join(root, 'rtl', 'unit', 'fail_tb.vhd'), 'w', newline='\n') as fil:
        fil.write((UNIT_TB % ('fail_tb', 'fail_tb', 'fail_tb', 'failure')).replace(
            'n > 100', 'n > 100000000'))
    got = []
    for name in ('count_tb', 'fail_tb'):
        res = subprocess.run([sys.executable, os.path.join('agentic', 'check.py'), '--unit', name],
                             cwd=root, capture_output=True, text=True, timeout=1500)
        got.append((res.returncode, (res.stdout.strip().splitlines() or [''])[-1]))
    check('v3 tracks: a unit test runs under GHDL on golden data: one that ends passes, one '
          'that asserts fails',
          got[0][0] == 0 and 'UNIT PASS count_tb' in got[0][1]
          and got[1][0] == 1 and 'UNIT FAIL fail_tb' in got[1][1], str(got))


def test_track_history_and_prewait():
    import loop
    c = dict(_tc(), outcome='track_step_passed', reason='byte-exact on every draw',
             track={'id': 'wide', 'step': 2, 'kind': 'ports'})
    text = loop.history_text({'iterations': [{'iteration': 57, 'candidates': [c]}]})
    check('v3 tracks: the history shows a track step as a step, not a rejected candidate',
          text == '- i57 t1a1 track wide step 2 (ports): passed -- byte-exact on every draw',
          text)
    check('v3 tracks: the window pre-wait scales with the longest slot, capped at an hour',
          loop.prewait_s({'tracks': [_track()]}) == 3600
          and loop.prewait_s({'tracks': []}) == min(60, int(loop.CONFIG['session_timeout_min'])) * 60)
    check('v3 tracks: track candidates are never retried and move no skill counter',
          not loop.retry_eligible(dict(c, outcome='candidate', measured={'gain_pct': 50.0}), [])
          and 'track_step_short' in loop.NOT_MEASURED)


def test_track_candidates_do_not_touch_skills():
    import loop
    skills = seed()
    before = json.dumps(skills, sort_keys=True)
    c = dict(_tc(), outcome='track_step_short', adopted=False, advantage=None,
             direction={'skill_ids': [list(skills.get('skills', {}) or ['x'])[0]
                                      if isinstance(skills.get('skills'), dict) else 'x']},
             primary_skill=None, id='t')
    loop.record_outcomes(skills, [c])
    changed, _lessons = loop.learn_step({}, [c], skills, KTEST, lambda m: None, fake=True)
    check('v3 tracks: a track step moves no skill counter and is not in the learner\'s group',
          json.dumps(skills, sort_keys=True) == before and changed == [])


def kitcheck_modules():
    """Every agentic/kitcheck_*.py, imported, with this file's check() in it.

    Separate files so changes made in parallel can each add checks without
    all editing this one; there is still one FAILED list and one verdict.
    """
    import importlib
    mods = []
    for fname in sorted(os.listdir(KIT)):
        if fname.startswith('kitcheck_') and fname.endswith('.py'):
            mod = importlib.import_module(fname[:-3])
            mod.check = check
            mods.append(mod)
    return mods


def main():
    for name, func in sorted(globals().items()):
        if name.startswith('test_') and callable(func):
            func()
    for mod in kitcheck_modules():
        for name, func in sorted(vars(mod).items()):
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
