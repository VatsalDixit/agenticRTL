#!/usr/bin/env python3
"""
Paper figures from one or two loop runs, in the manner of Dr. RTL's.

    python agentic/paper_figs.py RUN_DIR [RUN_DIR2] --out DIR
    python agentic/paper_figs.py RUNS/fastguard200 RUNS/fastguard200-notracks \\
        --labels "tracks on,tracks off" --out figs

RUN_DIR is a run's folder (.agentic/runs/<run>) or its state.json. Standard
library, numpy and matplotlib only, so it runs on the HACC host. Writes, for
each figure, <name>.png (2x), <name>.pdf and <name>.json (every plotted number):

  trajectory  the search, like Dr. RTL's Fig. 5: one panel per metric
              (throughput, bytes/cycle, f_max, area), x the iteration. Every
              measured candidate is a dot: filled when correct and measured,
              an x when it failed correctness, compilation or synthesis, hollow
              when the tools failed (measure_error). A failure with no number
              for that metric sits on the floor of the panel. The line is the
              best design as a guard confirmed it (stars where it moved), the
              grey hairline the baseline.
              A fast iteration simulates a subset of the tables and does not
              synthesise (its f_max is the parent's, carried), so its numbers
              are not comparable with a full measurement: throughput and
              bytes/cycle panels show only candidates of full and guard
              iterations, f_max and area only synthesised ones (full, guard,
              clock), and the best line moves only where the best was guarded
              (record['best_guarded'], as ab_tracks.series() reads it), so a
              design adopted in a fast iteration gets its star at the guard
              that confirmed it, and a confirmed design can sit lower (its
              f_max fell). A strip along the x axis marks the fast and clock
              iterations (one strip per run when the two schedules differ); a
              triangle over it a guard that rolled the best back. Candidates
              far off the best line's range are carets on the panel edge.
  outcomes    candidates per iteration, stacked by outcome (adopted, candidate,
              track step passed / other, no_gain, regressed, failed_*,
              measure_error, no_proposal, other = too_expensive and
              ram_latency_changed); one panel per run, fast iterations marked
              under the axis as in the trajectory.
  cost        the guarded best throughput against cumulative model cost in
              dollars (record['cost_usd'], summed the way ab_tracks.py does;
              attempts discarded at a usage-limit wait are not in the records
              and are reported in the JSON only).

With two runs (a tracks-on and a tracks-off arm, say) the runs are drawn in
two colours with a legend. The style constants and setup() are copied from
the deck's figstyle.py so the figures match the slides.
"""

import argparse
import json
import logging
import math
import os
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt                         # noqa: E402
from matplotlib import font_manager                     # noqa: E402
from matplotlib.lines import Line2D                     # noqa: E402
from matplotlib.patches import Patch, Rectangle         # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402
import matplotlib.transforms as mtransforms             # noqa: E402


# ---------------------------------------------------------------------------
# style: copied from the deck's figstyle.py (constants and setup()), plus the
# few helpers every figure uses

SURFACE = '#FCFCFB'
INK = '#0B0B0B'        # primary text
INK2 = '#52514E'       # secondary text, axis labels, tick labels
MUTED = '#6B6964'      # least important text (still >= 4.5:1)
GRID = '#E1E0D9'       # hairline grid
AXIS = '#C3C2B7'       # baseline / spines
BLUE = '#2a78d6'       # series 1
ORANGE = '#eb6834'     # series 2
AQUA = '#1baf7a'       # series 3 (low contrast: always direct-label it)
GRAY = '#B5B3AC'       # de-emphasised marks (baseline, context)
BLUE_LIGHT = '#86b6ef'  # lighter step of series 1 (same entity, other state)
ORANGE_LIGHT = '#f4a07e'
TEXT_ACCENT = '#1C5CAB'  # accent for text (6.5:1), never a mark colour
SEQ_BLUE = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b']

MIN_PT = 17.3                   # 24 px on the slide
BASE_PT = 18


def setup():
    names = {f.name for f in font_manager.fontManager.ttflist}
    family = 'Inter' if 'Inter' in names else 'DejaVu Sans'
    params = {
        'font.family': family,
        'font.size': BASE_PT,
        'axes.titlesize': BASE_PT,
        'axes.labelsize': BASE_PT,
        'xtick.labelsize': BASE_PT,
        'ytick.labelsize': BASE_PT,
        'legend.fontsize': BASE_PT,
        'figure.facecolor': SURFACE,
        'axes.facecolor': SURFACE,
        'savefig.facecolor': SURFACE,
        'axes.edgecolor': AXIS,
        'axes.linewidth': 1.0,
        'axes.labelcolor': INK2,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.grid': False,
        'grid.color': GRID,
        'grid.linewidth': 1.0,
        'grid.linestyle': '-',
        'xtick.color': AXIS,
        'ytick.color': AXIS,
        'xtick.labelcolor': INK2,
        'ytick.labelcolor': INK2,
        'xtick.major.size': 0,
        'ytick.major.size': 0,
        'xtick.major.pad': 8,
        'ytick.major.pad': 8,
        'legend.frameon': False,
        'legend.handlelength': 1.2,
        'legend.labelcolor': INK2,
        'lines.linewidth': 2.0,
        'lines.solid_capstyle': 'round',
        'lines.solid_joinstyle': 'round',
        'lines.markersize': 9,
        'patch.linewidth': 0,
        'mathtext.fontset': 'cm',
        'text.color': INK,
        # paper copies: TrueType in the PDF, so the text stays text
        'pdf.fonttype': 42,
        'hatch.linewidth': 1.2,
    }
    # an older matplotlib (the HACC host's, say) lacks a few of these keys
    plt.rcParams.update(dict((k, v) for k, v in params.items() if k in plt.rcParams))
    # fontTools complains about the timestamps inside some font files when
    # it subsets them into the PDF; nothing is wrong with the output
    logging.getLogger('fontTools').setLevel(logging.ERROR)
    return family


