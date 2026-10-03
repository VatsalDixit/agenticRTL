"""Logic-level analysis of a yosys synth_xilinx JSON netlist.

For every FF/SRL/LUTRAM data endpoint, find the deepest combinational path
from any sequential startpoint (FF Q, SRL Q, LUTRAM O via its address) and
report it with a crude UltraScale+ delay estimate:
  LUT 0.12 ns + net 0.35 ns, MUXF7/F8 0.15 ns each (no net), CARRY8 0.10 ns
  per chained stage (+ 0.35 ns net when entered from a LUT), SRL/LUTRAM A->O
  0.5 ns + net, clk->Q 0.1 ns, setup 0.05 ns.
Groups endpoints by register base name (bit index stripped).
"""
import json, re, sys, collections

nl = json.load(open(sys.argv[1]))
top = [m for m in nl['modules'].values() if m.get('attributes', {}).get('top')]
mod = top[0] if top else list(nl['modules'].values())[0]
cells = mod['cells']
netname = {}
for n, v in mod['netnames'].items():
    for b in v['bits']:
        if isinstance(b, int):
            if b not in netname or (n.startswith('$') is False and netname[b].startswith('$')):
                netname[b] = n

SEQ = re.compile(r'^(FD[A-Z]*|SRL.*|RAM.*)$')
OUT_PORTS = {'O', 'O5', 'O6', 'Q', 'Q31', 'CO', 'O_', 'DOA', 'DOB', 'DOC', 'DOD', 'SPO', 'DPO'}
driver = {}   # bit -> (cell, type, port)
for cn, c in cells.items():
    for p, bits in c['connections'].items():
        if c['port_directions'].get(p) == 'output':
            for b in bits:
                if isinstance(b, int):
                    driver[b] = (cn, c['type'], p)

def cdelay(t):
    if t.startswith('LUT'): return 0.12 + 0.35, 'LUT'
    if t.startswith('MUXF'): return 0.15, 'MUXF'
    if t.startswith('CARRY'): return 0.10, 'CARRY'
    if t in ('INV', 'BUF'): return 0.0, 'BUF'
    return 0.12 + 0.35, t

memo = {}
sys.setrecursionlimit(100000)
def arr(b):
    """(delay, levels counter, path) of the latest arrival at net bit b."""
    if not isinstance(b, int):
        return (0.0, collections.Counter(), [])
    if b in memo:
        return memo[b]
    memo[b] = (0.0, collections.Counter(), ['<loop>'])
    d = driver.get(b)
    if d is None:
        res = (0.0, collections.Counter(), ['in:' + netname.get(b, str(b))])
    else:
        cn, t, p = d
        c = cells[cn]
        if t.startswith('FD'):
            res = (0.1, collections.Counter(), ['ff:' + netname.get(b, cn)])
        elif t.startswith('SRL') or t.startswith('RAM'):
            # address -> output is combinational
            best = (0.0, collections.Counter(), ['seq:' + netname.get(b, cn)])
            for ip, bits in c['connections'].items():
                if c['port_directions'].get(ip) == 'input' and (ip.startswith('A') and ip not in ('A',) or ip in ('A',) ):
                    for ib in bits:
                        a = arr(ib)
                        if a[0] > best[0]:
                            best = a
            cnt = best[1].copy(); cnt['SRL/RAM'] += 1
            res = (best[0] + 0.85, cnt, best[2] + [t + ':' + netname.get(b, cn)])
        else:
            dl, kind = cdelay(t)
            best = (0.0, collections.Counter(), [])
            for ip, bits in c['connections'].items():
                if c['port_directions'].get(ip) == 'input':
                    for ib in bits:
                        a = arr(ib)
                        if a[0] > best[0]:
                            best = a
            add = dl
            if kind == 'CARRY':
                pd = driver.get(best[2] and None)
            cnt = best[1].copy(); cnt[kind] += 1
            res = (best[0] + add, cnt, best[2] + [t + ':' + netname.get(b, cn)])
    memo[b] = res
    return res

groups = {}
for cn, c in cells.items():
    t = c['type']
    if not SEQ.match(t):
        continue
    for p, bits in c['connections'].items():
        if c['port_directions'].get(p) != 'input' or p in ('C', 'CLK', 'WCLK'):
            continue
        if t.startswith('RAM') and p.startswith('ADDR') or (t.startswith('SRL') and p.startswith('A')):
            continue
        for b in bits:
            a = arr(b)
            qn = None
            for op, ob in c['connections'].items():
                if c['port_directions'].get(op) == 'output':
                    qn = netname.get(ob[0], cn); break
            g = re.sub(r'\[\d+\]', '', qn or cn)
            g = re.sub(r'_reg$', '', g)
            if g not in groups or a[0] > groups[g][0]:
                groups[g] = (a[0] + 0.05, a[1], a[2], p, t)

top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
filt = sys.argv[3] if len(sys.argv) > 3 else None
rows = sorted(groups.items(), key=lambda kv: -kv[1][0])
for g, (d, cnt, path, p, t) in rows[:top_n]:
    if filt and not re.search(filt, g):
        continue
    print(f'{d:5.2f} ns  {g}  ({t}.{p})  levels={dict(cnt)}')
if filt:
    for g, (d, cnt, path, p, t) in rows:
        if re.search(filt, g):
            print(f'--- {g}: {d:.2f} ns levels={dict(cnt)}')
            for s in path:
                print('     ', s)
