#!/usr/bin/env python3
"""
A window that shows what the loop is doing, live.

    python agentic/gui.py                 the newest run
    python agentic/gui.py --run NAME
    python agentic/gui.py --demo          replay a finished run, one phase a second

Three things on one screen:

  * THE LOOP as a flow diagram, with the box the run is in right now lit up,
    the iteration number, and how long it has been in that step.
  * AREA and CLOCK FREQUENCY per iteration: the line is the design the loop
    kept, the dots are every candidate it measured and threw away.
  * THROUGHPUT per iteration, which is the thing being optimised, plus the
    table of what each iteration adopted.

It only reads files (state.json and status.json under .agentic/runs/<run>/),
never writes, and it polls once a second. Nothing here can affect a run:
closing it, or its crashing, leaves the loop untouched.

Standard library only (tkinter), so it runs wherever the kit runs.
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

KIT = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(KIT)
RUNS = os.path.join(ROOT, '.agentic', 'runs')

POLL_MS = 1000

# ---------------------------------------------------------------------------
# palette

C = {
    'bg':       '#eef0ec',
    'panel':    '#ffffff',
    'ink':      '#1b2026',
    'muted':    '#6b7670',
    'faint':    '#9aa59f',
    'rule':     '#d7dcd4',
    'grid':     '#e8ebe6',
    'accent':   '#1f7a5c',   # active / adopted
    'accentbg': '#dff0e8',
    'warn':     '#b5741a',
    'warnbg':   '#f8ecda',
    'crit':     '#a3382f',
    'critbg':   '#f7e3e0',
    'area':     '#b5741a',
    'fmax':     '#2f6fb5',
    'thru':     '#1f7a5c',
    'dim':      '#f4f6f3',
}

# The steps the loop moves through, in order. The key is the phase string
# loop.py writes into status.json; keeping them equal is what makes the
# highlight honest rather than guessed.
SETUP_STEPS = [
    ('corpus',   'build stimulus',    'ten draws: 8 real Parquet page sets, 2 synthetic'),
    ('baseline', 'measure baseline',  'simulate every draw, then synthesise'),
]
LOOP_STEPS = [
    ('plan',    '1. analyse & plan',  'stage rates vs ceilings, then pick {n} direction{s}'),
    ('write',   '2. write candidates', '{n} Claude session{s}, each in its own git worktree'),
    ('measure', '3. measure each',    'oracle on every byte, bytes/cycle, f_max, area'),
    ('adopt',   '4. select & adopt',  'best score moves the run branch'),
    ('learn',   '5. learn skills',    'group advantage updates skills.json'),
]
ALL_PHASES = [s[0] for s in SETUP_STEPS + LOOP_STEPS]


# ---------------------------------------------------------------------------
# data

def list_runs():
    if not os.path.isdir(RUNS):
        return []
    out = []
    for name in os.listdir(RUNS):
        if os.path.exists(os.path.join(RUNS, name, 'state.json')):
            out.append(name)
    out.sort(key=lambda n: os.path.getmtime(os.path.join(RUNS, n, 'state.json')))
    return out[::-1]


def _read_json(path, last):
    """Read a JSON file, keeping the last good copy if it is mid-write."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    if last and last[0] == mtime:
        return last[1], mtime
    try:
        with open(path, encoding='utf-8') as fil:
            return json.load(fil), mtime
    except (ValueError, IOError):
        return (last[1] if last else None), (last[0] if last else None)


def pid_alive(pid):
    if not pid:
        return False
    try:
        if os.name == 'nt':
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