# Two runs: the first in blue, the second in orange; candidates in the light
# step of their run's colour, the best line in the full step.
RUN_COLOURS = [(BLUE, BLUE_LIGHT), (ORANGE, ORANGE_LIGHT)]


def new_figure(w_px, h_px):
    """A figure of w x h display pixels (1 inch = 100 px), constrained layout."""
    size = (w_px / 100.0, h_px / 100.0)
    try:
        fig = plt.figure(figsize=size, dpi=100, layout='constrained')
    except TypeError:                     # matplotlib older than 3.5
        fig = plt.figure(figsize=size, dpi=100, constrained_layout=True)
    return fig


def value_grid(ax, axis='y'):
    """Hairline grid on the value axis only, behind the marks."""
    ax.set_axisbelow(True)
    ax.grid(True, axis=axis, color=GRID, linewidth=1.0, linestyle='-')


def panel_label(ax, letter, x=-0.02, y=1.02):
    ax.text(x, y, '(%s)' % letter, transform=ax.transAxes, ha='right', va='bottom',
            fontsize=BASE_PT, fontweight='bold', color=INK)


def check(fig):
    """Raise if any visible text is below MIN_PT."""
    small = []
    for t in fig.findobj(matplotlib.text.Text):
        if t.get_visible() and t.get_text().strip() and t.get_fontsize() < MIN_PT - 0.01:
            small.append((t.get_text()[:30], t.get_fontsize()))
    if small:
        raise ValueError('text below %.1f pt: %s' % (MIN_PT, small[:8]))


def save(fig, out_dir, name, data, formats):
    check(fig)
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for fmt in formats:
        path = os.path.join(out_dir, '%s.%s' % (name, fmt))
        fig.savefig(path, dpi=200)
        paths.append(path)
    plt.close(fig)
    path = os.path.join(out_dir, name + '.json')
    with open(path, 'w', encoding='utf-8', newline='\n') as fil:
        json.dump(data, fil, indent=1, sort_keys=True)
    paths.append(path)
    return paths


def fit_legend(fig, host, handles, ncol, **kw):
    """A legend on ``host`` with at most ``ncol`` columns, fewer if it would
    be wider than the figure (long run names, a font without Inter's widths)."""
    limit = 0.97 * fig.get_figwidth() * fig.dpi
    renderer = fig.canvas.get_renderer()
    for n in range(max(1, ncol), 0, -1):
        leg = host.legend(handles=handles, ncol=n, **kw)
        if n == 1 or leg.get_window_extent(renderer=renderer).width <= limit:
            return leg
        leg.remove()
    return leg


def tex(text):
    """A dollar sign that is not a mathtext delimiter."""
    return text.replace('$', r'\$')


# ---------------------------------------------------------------------------
# reading a run record (the format agentic/loop.py writes)

# (key, axis label, the iteration kinds whose candidate numbers are comparable
# with a full measurement for this metric)
FULL_KINDS = ('full', 'guard')            # every table simulated, synthesised
SYNTH_KINDS = ('full', 'guard', 'clock')  # synthesised (clock: a subset of tables)
METRICS = (('throughput_gbps', 'Throughput (GB/s)', FULL_KINDS),
           ('bytes_per_cycle', 'Bytes/cycle', FULL_KINDS),
           ('f_max_mhz', 'f$_\\mathregular{max}$ (MHz)', SYNTH_KINDS),
           ('area', 'Area', SYNTH_KINDS))
KEYS = tuple(m[0] for m in METRICS)
# evaluate()'s percentages, for records written before candidates carried
# their absolute numbers ('absolute')
PCT_KEYS = {'throughput_gbps': 'throughput_gain_pct', 'bytes_per_cycle': 'bpc_gain_pct',
            'f_max_mhz': 'fmax_gain_pct', 'area': 'area_gain_pct'}

