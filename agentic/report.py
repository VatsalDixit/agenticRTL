#!/usr/bin/env python3
"""
The run's live view: status.json (a small file rewritten at every phase) and
report.html (a page redrawn after every iteration). Both are windows, not
inputs: nothing in the loop reads them, and a failure to write them never
stops a run.

Usage:
    python agentic/report.py --run NAME        print the status and the table
"""

import argparse
import html
import os
import sys
import time

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

from tools import (ROOT, area_of, area_unit, now_iso, read_json,  # noqa: E402
                   write_json)


def runs_dir():
    return os.path.join(ROOT, '.agentic', 'runs')


def run_dir(name):
    return os.path.join(runs_dir(), name)


def latest_run():
    base = runs_dir()
    if not os.path.isdir(base):
        return None
    names = [n for n in os.listdir(base)
             if os.path.exists(os.path.join(base, n, 'state.json'))]
    if not names:
        return None
    names.sort(key=lambda n: os.path.getmtime(os.path.join(base, n, 'state.json')))
    return names[-1]


class Status(object):
    """Rewrite status.json at every phase change. Never raises."""

    def __init__(self, path):
        self.path = path
        self.data = {'started': now_iso(), 'phase': 'starting', 'iteration': 0,
                     'detail': '', 'phase_started': time.time(), 'pid': os.getpid(),
                     'finished': None, 'best': {}, 'baseline': {}}

    def set(self, **kw):
        try:
            if 'phase' in kw and kw['phase'] != self.data.get('phase'):
                self.data['phase_started'] = time.time()
            self.data.update(kw)
            self.data['updated'] = now_iso()
            write_json(self.path, self.data)
        except Exception:
            pass


def _pct(v):
    return 'n/a' if v is None else '%+.2f%%' % v


def _num(v, fmt='%.3f'):
    return 'n/a' if v is None else fmt % v