class Store(object):
    """Everything the window draws, re-read from disk when it changes."""

    def __init__(self, run):
        self.run = run
        self._state = self._status = None
        self._sm = self._um = None

    @property
    def dir(self):
        return os.path.join(RUNS, self.run)

    def refresh(self):
        self._state, self._sm = _read_json(os.path.join(self.dir, 'state.json'),
                                           (self._sm, self._state))
        self._status, self._um = _read_json(os.path.join(self.dir, 'status.json'),
                                            (self._um, self._status))

    @property
    def state(self):
        return self._state or {}

    @property
    def status(self):
        return self._status or {}

    def liveness(self):
        """(label, colour). Honest about a dead process holding a live file."""
        st, state = self.status, self.state
        if state.get('stopped') or st.get('finished'):
            done = state.get('stopped') or st.get('detail') or 'finished'
            bad = any(w in done.lower() for w in ('crash', 'fail', 'interrupt'))
            return ('FINISHED: ' + done, C['crit'] if bad else C['accent'])
        if st.get('phase') == 'waiting':
            return ('WAITING: ' + (st.get('detail') or 'usage limit'), C['warn'])
        if pid_alive(st.get('pid')):
            return ('RUNNING', C['accent'])
        return ('STOPPED (the loop process is gone; resume with --resume)', C['crit'])

    def series(self):
        """Per-iteration points for the charts.

        best[]  the design the loop kept, iteration 0 being the baseline
        cands[] every candidate measured, with whether it was adopted
        """
        state = self.state
        base = state.get('baseline') or {}
        keys = ('area_um2', 'f_max_mhz', 'throughput_gbps', 'bytes_per_cycle')
        best = [{'i': 0, **{k: base.get(k) for k in keys}}] if base else []
        cands = []
        parent = base
        for it in state.get('iterations', []):
            i = it.get('iteration', len(best))
            for c in it.get('candidates', []):
                m = c.get('measured') or {}
                abs_ = c.get('absolute') or {}
                point = {'i': i, 'label': c.get('label'), 'id': c.get('id'),
                         'adopted': bool(c.get('adopted')),
                         'outcome': c.get('outcome')}
                # Absolutes were recorded from the campaign after this was
                # written; for older runs rebuild them from the percentages
                # against the design this candidate started from.
                for key, pct in (('area_um2', 'area_gain_pct'),
                                 ('f_max_mhz', 'fmax_gain_pct'),
                                 ('throughput_gbps', 'throughput_gain_pct'),
                                 ('bytes_per_cycle', 'bpc_gain_pct')):
                    if abs_.get(key) is not None:
                        point[key] = abs_[key]
                    elif m.get(pct) is not None and parent.get(key) is not None:
                        point[key] = parent[key] * (1.0 + m[pct] / 100.0)
                    else:
                        point[key] = None
                if point['area_um2'] is not None:
                    cands.append(point)
            after = it.get('best_after') or {}
            if any(after.get(k) is not None for k in keys):
                best.append({'i': i, **{k: after.get(k) for k in keys}})
                parent = after
        return best, cands


# ---------------------------------------------------------------------------
# drawing helpers

def rounded(cv, x1, y1, x2, y2, r, **kw):
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def nice_bounds(lo, hi, target=5):
    """A rounded (lo, hi, step) that contains the data with a little air.

    Deliberately NOT anchored at zero. These charts exist to show a design
    changing by tens of percent, and a zeroed axis squashed a real 24% rise
    in area into 11% of the chart height, which read as a flat line.
    """
    if lo is None or hi is None:
        return 0.0, 1.0, 0.5
    if hi == lo:
        pad = abs(hi) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    span = hi - lo
    lo -= span * 0.06
    hi += span * 0.06
    raw = (hi - lo) / float(target)
    mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for mult in (1, 2, 2.5, 5, 10):
        if mult * mag >= raw:
            step = mult * mag
            break
    else:
        step = 10 * mag
    lo = math.floor(lo / step) * step
    hi = math.ceil(hi / step) * step
    return lo, hi, step


# A rejected candidate can sit far outside the range the kept design ever
# occupies (one here was +91% area). Letting it set the scale flattens the
# line the chart is actually about, so it is drawn pinned to the edge with a
# caret instead, and the axis is scaled to the design.
OUTLIER_SPAN_FACTOR = 2.0


def fmt(v):
    if v is None:
        return '-'
    a = abs(v)
    if a >= 100000:
        return '%.0fk' % (v / 1000.0)
    if a >= 1000:
        return '%.1fk' % (v / 1000.0)
    if a >= 100:
        return '%.0f' % v
    if a >= 10:
        return '%.1f' % v
    return '%.3f' % v