# evaluate() and the track judging, grouped by how a dot is drawn
FAILED = ('failed_correctness', 'failed_compile', 'failed_synth', 'track_step_failed')
ERRORED = ('measure_error', 'track_never_measured')
NOT_MEASURED = ('no_proposal', 'track_design_done', 'track_design_invalid',
                'track_session_failed', 'track_pending')
TRACK_PASSED = ('track_step_passed', 'track_design_done', 'track_final_pending')


def metric_value(d, key):
    d = d or {}
    if key == 'area':
        # `area` is LUTs from Vivado, um2 from Yosys; older records only
        # carry area_um2 (tools.area_of)
        if d.get('area') is not None:
            return d['area']
        if d.get('area_um2') is not None:
            return d['area_um2']
        return d.get('luts')
    return d.get(key)


def area_unit(state):
    for d in [state.get('baseline')] + [it.get('best_after')
                                        for it in state.get('iterations') or []]:
        if (d or {}).get('area_unit'):
            return d['area_unit']
        if (d or {}).get('area_um2') is not None:
            return 'um2'
    return ''


def area_axis_label(unit):
    return {'LUTs': 'LUTs', 'um2': 'Area ($\\mu$m$^2$)'}.get(unit, 'Area (%s)' % unit
                                                              if unit else 'Area')


def load_state(path):
    if os.path.isdir(path):
        path = os.path.join(path, 'state.json')
    with open(path, encoding='utf-8') as fil:
        return json.load(fil), path


def rec_cost(rec):
    if rec.get('cost_usd') is not None:
        return float(rec['cost_usd'])
    return float(sum(((c.get('session') or {}).get('cost_usd') or 0)
                     for c in rec.get('candidates') or []))


def dot_class(c, has_number):
    """'ok' (filled), 'failed' (x), 'error' (hollow) or None (not drawn)."""
    out = c.get('outcome') or ''
    if out in FAILED:
        return 'failed'
    if out in ERRORED:
        return 'error'
    if out in NOT_MEASURED or not has_number:
        return None
    return 'ok'


def outcome_group(c):
    out = c.get('outcome') or ''
    if out == 'adopted' or c.get('adopted'):
        return 'adopted'
    if out in ('candidate', 'no_gain', 'regressed', 'failed_correctness',
               'failed_compile', 'failed_synth', 'measure_error', 'no_proposal'):
        return out
    if out in TRACK_PASSED:
        return 'track_passed'
    if out.startswith('track_'):
        return 'track_other'
    return 'other'           # too_expensive (an old rule), ram_latency_changed, unset


class Run(object):
    """One run record, read the way loop.py writes it and ab_tracks reads it."""

    def __init__(self, state, label):
        self.state = state
        self.label = label
        self.base = state.get('baseline') or {}
        self.unit = area_unit(state)
        recs = [r for r in state.get('iterations') or [] if r.get('iteration') is not None]
        recs.sort(key=lambda r: r['iteration'])
        self.recs = recs
        self.last = recs[-1]['iteration'] if recs else 0
        base_vals = dict((k, metric_value(self.base, k)) for k in KEYS)
        self.kinds = {}           # iteration -> full | guard | clock | fast
        self.best = [(0, base_vals)]   # (iteration, values) where a guard confirmed
        self.promotions = []      # iterations where the guarded best moved
        self.rollbacks = []
        self.dots = []            # every candidate, with its numbers
        self.outcomes = []        # (iteration, {group: count})
        self.cost = [(0.0, base_vals['throughput_gbps'], 0)]  # (cum $, tp, iteration)
        self.cost_promotions = []
        spent = 0.0
        guarded = dict(base_vals)
        parent = self.base
        for rec in recs:
            k = rec['iteration']
            kind = rec.get('measure_kind') or 'full'
            self.kinds[k] = kind
            if (rec.get('guard') or {}).get('kind') == 'rollback':
                self.rollbacks.append(k)
            counts = {}
            for c in rec.get('candidates') or []:
                grp = outcome_group(c)
                counts[grp] = counts.get(grp, 0) + 1
                self.dots.append(self._dot(c, k, kind, parent))
            self.outcomes.append((k, counts))
            after = rec.get('best_after') or {}
            vals = dict((key, metric_value(after, key)) for key in KEYS)
            moved = False
            if rec.get('best_guarded', True) and any(v is not None for v in vals.values()):
                moved = any(_differs(vals[key], guarded[key]) for key in KEYS)
                if moved:
                    self.promotions.append(k)
                guarded = vals
                self.best.append((k, dict(vals)))
            if any(v is not None for v in vals.values()):
                parent = after
            spent += rec_cost(rec)
            self.cost.append((spent, guarded['throughput_gbps'], k))
            if moved and _differs(guarded['throughput_gbps'], self.cost[-2][1]):
                self.cost_promotions.append(len(self.cost) - 1)
        self.guarded_final = guarded
        self.spent = spent

    @staticmethod
    def _dot(c, k, kind, parent):
        abs_ = c.get('absolute') or {}
        meas = c.get('measured') or {}
        vals = {}
        for key in KEYS:
            v = metric_value(abs_, key) if abs_ else None
            if v is None and not abs_ and kind == 'full':
                # a record from before 'absolute': rebuilt from the
                # percentage against the design it started from (gui.py does
                # the same)
                pct, ref = meas.get(PCT_KEYS[key]), metric_value(parent, key)
                if pct is not None and ref is not None:
                    v = ref * (1.0 + pct / 100.0)
            vals[key] = v
        return {'iteration': k, 'kind': kind, 'label': c.get('label'), 'id': c.get('id'),
                'outcome': c.get('outcome') or '', 'track': bool(c.get('track')),
                'adopted': bool(c.get('adopted')), 'values': vals}

    def best_steps(self, key):
        """x, y for a steps-post line of the guarded best through the last
        iteration (held flat across iterations that did not confirm one)."""
        xs, ys = [], []
        for k, vals in self.best:
            if vals.get(key) is not None:
                xs.append(k)
                ys.append(vals[key])
        if xs and xs[-1] < self.last:
            xs.append(self.last)
            ys.append(ys[-1])
        return xs, ys

    def best_value(self, key, k):
        val = None
        for i, vals in self.best:
            if i > k:
                break
            if vals.get(key) is not None:
                val = vals[key]
        return val


