"""Finding r0-2/3/4 reproducer: logic depth of the writer loop L1 (writer + ELQ).

    python timing_l1.py [--rtl DIR] [--keep]

Synthesises vhsnunzip_writer + vhsnunzip_elq (wrapper wr_elq.vhd) with
yosys + ghdl-plugin for xcup (as review-r0-2 did) and runs depth.py on the
netlist. Yosys/ABC depth is a crude proxy for Vivado, so the check is
RELATIVE: the deepest path into every L1 next-state register (head hd, the
bundle bnd, the window counts nv/nu, the ELQ fetch pointer fp) must not be
deeper than the deepest decision path into the command register (cmd) plus a
small margin. Before the round-0 fixes: hd 34 LUT + 11 CARRY4 + 12 MUXF
(19.0 ns est.), fp 21 LUT + 20 CARRY4 (12.0 ns), against cmd 9 LUT + 5 CARRY4
(4.9 ns).
After them (yosys 0.66 + ghdl plugin, this script): hd 4.9 / hcand 5.1 ns,
bnd 6.3, nu 3.7, fp 4.6, against cmd 4.4 (wr.e 7.3, info only).
"""
import argparse, os, re, subprocess, sys
sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MARGIN_NS = 2.0          # allowed excess over the decision path (crude model)
# Head FFs show up as wr.hd or wr.hcand (yosys names them after either net).
CHECK = ['wr.hd', 'wr.hcand', 'wr.bnd', 'wr.nv', 'wr.nu', 'elq.fp']
INFO = ['wr.e']   # window refill from the PF LUTRAM (depth.py also counts the
                  # LUTRAM write address as combinational to the read data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rtl', default=r'C:/Users/Vatsal/dsw4/rtl')
    a = ap.parse_args()
    work = os.path.join(HERE, 'work', 'timing_l1')
    os.makedirs(work, exist_ok=True)
    rtl = tools.shell_path(a.rtl)
    files = ' '.join('%s/%s' % (rtl, f) for f in (
        'vhsnunzip_utils_pkg.vhd', 'vhsnunzip_int_pkg.vhd', 'vhsnunzip_dsw4_pkg.vhd',
        'vhsnunzip_elq.vhd', 'vhsnunzip_writer.vhd'))
    ys = os.path.join(work, 'syn.ys')
    with open(ys, 'w', newline='\n') as f:
        f.write('ghdl --std=08 %s %s -e wr_elq\n' % (files, tools.shell_path(os.path.join(HERE, 'wr_elq.vhd'))))
        f.write('synth_xilinx -family xcup -flatten -top wr_elq\nstat\nwrite_json wr_elq.json\n')
    r = tools.eda_shell('cd %s && yosys -m ghdl -q -l syn.log syn.ys > /dev/null 2>&1; echo RC=$?; ls -la wr_elq.json'
                        % tools.shell_path(work), timeout=3600)
    print(r.text.strip())
    out = subprocess.run([sys.executable, os.path.join(HERE, 'depth.py'), os.path.join(work, 'wr_elq.json'), '400'],
                         capture_output=True, text=True).stdout
    rows = {}
    for ln in out.splitlines():
        m = re.match(r'\s*([\d.]+) ns\s+(\S+)\s+\((\S+)\)\s+levels=(.*)', ln)
        if m:
            rows.setdefault(m.group(2), (float(m.group(1)), m.group(4)))
    print('\n'.join(out.splitlines()[:25]))
    dec = max(v[0] for k, v in rows.items() if k.startswith('wr.cmd'))
    bad = []
    for k in CHECK:
        if k not in rows:
            print('%-8s (not found)' % k)
            continue
        d, lv = rows[k]
        ok = d <= dec + MARGIN_NS
        print('%-8s %5.2f ns  %s  %s' % (k, d, lv, 'ok' if ok else 'DEEPER THAN DECISION + %.1f' % MARGIN_NS))
        bad += [] if ok else [k]
    for k in INFO:
        if k in rows:
            print('%-8s %5.2f ns  %s  (info only)' % (k, rows[k][0], rows[k][1]))
    print('decision (wr.cmd*) %.2f ns' % dec)
    print('TIMING_L1_PASS' if not bad else 'TIMING_L1_FAIL ' + ' '.join(bad))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