def hhmmss(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return '%d:%02d:%02d' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
    return '%d:%02d' % (seconds // 60, seconds % 60)


# ---------------------------------------------------------------------------
# the flow diagram

class FlowDiagram(tk.Canvas):
    """The loop, with the step it is in right now lit up."""

    def __init__(self, master, fonts, **kw):
        tk.Canvas.__init__(self, master, bg=C['panel'], highlightthickness=0,
                           bd=0, **kw)
        self.f = fonts
        self.store = None
        self.bind('<Configure>', lambda e: self.redraw())

    def set_store(self, store):
        self.store = store
        self.redraw()

    def redraw(self):
        self.delete('all')
        w = self.winfo_width()
        if w < 50 or not self.store:
            return
        st = self.store.status
        state = self.store.state
        phase = st.get('phase') or ''
        stopped = bool(state.get('stopped') or st.get('finished'))
        waiting = phase == 'waiting'
        done_setup = bool(state.get('baseline'))

        x1, x2 = 14, w - 14
        y = 10

        y = self._heading(x1, y, 'SETUP, ONCE')
        for key, title, sub in SETUP_STEPS:
            active = (phase == key) and not stopped
            done = done_setup and not active
            y = self._step(x1, x2, y, title, sub, active, done,
                           st.get('detail') if active else '')
            y = self._arrow(x1, x2, y)

        k = st.get('iteration') or len(state.get('iterations', []))
        total = state.get('max_iters') or '?'
        y = self._heading(x1, y, 'EVERY ITERATION' + ('    iteration %s of %s' % (k, total)
                                                      if k else ''))
        # How many candidates this run actually writes each iteration. Taken
        # from the last iteration on record rather than the setting, because
        # --candidates can change when a run is resumed, and it did here.
        n = state.get('candidates') or 3
        iters = state.get('iterations') or []
        if iters and iters[-1].get('candidates'):
            n = len([c for c in iters[-1]['candidates']
                     if (c.get('session') or {}).get('status') != 'retry']) or n
        fill = {'n': n, 's': '' if n == 1 else 's'}

        top = y
        for key, title, sub in LOOP_STEPS:
            active = (phase == key) and not stopped
            y = self._step(x1 + 16, x2, y, title, sub.format(**fill), active, False,
                           st.get('detail') if active else '')
            if key != LOOP_STEPS[-1][0]:
                y = self._arrow(x1 + 16, x2, y)
        bottom = y
        # the loop-back arrow down the left margin
        mid = x1 + 7
        self.create_line(x1 + 16, bottom - 12, mid, bottom - 12, mid, top + 20,
                         x1 + 16, top + 20, fill=C['rule'], width=2,
                         arrow=tk.LAST, arrowshape=(8, 9, 3), smooth=False)
        self.create_text(mid + 4, (top + bottom) / 2, text='repeat', anchor='w',
                         fill=C['faint'], font=self.f['tiny'], angle=90)

        y = bottom + 6
        if waiting:
            y = self._banner(x1, x2, y, 'WAITING', st.get('detail') or
                             'the model provider reports a usage limit', C['warn'], C['warnbg'])
        if stopped:
            done = state.get('stopped') or st.get('detail') or 'finished'
            bad = any(wd in done.lower() for wd in ('crash', 'fail', 'interrupt'))
            y = self._banner(x1, x2, y, 'STOPPED', done,
                             C['crit'] if bad else C['accent'],
                             C['critbg'] if bad else C['accentbg'])
        self.configure(scrollregion=(0, 0, w, y + 10))

    def _heading(self, x, y, text):
        self.create_text(x, y, text=text, anchor='nw', fill=C['faint'],
                         font=self.f['label'])
        return y + 18

    def _step(self, x1, x2, y, title, sub, active, done, detail):
        # Kept tight so all seven steps plus the banner fit the panel without
        # scrolling at the default window size.
        h = 42 if not active else 54
        fill = C['accentbg'] if active else (C['panel'] if not done else C['dim'])
        edge = C['accent'] if active else C['rule']
        rounded(self, x1, y, x2, y + h, 7, fill=fill, outline=edge,
                width=2 if active else 1)
        if active:
            self.create_rectangle(x1, y + 8, x1 + 4, y + h - 8, fill=C['accent'],
                                  outline='')
        tx = x1 + 14
        self.create_text(tx, y + 8, text=title, anchor='nw',
                         fill=C['ink'] if not done else C['muted'],
                         font=self.f['bold'] if active else self.f['body'])
        self.create_text(tx, y + 25, text=sub, anchor='nw', fill=C['muted'],
                         font=self.f['small'], width=x2 - tx - 10)
        if active and detail:
            self.create_text(tx, y + 39, text=detail[:70], anchor='nw',
                             fill=C['accent'], font=self.f['small'],
                             width=x2 - tx - 10)
        if done:
            self.create_text(x2 - 12, y + 8, text='done', anchor='ne',
                             fill=C['faint'], font=self.f['tiny'])
        return y + h

    def _arrow(self, x1, x2, y):
        cx = (x1 + x2) / 2
        self.create_line(cx, y + 1, cx, y + 11, fill=C['rule'], width=2,
                         arrow=tk.LAST, arrowshape=(7, 8, 3))
        return y + 12

    def _banner(self, x1, x2, y, title, text, fg, bg):
        rounded(self, x1, y, x2, y + 44, 7, fill=bg, outline=fg, width=1)
        self.create_text(x1 + 12, y + 9, text=title, anchor='nw', fill=fg,
                         font=self.f['bold'])
        self.create_text(x1 + 12, y + 25, text=text, anchor='nw', fill=C['ink'],
                         font=self.f['small'], width=x2 - x1 - 24)
        return y + 52


# ---------------------------------------------------------------------------
# a chart

class Chart(tk.Canvas):
    """One metric against iteration: a line for the kept design, dots for
    every candidate that was measured."""

    def __init__(self, master, fonts, title, unit, key, colour, **kw):
        tk.Canvas.__init__(self, master, bg=C['panel'], highlightthickness=0,
                           bd=0, **kw)
        self.f = fonts
        self.title, self.unit, self.key, self.colour = title, unit, key, colour
        self.best = self.cands = []
        self.bind('<Configure>', lambda e: self.redraw())
        self.bind('<Motion>', self._hover)
        self._tip = None

    def set_data(self, best, cands):
        self.best, self.cands = best, cands
        self.redraw()

    def redraw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 80 or h < 60:
            return
        pad_l, pad_r, pad_t, pad_b = 58, 16, 27, 18
        vals = [p[self.key] for p in self.best if p.get(self.key) is not None]
        vals += [p[self.key] for p in self.cands if p.get(self.key) is not None]
        self.create_text(12, 9, text=self.title, anchor='nw', fill=C['ink'],
                         font=self.f['bold'])
        self.create_text(w - 12, 11, text=self.unit, anchor='ne', fill=C['faint'],
                         font=self.f['tiny'])
        if not vals:
            self.create_text(w / 2, h / 2, text='no measurements yet',
                             fill=C['faint'], font=self.f['small'])
            return

        # Scale to the design the loop kept; a candidate far outside that
        # range is pinned to the edge rather than allowed to flatten it.
        line = [p[self.key] for p in self.best if p.get(self.key) is not None]
        if line and len(line) > 1:
            lspan = max(line) - min(line)
            room = max(lspan * OUTLIER_SPAN_FACTOR, abs(max(line)) * 0.04)
            keep = [v for v in vals if min(line) - room <= v <= max(line) + room]
            lo, hi, step = nice_bounds(min(keep or line), max(keep or line))
        else:
            lo, hi, step = nice_bounds(min(vals), max(vals))
        imax = max([p['i'] for p in self.best] + [p['i'] for p in self.cands] + [1])
        px = lambda i: pad_l + (w - pad_l - pad_r) * (i / float(max(imax, 1)))
        py = lambda v: h - pad_b - (h - pad_t - pad_b) * ((v - lo) / float(hi - lo))

        # grid and y labels
        t = lo
        while t <= hi + step / 1000.0:
            yy = py(t)
            self.create_line(pad_l, yy, w - pad_r, yy, fill=C['grid'])
            self.create_text(pad_l - 7, yy, text=fmt(t), anchor='e',
                             fill=C['faint'], font=self.f['tiny'])
            t += step
        # x labels
        stepx = max(1, int(imax / 8) + (1 if imax % 8 else 0))
        # No axis title: every chart title already ends in "per iteration",
        # and a centred one collided with the tick numbers.
        for i in range(0, imax + 1, stepx):
            self.create_text(px(i), h - pad_b + 3, text=str(i), anchor='n',
                             fill=C['faint'], font=self.f['tiny'])

        # baseline reference
        base = self.best[0][self.key] if self.best and self.best[0].get(self.key) else None
        if base is not None:
            self.create_line(pad_l, py(base), w - pad_r, py(base), fill=C['faint'],
                             dash=(4, 3))

        # rejected / adopted candidates
        for p in self.cands:
            v = p.get(self.key)
            if v is None or p['adopted']:
                continue
            x = px(p['i'])
            tag = ('pt', 'pt:%s' % json.dumps(
                {'i': p['i'], 'id': p.get('id'), 'v': v, 'o': p.get('outcome')}))
            if v > hi or v < lo:                 # off the scale: pin it
                edge = pad_t + 5 if v > hi else h - pad_b - 5
                up = 1 if v > hi else -1
                self.create_polygon(x, edge - 5 * up, x - 4, edge + 2 * up,
                                    x + 4, edge + 2 * up, fill=C['panel'],
                                    outline=C['faint'], width=1, tags=tag)
                self.create_text(x + 7, edge, text=fmt(v), anchor='w',
                                 fill=C['faint'], font=self.f['tiny'], tags=tag)
                continue
            yv = py(v)
            self.create_oval(x - 3, yv - 3, x + 3, yv + 3, outline=C['faint'],
                             fill=C['panel'], width=1, tags=tag)

        # the design the loop kept
        pts = [(px(p['i']), py(p[self.key])) for p in self.best
               if p.get(self.key) is not None]
        if len(pts) > 1:
            flat = []
            for j, (x, yv) in enumerate(pts):          # step line: a design
                if j:                                   # holds until replaced
                    flat += [x, flat[-1]]
                flat += [x, yv]
            self.create_line(*flat, fill=self.colour, width=2)
        for j, (x, yv) in enumerate(pts):
            adopted = j > 0
            self.create_oval(x - 4, yv - 4, x + 4, yv + 4,
                             fill=self.colour if adopted else C['panel'],
                             outline=self.colour, width=2,
                             tags=('pt', 'pt:%s' % json.dumps(
                                 {'i': self.best[j]['i'], 'id': 'best design',
                                  'v': self.best[j][self.key], 'o': 'kept'})))
        if pts:
            x, yv = pts[-1]
            self.create_text(min(x + 8, w - pad_r), yv - 10,
                             text=fmt(self.best[-1][self.key]), anchor='w',
                             fill=self.colour, font=self.f['bold'])

    def _hover(self, event):
        if self._tip:
            self.delete(self._tip)
            self._tip = None
        for item in self.find_overlapping(event.x - 4, event.y - 4,
                                          event.x + 4, event.y + 4):
            for tag in self.gettags(item):
                if tag.startswith('pt:'):
                    try:
                        d = json.loads(tag[3:])
                    except ValueError:
                        return
                    text = 'i%s  %s  %s  %s' % (d['i'], d.get('id') or '',
                                                fmt(d['v']), d.get('o') or '')
                    self._tip = self.create_text(
                        min(event.x + 10, self.winfo_width() - 8), event.y - 12,
                        text=text, anchor='e' if event.x > self.winfo_width() / 2
                        else 'w', fill=C['ink'], font=self.f['tiny'])
                    return


# ---------------------------------------------------------------------------
# the window

class Dashboard(tk.Tk):
    def __init__(self, run=None, demo=False):
        tk.Tk.__init__(self)
        self.title('agentic RTL loop')
        self.geometry('1200x820')
        self.minsize(940, 620)
        self.configure(bg=C['bg'])
        family = 'Segoe UI' if 'Segoe UI' in tkfont.families() else 'TkDefaultFont'
        self.f = {
            'body':  tkfont.Font(family=family, size=10),
            'bold':  tkfont.Font(family=family, size=10, weight='bold'),
            'small': tkfont.Font(family=family, size=9),
            'tiny':  tkfont.Font(family=family, size=8),
            'label': tkfont.Font(family=family, size=8, weight='bold'),
            'huge':  tkfont.Font(family=family, size=20, weight='bold'),
            'mono':  tkfont.Font(family='Consolas', size=9),
        }
        self.demo = demo
        self.demo_at = 0
        self.store = None
        self._build()
        runs = list_runs()
        if run and run in runs:
            self.run_var.set(run)
        elif runs:
            self.run_var.set(runs[0])
        self._pick()
        self.after(POLL_MS, self._tick)

    # -- layout ------------------------------------------------------------
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TCombobox', fieldbackground=C['panel'])

        head = tk.Frame(self, bg=C['bg'])
        head.pack(fill='x', padx=12, pady=(10, 6))
        tk.Label(head, text='run', bg=C['bg'], fg=C['faint'],
                 font=self.f['label']).pack(side='left', padx=(2, 6))
        self.run_var = tk.StringVar()
        self.picker = ttk.Combobox(head, textvariable=self.run_var, width=26,
                                   state='readonly', values=list_runs(),
                                   font=self.f['body'])
        self.picker.pack(side='left')
        self.picker.bind('<<ComboboxSelected>>', lambda e: self._pick())
        self.goal_lbl = tk.Label(head, text='', bg=C['bg'], fg=C['ink'],
                                 font=self.f['body'])
        self.goal_lbl.pack(side='left', padx=14)
        self.live_lbl = tk.Label(head, text='', bg=C['bg'], fg=C['muted'],
                                 font=self.f['bold'])
        self.live_lbl.pack(side='right')

        cards = tk.Frame(self, bg=C['bg'])
        cards.pack(fill='x', padx=12, pady=(0, 8))
        self.cards = {}
        for key, title, unit in (('throughput_gbps', 'throughput', 'GB/s'),
                                 ('f_max_mhz', 'clock frequency', 'MHz'),
                                 ('area_um2', 'area', 'um2'),
                                 ('bytes_per_cycle', 'bytes / cycle', 'real pages')):
            card = tk.Frame(cards, bg=C['panel'], highlightbackground=C['rule'],
                            highlightthickness=1)
            card.pack(side='left', expand=True, fill='both', padx=(0, 8))
            tk.Label(card, text=title.upper(), bg=C['panel'], fg=C['faint'],
                     font=self.f['label']).pack(anchor='w', padx=12, pady=(8, 0))
            val = tk.Label(card, text='-', bg=C['panel'], fg=C['ink'],
                           font=self.f['huge'])
            val.pack(anchor='w', padx=12)
            sub = tk.Label(card, text=unit, bg=C['panel'], fg=C['muted'],
                           font=self.f['small'], justify='left')
            sub.pack(anchor='w', padx=12, pady=(0, 9))
            self.cards[key] = (val, sub, unit)

        body = tk.Frame(self, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=12, pady=(0, 10))

        left = tk.Frame(body, bg=C['panel'], highlightbackground=C['rule'],
                        highlightthickness=1, width=372)
        left.pack(side='left', fill='y')
        left.pack_propagate(False)
        tk.Label(left, text='THE LOOP', bg=C['panel'], fg=C['faint'],
                 font=self.f['label']).pack(anchor='w', padx=14, pady=(10, 0))
        self.phase_lbl = tk.Label(left, text='', bg=C['panel'], fg=C['ink'],
                                  font=self.f['bold'], justify='left', anchor='w')
        self.phase_lbl.pack(fill='x', padx=14)
        self.flow = FlowDiagram(left, self.f, width=360, height=380)
        self.flow.pack(fill='both', expand=True, padx=2, pady=(4, 6))
        # A short window can cut the diagram off; let the wheel scroll it.
        self.flow.bind('<MouseWheel>',
                       lambda e: self.flow.yview_scroll(-1 * (e.delta // 120), 'units'))
        self.flow.bind('<Button-4>', lambda e: self.flow.yview_scroll(-1, 'units'))
        self.flow.bind('<Button-5>', lambda e: self.flow.yview_scroll(1, 'units'))

        right = tk.Frame(body, bg=C['bg'])
        right.pack(side='left', fill='both', expand=True, padx=(10, 0))
        self.charts = []
        # grid, not pack: the three charts must share the height in fixed
        # proportions. Packed, the last one was squeezed to nothing.
        specs = (('area per iteration', 'square micrometres', 'area_um2', C['area'], 3),
                 ('clock frequency per iteration', 'MHz', 'f_max_mhz', C['fmax'], 3),
                 ('throughput per iteration', 'GB/s = bytes/cycle x f_max',
                  'throughput_gbps', C['thru'], 2))
        for row, (title, unit, key, colour, weight) in enumerate(specs):
            holder = tk.Frame(right, bg=C['panel'], highlightbackground=C['rule'],
                              highlightthickness=1)
            holder.grid(row=row, column=0, sticky='nsew', pady=(0, 7))
            right.rowconfigure(row, weight=weight, minsize=104)
            chart = Chart(holder, self.f, title, unit, key, colour,
                          width=200, height=110)
            chart.pack(fill='both', expand=True)
            self.charts.append(chart)
        right.columnconfigure(0, weight=1)

        legend = tk.Frame(right, bg=C['bg'])
        legend.grid(row=len(specs), column=0, sticky='ew')
        tk.Label(legend, text='line + filled dot: the design the loop kept     '
                              'hollow dot: a candidate it measured and rejected     '
                              'dashed line: the baseline',
                 bg=C['bg'], fg=C['muted'], font=self.f['tiny']).pack(side='left')

        foot = tk.Frame(self, bg=C['panel'], highlightbackground=C['rule'],
                        highlightthickness=1, height=116)
        foot.pack(fill='x', padx=12, pady=(0, 12))
        foot.pack_propagate(False)
        tk.Label(foot, text='ITERATIONS', bg=C['panel'], fg=C['faint'],
                 font=self.f['label']).pack(anchor='w', padx=12, pady=(8, 2))
        cols = ('it', 'adopted', 'throughput', 'f_max', 'area', 'candidates', 'when')
        self.table = ttk.Treeview(foot, columns=cols, show='headings', height=3)
        for col, width, anchor in (('it', 40, 'e'), ('adopted', 300, 'w'),
                                   ('throughput', 100, 'e'), ('f_max', 90, 'e'),
                                   ('area', 90, 'e'), ('candidates', 240, 'w'),
                                   ('when', 150, 'w')):
            self.table.heading(col, text=col)
            self.table.column(col, width=width, anchor=anchor, stretch=(col in ('adopted', 'candidates')))
        bar = ttk.Scrollbar(foot, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y', padx=(0, 10), pady=(0, 10))
        self.table.pack(fill='both', expand=True, padx=12, pady=(0, 10))
        self.table.tag_configure('win', foreground=C['accent'])
        self.table.tag_configure('none', foreground=C['muted'])

    # -- data --------------------------------------------------------------
    def _pick(self):
        name = self.run_var.get()
        self.store = Store(name) if name else None
        if self.store:
            self.store.refresh()
        self.flow.set_store(self.store)
        self._paint()

    def _tick(self):
        try:
            runs = list_runs()
            if list(self.picker['values']) != runs:
                self.picker['values'] = runs
                if self.run_var.get() not in runs and runs:
                    self.run_var.set(runs[0])
                    self._pick()
            if self.store:
                self.store.refresh()
                if self.demo:
                    self._demo_step()
                self._paint()
        except Exception as exc:                 # a window must never die
            self.live_lbl.configure(text='dashboard error: %s' % str(exc)[:60],
                                    fg=C['crit'])
        self.after(POLL_MS, self._tick)

    def _demo_step(self):
        """Walk a finished run's phases so the highlight can be seen."""
        n = len(self.store.state.get('iterations', []))
        seq = ([('corpus', 'building stimulus'), ('baseline', 'measuring the starting design')]
               + [(p, d) for i in range(max(1, n))
                  for p, d in (('plan', 'choosing 2 directions'),
                               ('write', '2 coding sessions in parallel'),
                               ('measure', '2 candidates: simulate + synthesise'),
                               ('adopt', 'best candidate'),
                               ('learn', 'updating the skill library'))])
        phase, detail = seq[self.demo_at % len(seq)]
        it = 1 + max(0, (self.demo_at - 2)) // 5
        self.store._status = dict(self.store.status or {})
        self.store._status.update({'phase': phase, 'detail': detail,
                                   'iteration': min(it, max(1, n)),
                                   'phase_started': time.time(), 'finished': None,
                                   'pid': os.getpid()})
        self.store._state = dict(self.store.state)
        self.store._state['stopped'] = None
        self.demo_at += 1

    def _paint(self):
        if not self.store:
            self.goal_lbl.configure(text='no runs yet under .agentic/runs')
            return
        state, st = self.store.state, self.store.status
        self.goal_lbl.configure(text='goal: %s' % (state.get('goal_text') or '?'))
        label, colour = self.store.liveness()
        if self.demo:
            label, colour = 'DEMO: replaying phases, not a live run', C['warn']
        self.live_lbl.configure(text=label, fg=colour)

        # phase line. The elapsed counter means different things depending on
        # whether the loop is still working: "in this step" while it runs, and
        # "ago" once it has stopped. Showing a climbing "in this step" on a
        # finished run reads as if something were still happening.
        started = st.get('phase_started')
        secs = time.time() - started if started else 0
        k = st.get('iteration') or len(state.get('iterations', []))
        phase = st.get('phase') or '-'
        pretty = {key: title for key, title, _ in SETUP_STEPS + LOOP_STEPS}.get(phase, phase)
        over = bool(state.get('stopped') or st.get('finished'))
        if over:
            when = st.get('finished') or ''
            tail = 'stopped %s ago%s' % (hhmmss(secs),
                                         ', at ' + when[11:16] if len(when) >= 16 else '')
        elif not pid_alive(st.get('pid')) and not self.demo:
            tail = 'no loop process; last wrote %s ago' % hhmmss(secs)
        else:
            tail = '%s in this step' % hhmmss(secs)
        self.phase_lbl.configure(
            text='iteration %s of %s\n%s   ·   %s'
                 % (k or '-', state.get('max_iters', '?'),
                    'finished' if over else pretty, tail))

        base = state.get('baseline') or {}
        best = (state.get('best') or {}).get('metrics') or {}
        for key, (val, sub, unit) in self.cards.items():
            now, was = best.get(key), base.get(key)
            val.configure(text=fmt(now) if now is not None else '-')
            if now is not None and was:
                change = 100.0 * (now - was) / was
                good = change < 0 if key == 'area_um2' else change > 0
                sub.configure(text='%s   %+.2f%% vs baseline' % (unit, change),
                              fg=C['accent'] if good and abs(change) > 0.05
                              else (C['warn'] if abs(change) > 0.05 else C['muted']))
            else:
                sub.configure(text=unit, fg=C['muted'])

        self.flow.redraw()
        best_pts, cands = self.store.series()
        for chart in self.charts:
            chart.set_data(best_pts, cands)

        for row in self.table.get_children():
            self.table.delete(row)
        for it in reversed(state.get('iterations', [])):
            win = it.get('winner')
            after = it.get('best_after') or {}
            cs = []
            for c in it.get('candidates', []):
                mark = '+' if c.get('adopted') else '-'
                cs.append('%s%s' % (mark, (c.get('id') or c.get('label') or '?')[:26]))
            self.table.insert('', 'end', tags=('win' if win else 'none',), values=(
                it.get('iteration'),
                (win.get('id') if win else 'nothing adopted'),
                fmt(after.get('throughput_gbps')),
                fmt(after.get('f_max_mhz')),
                fmt(after.get('area_um2')),
                ' '.join(cs)[:80],
                (it.get('at') or '').replace('T', ' ')))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', default=None, help='which run to show (default: newest)')
    ap.add_argument('--demo', action='store_true',
                    help='replay the phases of a finished run, one a second')
    ap.add_argument('--selftest', action='store_true',
                    help='build the window, render once, exit (no display needed beyond Tk)')
    args = ap.parse_args()

    if not list_runs():
        print('No runs found under %s.' % RUNS)
        print('Start one first:  python agentic/loop.py --goal "increase throughput by 50%"')
        return 1
    app = Dashboard(run=args.run, demo=args.demo)
    if args.selftest:
        app.update()
        app.update_idletasks()
        app._tick()
        app.update()
        print('gui: built and rendered run %r with %d iteration(s), no errors'
              % (app.run_var.get(), len(app.store.state.get('iterations', []))))
        app.destroy()
        return 0
    app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