def _differs(a, b):
    if a is None or b is None:
        return a is not b
    return abs(a - b) > 1e-9 * max(1.0, abs(a), abs(b))


def fast_iterations(run):
    return sorted(k for k, kind in run.kinds.items() if kind not in FULL_KINDS)


def spans(iters):
    """[(first, last)] of consecutive runs of iteration numbers."""
    out = []
    for k in iters:
        if out and k == out[-1][1] + 1:
            out[-1][1] = k
        else:
            out.append([k, k])
    return [tuple(s) for s in out]


# ---------------------------------------------------------------------------
# figure 1: the trajectory

OUT_SCALE = 1.5          # a candidate further than this many best-line spans
                         # beyond it is drawn as a caret on the panel's edge
MARK_OK = 9              # marker sizes in points
MARK_TRACK = 8
MARK_STAR = 17
BAND = MUTED             # the strip along the x axis under fast iterations
BAND_H = 0.012           # its height, in axes fraction
BAND_STEP = 0.02         # one strip per run when the runs' schedules differ
BAND_LW = 5              # its legend line, in points


def _limits(core, cands):
    """(in-scale lo, hi) for the candidates, given the values the panel is
    about (best line and baseline)."""
    core = [v for v in core if v is not None]
    if not core:
        core = [v for v in cands if v is not None] or [0.0, 1.0]
    lo, hi = min(core), max(core)
    span = max(hi - lo, 0.08 * max(abs(hi), abs(lo)), 1e-9)
    return lo - OUT_SCALE * span, hi + OUT_SCALE * span


def _ylim(values, f0, f1):
    """ylim placing min(values) at axes fraction f0 and max at f1."""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9 * max(1.0, abs(hi)):
        pad = 0.05 * abs(hi) or 1.0
        lo, hi = lo - pad, hi + pad
    total = (hi - lo) / (f1 - f0)
    bottom = lo - f0 * total
    return bottom, bottom + total