def _svg_chart(points, base_value, title, unit):
    """A tiny inline SVG line chart: iteration on x, value on y."""
    if not points:
        return ''
    width, height, pad = 640, 220, 40
    xs = [p[0] for p in points]
    ys = [p[1] for p in points] + [base_value]
    lo, hi = min(ys), max(ys)
    if hi == lo:
        hi = lo + 1
    span = hi - lo

    def px(i):
        return pad + (width - 2 * pad) * (i - min(xs)) / max(1, max(xs) - min(xs))

    def py(v):
        return height - pad - (height - 2 * pad) * (v - lo) / span

    path = ' '.join('%s%.1f,%.1f' % ('M' if i == 0 else 'L', px(x), py(y))
                    for i, (x, y) in enumerate(points))
    base_y = py(base_value)
    return ('<svg viewBox="0 0 %d %d" width="100%%" style="max-width:%dpx">'
            '<text x="%d" y="18" font-size="13">%s (%s)</text>'
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#999" stroke-dasharray="4 3"/>'
            '<text x="%d" y="%.1f" font-size="11" fill="#999">baseline %.3f</text>'
            '<path d="%s" fill="none" stroke="#2a7" stroke-width="2"/>'
            '%s'
            '<text x="%d" y="%d" font-size="11">iteration</text></svg>'
            % (width, height, width, pad, html.escape(title), unit,
               pad, base_y, width - pad, base_y, pad + 4, base_y - 4, base_value,
               path,
               ''.join('<circle cx="%.1f" cy="%.1f" r="3" fill="#2a7"/>'
                       % (px(x), py(y)) for x, y in points),
               width // 2, height - 8))


def render_html(state):
    base = state.get('baseline') or {}
    best = (state.get('best') or {}).get('metrics') or {}
    rows = []
    points_tp, points_bpc = [], []
    for it in state.get('iterations', []):
        win = it.get('winner') or {}
        wm = (win.get('metrics') or {}) if win else {}
        cur = it.get('best_after') or {}
        if cur.get('throughput_gbps') is not None:
            points_tp.append((it['iteration'], cur['throughput_gbps']))
        if cur.get('bytes_per_cycle') is not None:
            points_bpc.append((it['iteration'], cur['bytes_per_cycle']))
        cands = []
        for c in it.get('candidates', []):
            m = c.get('measured') or {}
            cands.append('<div class="cand %s"><b>%s</b> %s &mdash; %s%s</div>' % (
                'win' if c.get('adopted') else ('bad' if c.get('outcome') not in ('candidate', 'adopted') else ''),
                html.escape(c.get('label', '')), html.escape(c.get('id') or 'no proposal'),
                html.escape(c.get('outcome', '')),
                (' (throughput %s, area %s, predicted %s)' % (
                    _pct(m.get('throughput_gain_pct')), _pct(m.get('area_gain_pct')),
                    _pct(c.get('expected_gain_pct')))) if m else
                (': ' + html.escape((c.get('reason') or c.get('problem') or '')[:160])
                 if (c.get('reason') or c.get('problem')) else '')))
        rows.append('<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            it['iteration'], html.escape(it.get('at', '')),
            html.escape((win.get('id') if win else '-') or '-'),
            _pct(win.get('measured', {}).get('throughput_gain_pct')) if win else '-',
            _num(cur.get('throughput_gbps')) + ' GB/s' if cur else '-',
            ''.join(cands)))

    prog = state.get('progress') or {}
    head = ('<h1>Agentic RTL loop: %s</h1><p>%s</p>' % (
        html.escape(state.get('run', '')), html.escape(state.get('goal_text', ''))))
    summary = ('<table class="kv"><tr><th></th><th>baseline</th><th>best so far</th><th>change</th></tr>'
               '<tr><td>throughput</td><td>%s GB/s</td><td>%s GB/s</td><td>%s</td></tr>'
               '<tr><td>bytes/cycle (real data)</td><td>%s</td><td>%s</td><td>%s</td></tr>'
               '<tr><td>f_max</td><td>%s MHz</td><td>%s MHz</td><td>%s</td></tr>'
               '<tr><td>area</td><td>%s %s</td><td>%s %s</td><td>%s</td></tr></table>'
               % (_num(base.get('throughput_gbps')), _num(best.get('throughput_gbps')), _pct(prog.get('throughput_gain_pct')),
                  _num(base.get('bytes_per_cycle')), _num(best.get('bytes_per_cycle')), _pct(prog.get('bpc_gain_pct')),
                  _num(base.get('f_max_mhz'), '%.1f'), _num(best.get('f_max_mhz'), '%.1f'), _pct(prog.get('fmax_gain_pct')),
                  _num(area_of(base), '%.0f'), html.escape(area_unit(base)),
                  _num(area_of(best), '%.0f'), html.escape(area_unit(best)),
                  _pct(prog.get('area_gain_pct'))))
    stopped = state.get('stopped')
    status_line = ('<p><b>Status:</b> %s. Best design: branch <code>%s</code> at commit <code>%s</code>.</p>' % (
        html.escape(stopped or 'running'), html.escape(state.get('branch', '')),
        html.escape(((state.get('best') or {}).get('commit') or '')[:12])))
    charts = _svg_chart(points_tp, base.get('throughput_gbps') or 0, 'throughput of the best design', 'GB/s') + \
        _svg_chart(points_bpc, base.get('bytes_per_cycle') or 0, 'bytes per cycle on real data', 'B/cycle')
    table = ('<table class="it"><tr><th>#</th><th>when</th><th>winner</th><th>gain</th><th>best after</th><th>candidates</th></tr>%s</table>'
             % ''.join(rows))
    css = ('<style>body{font-family:system-ui,sans-serif;margin:24px;max-width:1100px}'
           'table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #ccc;padding:4px 8px;font-size:13px;vertical-align:top}'
           '.cand{margin:2px 0;padding:2px 4px;border-radius:3px;background:#f3f3f3}.cand.win{background:#d8f5dc}.cand.bad{background:#f8e0e0}'
           'code{background:#eee;padding:1px 4px}</style>')
    return ('<!doctype html><html><head><meta charset="utf-8"><title>agentic run %s</title>%s</head><body>%s%s%s%s%s'
            '<p style="color:#777;font-size:12px">rendered %s</p></body></html>'
            % (html.escape(state.get('run', '')), css, head, status_line, summary, charts, table, now_iso()))


def write_report(state, path):
    try:
        with open(path, 'w', encoding='utf-8') as fil:
            fil.write(render_html(state))
    except Exception:
        pass


def print_status(name):
    rdir = run_dir(name)
    status = read_json(os.path.join(rdir, 'status.json'), {})
    state = read_json(os.path.join(rdir, 'state.json'), {})
    print('run:        %s' % name)
    print('goal:       %s' % state.get('goal_text'))
    print('phase:      %s (%s) since %s' % (status.get('phase'), status.get('detail', ''),
                                            status.get('updated')))
    print('iteration:  %s of %s' % (status.get('iteration'), state.get('max_iters')))
    prog = state.get('progress') or {}
    print('best:       throughput %s, bytes/cycle %s, f_max %s, area %s'
          % (_pct(prog.get('throughput_gain_pct')), _pct(prog.get('bpc_gain_pct')),
             _pct(prog.get('fmax_gain_pct')), _pct(prog.get('area_gain_pct'))))
    print('stopped:    %s' % state.get('stopped'))
    for it in state.get('iterations', []):
        win = it.get('winner')
        print('  i%-3d %s  winner: %s' % (it['iteration'], it.get('at', ''),
                                         (win.get('id') if win else '-')))
        for c in it.get('candidates', []):
            m = c.get('measured') or {}
            print('        %-4s %-40s %-14s %s' % (
                c.get('label'), (c.get('id') or 'no proposal')[:40], c.get('outcome', ''),
                ('throughput %s area %s' % (_pct(m.get('throughput_gain_pct')),
                                             _pct(m.get('area_gain_pct')))) if m
                else (c.get('reason') or c.get('problem') or '')[:80]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--run', default=None)
    args = ap.parse_args()
    name = args.run or latest_run()
    if not name:
        print('no runs under %s' % runs_dir())
        return 1
    print_status(name)
    return 0


if __name__ == '__main__':
    sys.exit(main())
