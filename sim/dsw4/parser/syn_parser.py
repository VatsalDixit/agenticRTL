"""Yosys (ghdl plugin) synth_xilinx of vhsnunzip_parser alone + depth.py report.
    python syn_parser.py [top]   (default vhsnunzip_parser)"""
import os, subprocess, sys
sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools  # noqa: E402
HERE = os.path.dirname(os.path.abspath(__file__))
top = sys.argv[1] if len(sys.argv) > 1 else 'vhsnunzip_parser'
work = os.path.join(HERE, 'syn')
rtl = tools.shell_path(r'C:/Users/Vatsal/dsw4/rtl')
files = ' '.join('%s/%s' % (rtl, f) for f in (
    'vhsnunzip_utils_pkg.vhd', 'vhsnunzip_int_pkg.vhd', 'vhsnunzip_dsw4_pkg.vhd',
    'vhsnunzip_blkrd.vhd', 'vhsnunzip_pt.vhd', 'vhsnunzip_walker.vhd', 'vhsnunzip_parser.vhd'))
with open(os.path.join(work, 'syn.ys'), 'w', newline='\n') as f:
    f.write('ghdl --std=08 %s -e %s\n' % (files, top))
    f.write('synth_xilinx -family xcup -flatten -top %s\nstat\nwrite_json %s.json\n' % (top, top))
r = tools.eda_shell('cd %s && /usr/bin/time -v yosys -m ghdl -q -l syn.log syn.ys > yosys.out 2>&1; echo RC=$?; '
                    'grep -i "error" syn.log | head; grep -i "Maximum resident" yosys.out'
                    % tools.shell_path(work), timeout=7200)
print(r.text.strip())
out = subprocess.run([sys.executable, os.path.join(HERE, '..', 'regress', 'depth.py'),
                      os.path.join(work, top + '.json'), '40'], capture_output=True, text=True)
print(out.stdout[:6000], out.stderr[-2000:])