def draw_trajectory(runs, out_dir, formats):
    two = len(runs) == 2
    dodge = [-0.17, 0.17] if two else [0.0]
    last = max(r.last for r in runs)
    same_kinds = (not two) or all(runs[0].kinds.get(k) == runs[1].kinds.get(k)
                                  for k in range(1, min(runs[0].last, runs[1].last) + 1))
    any_fast = any(fast_iterations(r) for r in runs)

    fig = new_figure(1664, 1060)
    gs = fig.add_gridspec(3, 2, height_ratios=[0.16, 1, 1])
    lax = fig.add_subplot(gs[0, :])
    lax.axis('off')
    axes = [fig.add_subplot(gs[1 + r, c]) for r in (0, 1) for c in (0, 1)]
    data = {'runs': [], 'metrics': []}
    seen = set()                       # which marks appear, for the legend

    for n, (ax, (key, ylabel, kinds_ok)) in enumerate(zip(axes, METRICS)):
        if key == 'area':
            ylabel = area_axis_label(runs[0].unit)
        panel = {'metric': key, 'label': ylabel, 'runs': []}
        # what the panel is about: the best lines and the baselines
        core = []
        for run in runs:
            core += run.best_steps(key)[1]
            core.append(metric_value(run.base, key))
        cand_vals = [d['values'][key] for run in runs for d in run.dots
                     if d['kind'] in kinds_ok and d['values'][key] is not None]
        lo_ok, hi_ok = _limits(core, cand_vals)

        # lanes at the bottom of the panel: fast band, failures with no
        # number, low off-scale carets
        y = 0.0
        lanes = {}
        if any_fast:
            nb = 1 if same_kinds else len(runs)
            lanes['band'] = [(y + BAND_STEP * j, y + BAND_STEP * j + BAND_H)
                             for j in range(nb)]
            y += BAND_STEP * nb + 0.012
        plotted = []
        floor, low, high = [], [], []
        for ri, run in enumerate(runs):
            for d in run.dots:
                if d['kind'] not in kinds_ok:
                    continue
                v = d['values'][key]
                cls = dot_class(d, v is not None)
                if cls is None:
                    continue
                x = d['iteration'] + dodge[ri]
                if v is None:
                    floor.append((ri, x, cls, d))
                elif v < lo_ok:
                    low.append((ri, x, cls, d))
                elif v > hi_ok:
                    high.append((ri, x, cls, d))
                else:
                    plotted.append((ri, x, v, cls, d))
        if floor:
            lanes['floor'] = y + 0.03
            y += 0.06
        if low:
            lanes['low'] = y + 0.03
            y += 0.06
        f0 = y + 0.05 if y else 0.04
        f1 = 0.93 if high else 0.97
        in_scale = [v for v in core if v is not None] + [p[2] for p in plotted]
        bottom, top = _ylim(in_scale or [0.0, 1.0], f0, f1)
        # the value axis (spine, ticks, grid) stops above the lanes, so a
        # mark on a lane never reads as a value
        axis_lo = bottom + max(0.0, f0 - 0.03) * (top - bottom)
        ticks = [t for t in MaxNLocator(nbins=5).tick_values(axis_lo, top)
                 if axis_lo - 1e-9 <= t <= top + 1e-9] if in_scale else []
        ax.set_yticks(ticks)
        ax.set_ylim(bottom, top)
        ax.spines['left'].set_bounds(axis_lo, top)
        if not in_scale:
            ax.text(0.5, (f0 + f1) / 2.0, 'not in the record', transform=ax.transAxes,
                    ha='center', va='center', color=MUTED, fontsize=BASE_PT)
        ax.set_xlim(-0.8, last + 0.8)
        lane_tf = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)

        # baseline hairlines
        bases = sorted(set(metric_value(r.base, key) for r in runs
                           if metric_value(r.base, key) is not None))
        for b in bases:
            ax.axhline(b, color=GRAY, linewidth=1.0, zorder=1)

        # candidates
        for ri, x, v, cls, d in plotted:
            _dot(ax, x, v, cls, d['track'], RUN_COLOURS[ri], ax.transData)
            seen.add('track' if (d['track'] and cls == 'ok') else cls)
        for ri, x, cls, d in floor:
            _dot(ax, x, lanes['floor'], cls, d['track'], RUN_COLOURS[ri], lane_tf)
            seen.add(cls)
            seen.add('floor')
        for ri, x, cls, d in low:
            ax.plot([x], [lanes['low']], 'v', color=RUN_COLOURS[ri][1], ms=8, mec=SURFACE,
                    mew=0.8, transform=lane_tf, clip_on=False, zorder=3)
            seen.add('offscale')
        for ri, x, cls, d in high:
            ax.plot([x], [0.975], '^', color=RUN_COLOURS[ri][1], ms=8, mec=SURFACE,
                    mew=0.8, transform=lane_tf, clip_on=False, zorder=3)
            seen.add('offscale')

        # the guarded best, stars where it moved
        for ri, run in enumerate(runs):
            full, light = RUN_COLOURS[ri]
            xs, ys = run.best_steps(key)
            if xs:
                ax.plot(xs, ys, drawstyle='steps-post', color=full, linewidth=2.4, zorder=4)
            sx = [k for k in run.promotions if run.best_value(key, k) is not None]
            sy = [run.best_value(key, k) for k in sx]
            ax.plot(sx, sy, '*', color=full, ms=MARK_STAR, mec=SURFACE, mew=1.2,
                    linestyle='none', zorder=5)
            panel['runs'].append({
                'run': run.label, 'best_guarded': [[x, y] for x, y in zip(xs, ys)],
                'promotions': sx,
                'candidates': [{'iteration': d['iteration'], 'id': d['id'],
                                'outcome': d['outcome'], 'track': d['track'],
                                'value': d['values'][key]}
                               for d in run.dots if d['kind'] in kinds_ok
                               and dot_class(d, d['values'][key] is not None)],
                'baseline': metric_value(run.base, key)})

        # fast iterations along the x axis; rollbacks on that band
        if any_fast:
            for j, (b0, b1) in enumerate(lanes['band']):
                src = runs if same_kinds else [runs[j]]
                col = BAND if same_kinds else RUN_COLOURS[j][1]
                for s0, s1 in spans(fast_iterations(src[0])):
                    ax.add_patch(Rectangle((s0 - 0.5, b0), s1 - s0 + 1, b1 - b0,
                                           transform=lane_tf, facecolor=col,
                                           edgecolor='none', zorder=2, clip_on=False))
            for ri, run in enumerate(runs):
                j = 0 if same_kinds else ri
                b0, b1 = lanes['band'][j]
                xs = [k + dodge[ri] for k in run.rollbacks]
                if xs:
                    ax.plot(xs, [b1 + 0.014] * len(xs), 'v',
                            color=RUN_COLOURS[ri][0], ms=10, mec=SURFACE, mew=1.0,
                            transform=lane_tf, clip_on=False, linestyle='none', zorder=6)
                    seen.add('rollback')

        value_grid(ax)
        ax.set_ylabel(ylabel)
        if n >= 2:
            ax.set_xlabel('Iteration')
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=8))
        if key == 'area':
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: '{:,.0f}'.format(v)))
        panel_label(ax, 'abcd'[n])
        data['metrics'].append(panel)

    # legend
    handles = []
    for ri, run in enumerate(runs):
        full = RUN_COLOURS[ri][0]
        handles.append(Line2D([], [], color=full, linewidth=2.4, marker='*', ms=MARK_STAR,
                              mec=SURFACE, mew=1.2,
                              label=('best, guarded: %s' % run.label) if two
                              else 'best design (guarded)'))
    neutral = GRAY if two else BLUE_LIGHT
    strong = INK2 if two else BLUE
    if 'ok' in seen:
        handles.append(Line2D([], [], color=neutral, marker='o', ms=MARK_OK, mec=SURFACE,
                              linestyle='none', label='candidate, correct'))
    if 'track' in seen:
        handles.append(Line2D([], [], color=neutral, marker='D', ms=MARK_TRACK, mec=SURFACE,
                              linestyle='none', label='track step, correct'))
    if 'failed' in seen:
        handles.append(Line2D([], [], color=strong, marker='x', ms=MARK_OK, mew=2.0,
                              linestyle='none',
                              label='failed' + (' (floor: no number)' if 'floor' in seen
                                                else '')))
    if 'error' in seen:
        handles.append(Line2D([], [], color=strong, marker='o', ms=MARK_OK, mfc=SURFACE,
                              mew=1.6, linestyle='none', label='measure error'))
    handles.append(Line2D([], [], color=GRAY, linewidth=1.0, label='baseline'))
    if any_fast:
        handles.append(Line2D([], [], color=BAND if same_kinds else neutral,
                              linewidth=BAND_LW, solid_capstyle='butt',
                              label='fast iteration' if same_kinds
                              else 'fast iteration (a strip per run)'))
    if 'rollback' in seen:
        handles.append(Line2D([], [], color=strong, marker='v', ms=10, mec=SURFACE,
                              linestyle='none', label='guard rollback'))
    if 'offscale' in seen:
        handles.append(Line2D([], [], color=neutral, marker='^', ms=8, mec=SURFACE,
                              linestyle='none', label='off scale'))
    fit_legend(fig, lax, handles, int(math.ceil(len(handles) / 2.0)), loc='center',
               borderaxespad=0.0, handletextpad=0.5, columnspacing=1.6, labelspacing=0.5,
               handlelength=1.6)

    for run in runs:
        data['runs'].append({'run': run.label, 'iterations': run.last,
                             'fast_iterations': fast_iterations(run),
                             'rollbacks': run.rollbacks, 'promotions': run.promotions,
                             'area_unit': run.unit})
    return save(fig, out_dir, 'trajectory', data, formats)


