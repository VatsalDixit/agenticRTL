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

    def run(fail_on):
        events, started = [], threading.Event()

        def fake_sim(rtl, build, ds, widths, timeout=None):
            names = [d.name for d, _p, _c in ds]
            events.append('sim ' + '+'.join(names))
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
            return {'f_max_mhz': 250.0, 'wns_ns': 0.0, 'area': 1000,
                    'area_unit': 'LUTs', 'synth_backend': 'hacc'}

        measure.simulate, measure.synthesize = fake_sim, fake_synth
        measure.draw_bytes = lambda e: 10 if e[1] == 'quick' else 10 ** 8
        measure.analyse.rtl_widths = lambda rtl: {}
        try:
            out = measure.measure('rtl', tempfile.mkdtemp(prefix='kit-measure-'), draws)
        finally:
            (measure.simulate, measure.synthesize, measure.draw_bytes,
             measure.analyse.rtl_widths) = saved
        return out, events

    out, events = run(None)
    check('small draws first, then synthesis runs while the long draws simulate',
          events[0] == 'sim small' and 'synthesis running' in events
          and out.get('throughput_gbps') == 1.25, '%s %s' % (events, out.get('throughput_gbps')))
    out, events = run('small')
    check('a design failing a small draw gets no synthesis and no long draws',
          events == ['sim small'] and 'f_max_mhz' not in out and not out['oracle_pass'],
          str(events))
    out, events = run('long')
    check('a design failing a long draw keeps no synthesis numbers',
          'synth' in events and 'f_max_mhz' not in out and not out['oracle_pass'],
          str(events))


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