def _dot(ax, x, y, cls, track, colours, transform):
    full, light = colours
    marker = 'D' if track else 'o'
    if cls == 'ok':
        ax.plot([x], [y], marker, color=light, ms=MARK_TRACK if track else MARK_OK,
                mec=SURFACE, mew=0.8, transform=transform, clip_on=False, zorder=3)
    elif cls == 'failed':
        ax.plot([x], [y], 'x', color=full, ms=MARK_OK - 1, mew=1.8, transform=transform,
                clip_on=False, zorder=3)
    else:
        ax.plot([x], [y], marker, color=full, mfc=SURFACE, ms=MARK_OK - 1, mew=1.5,
                transform=transform, clip_on=False, zorder=3)


# ---------------------------------------------------------------------------
# figure 2: outcomes per iteration

# Stacked bottom to top. Families: blue = it could be adopted, aqua = a track
# step, grey = correct but not better, orange = broken, hatched/light = never
# measured.
OUTCOMES = (
    ('adopted', 'adopted', dict(color=BLUE)),
    ('candidate', 'adoptable, outscored', dict(color=BLUE_LIGHT)),
    ('track_passed', 'track step passed', dict(color=AQUA)),
    ('track_other', 'track step, other', dict(color='#8fd6bb')),
    ('no_gain', 'no gain', dict(color=GRAY)),
    ('regressed', 'regressed', dict(color='#86847D')),
    ('failed_correctness', 'failed correctness', dict(color=ORANGE)),
    ('failed_synth', 'failed synthesis', dict(color=ORANGE_LIGHT)),
    ('failed_compile', 'failed compile', dict(color='#B4471C')),
    ('measure_error', 'measure error', dict(color=SURFACE, hatch='////', hatch_col=GRAY)),
    ('no_proposal', 'no proposal', dict(color='#E3E1DB')),
    ('other', 'other', dict(color='#5F5D57')),
)


def draw_outcomes(runs, out_dir, formats):
    present = set(g for run in runs for _, counts in run.outcomes for g in counts)
    cats = [c for c in OUTCOMES if c[0] in present]
    last = max(r.last for r in runs)
    h = 600 if len(runs) == 1 else 900
    fig = new_figure(1664, h)
    gs = fig.add_gridspec(len(runs), 2, width_ratios=[1, 0.24])
    lax = fig.add_subplot(gs[:, 1])
    lax.axis('off')
    axes = [fig.add_subplot(gs[j, 0]) for j in range(len(runs))]
    data = {'runs': []}
    ymax = max([sum(c.values()) for run in runs for _, c in run.outcomes] + [1])
    band_lo, band_hi = -0.075 * ymax, -0.045 * ymax
    # the surface gap between segments and bars, thinner when the bars are
    # (a 200-iteration run would otherwise read as stripes)
    gap = max(0.4, min(1.0, 60.0 / max(1, last)))
    for j, (ax, run) in enumerate(zip(axes, runs)):
        its = [k for k, _ in run.outcomes]
        bottom = np.zeros(len(its))
        for grp, label, sty in cats:
            vals = np.array([counts.get(grp, 0) for _, counts in run.outcomes], dtype=float)
            if not vals.any():
                continue
            if sty.get('hatch'):
                ax.bar(its, vals, bottom=bottom, width=0.8, color=sty['color'],
                       hatch=sty['hatch'], edgecolor=sty['hatch_col'], linewidth=0, zorder=2)
                ax.bar(its, vals, bottom=bottom, width=0.8, color='none',
                       edgecolor=SURFACE, linewidth=gap, zorder=3)
            else:
                ax.bar(its, vals, bottom=bottom, width=0.8, color=sty['color'],
                       edgecolor=SURFACE, linewidth=gap, zorder=2)
            bottom += vals
        ax.axhline(0, color=AXIS, linewidth=1.0, zorder=1)
        ax.spines['bottom'].set_visible(False)
        fast = fast_iterations(run)
        for s0, s1 in spans(fast):
            ax.add_patch(Rectangle((s0 - 0.5, band_lo), s1 - s0 + 1, band_hi - band_lo,
                                   facecolor=BAND, edgecolor='none', zorder=2))
        if run.rollbacks:
            ax.plot(run.rollbacks, [(band_lo + band_hi) / 2.0] * len(run.rollbacks), 'v',
                    color=INK2, ms=10, mec=SURFACE, mew=1.0, linestyle='none', zorder=4)
        ax.set_xlim(0.3, last + 0.7)
        ax.set_yticks([t for t in MaxNLocator(integer=True, nbins=4).tick_values(0, ymax)
                       if 0 <= t <= ymax])
        ax.set_ylim(band_lo - 0.03 * ymax, ymax * 1.04)
        ax.spines['left'].set_bounds(0, ymax * 1.04)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=12))
        value_grid(ax)
        ax.set_ylabel('Candidates')
        if j == len(runs) - 1:
            ax.set_xlabel('Iteration')
        if len(runs) == 2:
            panel_label(ax, 'ab'[j])
            ax.text(0.0, 1.02, run.label, transform=ax.transAxes, ha='left', va='bottom',
                    color=INK, fontsize=BASE_PT)
        data['runs'].append({'run': run.label, 'per_iteration': [
            {'iteration': k, 'kind': run.kinds.get(k), 'counts': counts}
            for k, counts in run.outcomes],
            'totals': dict((g, sum(c.get(g, 0) for _, c in run.outcomes)) for g, _, _ in cats)})

    handles = []
    for grp, label, sty in reversed(cats):          # top of the stack first
        if sty.get('hatch'):
            handles.append(Patch(facecolor=sty['color'], hatch=sty['hatch'],
                                 edgecolor=sty['hatch_col'], linewidth=0, label=label))
        else:
            handles.append(Patch(facecolor=sty['color'], label=label))
    if any(fast_iterations(r) for r in runs):
        handles.append(Line2D([], [], color=BAND, linewidth=BAND_LW, solid_capstyle='butt',
                              label='fast iteration'))
    if any(r.rollbacks for r in runs):
        handles.append(Line2D([], [], color=INK2, marker='v', ms=10, mec=SURFACE,
                              linestyle='none', label='guard rollback'))
    lax.legend(handles=handles, loc='center left', ncol=1, borderaxespad=0.0,
               handletextpad=0.6, labelspacing=0.32, handlelength=1.0)
    return save(fig, out_dir, 'outcomes', data, formats)


# ---------------------------------------------------------------------------
# figure 3: best guarded throughput against dollars

def draw_cost(runs, out_dir, formats):
    fig = new_figure(1664, 600)
    gs = fig.add_gridspec(1, 2, width_ratios=[1, 0.42 if len(runs) == 2 else 0.3])
    ax = fig.add_subplot(gs[0, 0])
    lax = fig.add_subplot(gs[0, 1])
    lax.axis('off')
    data = {'runs': []}
    handles = []
    for ri, run in enumerate(runs):
        full = RUN_COLOURS[ri][0]
        pts = [(c, tp, k) for c, tp, k in run.cost if tp is not None]
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, drawstyle='steps-post', color=full, linewidth=2.4, zorder=4)
        stars = [run.cost[i] for i in run.cost_promotions if run.cost[i][1] is not None]
        ax.plot([s[0] for s in stars], [s[1] for s in stars], '*', color=full, ms=MARK_STAR,
                mec=SURFACE, mew=1.2, linestyle='none', zorder=5)
        base_tp = metric_value(run.base, 'throughput_gbps')
        gain = (100.0 * (ys[-1] / base_tp - 1.0)) if base_tp else None
        label = '%s%.3f GB/s%s\n$%.0f, %d iterations' % (
            (run.label + '\n') if len(runs) == 2 else 'best design, guarded\n', ys[-1],
            (' (%+.1f%%)' % gain) if gain is not None else '', xs[-1], run.last)
        handles.append(Line2D([], [], color=full, linewidth=2.4, marker='*', ms=MARK_STAR,
                              mec=SURFACE, mew=1.2, label=tex(label)))
        data['runs'].append({'run': run.label, 'baseline_gbps': base_tp,
                             'points': [{'cost_usd': round(c, 2), 'gbps': tp, 'iteration': k}
                                        for c, tp, k in pts],
                             'final_gbps': ys[-1], 'final_gain_pct': gain,
                             'spent_usd': round(run.spent, 2),
                             'discarded_usd': run.state.get('discarded_usd') or 0.0})
    bases = sorted(set(metric_value(r.base, 'throughput_gbps') for r in runs
                       if metric_value(r.base, 'throughput_gbps') is not None))
    for b in bases:
        ax.axhline(b, color=GRAY, linewidth=1.0, zorder=1)
    handles.append(Line2D([], [], color=GRAY, linewidth=1.0, label='baseline'))
    if len(runs) == 2 and all(r.spent > 0 for r in runs):
        equal = min(r.spent for r in runs)
        ax.axvline(equal, color=MUTED, linewidth=1.0, linestyle=(0, (4, 3)), zorder=1)
        handles.append(Line2D([], [], color=MUTED, linewidth=1.0, linestyle=(0, (4, 3)),
                              label=tex('equal spend ($%.0f)' % equal)))
        data['equal_spend_usd'] = round(equal, 2)
    ax.set_xlim(left=0)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: tex('$%.0f' % v)))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
    value_grid(ax)
    ax.set_xlabel('Cumulative model cost')
    ax.set_ylabel('Best throughput, guarded (GB/s)')
    lax.legend(handles=handles, loc='center left', ncol=1, borderaxespad=0.0,
               handletextpad=0.6, labelspacing=0.9, handlelength=1.8)
    return save(fig, out_dir, 'cost', data, formats)


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runs', nargs='+', metavar='RUN_DIR',
                    help='a run folder (.agentic/runs/<run>) or its state.json; one or two')
    ap.add_argument('--out', required=True, help='folder for the figures')
    ap.add_argument('--labels', default=None,
                    help='comma list naming the runs in the legends (default: run names)')
    ap.add_argument('--formats', default='png,pdf', help='comma list (default png,pdf)')
    args = ap.parse_args(argv)
    if len(args.runs) > 2:
        ap.error('one or two runs')
    labels = [s.strip() for s in args.labels.split(',')] if args.labels else []
    runs = []
    for n, path in enumerate(args.runs):
        state, where = load_state(path)
        label = (labels[n] if n < len(labels) and labels[n] else
                 state.get('run') or os.path.basename(os.path.dirname(os.path.abspath(where))))
        run = Run(state, label)
        if not run.recs:
            sys.exit('%s: no iterations recorded' % where)
        runs.append(run)
    if len(runs) == 2:
        if runs[0].unit != runs[1].unit:
            sys.stderr.write('note: the runs measure area in different units (%s, %s); '
                             'the area panel is labelled with the first\n'
                             % (runs[0].unit, runs[1].unit))
        b0, b1 = runs[0].base, runs[1].base
        if b0.get('bytes_per_cycle') != b1.get('bytes_per_cycle'):
            sys.stderr.write('note: the baselines differ (%s vs %s bytes/cycle): not one '
                             'starting design\n' % (b0.get('bytes_per_cycle'),
                                                    b1.get('bytes_per_cycle')))
    family = setup()
    formats = [f.strip() for f in args.formats.split(',') if f.strip()]
    written = []
    written += draw_trajectory(runs, args.out, formats)
    written += draw_outcomes(runs, args.out, formats)
    written += draw_cost(runs, args.out, formats)
    print('font: %s' % family)
    for path in written:
        print('wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
