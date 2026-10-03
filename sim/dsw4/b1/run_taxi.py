"""B1: simulate the gearbox design on the train-taxi draw with the plain
harness testbench (run_probe.py style, kit agenticRTL_v2_real)."""
import os, sys, time
KIT = r'C:\Users\Vatsal\agenticRTL_v2_real\agentic'
sys.path.insert(0, KIT)
os.chdir(os.path.dirname(KIT))
import measure, analyse
HERE = os.path.dirname(os.path.abspath(__file__))
RTL = r'C:\Users\Vatsal\dsw4\rtl'
want = sys.argv[1:] or ['train-taxi']
draws = [d for d in measure.prepare_corpus() if d[0].name in want]
print('tb:', measure.TB_FILE)
print('draws:', [(d[0].name, d[2]) for d in draws], flush=True)
widths = analyse.rtl_widths(RTL); print(widths)
t = time.time()
res = measure.simulate(RTL, os.path.join(HERE, 'build_taxi'), draws, widths, jobs=1)
for r in res:
    print(r['name'], 'pass' if r['oracle_pass'] else r['problem'], r.get('bytes_per_cycle'), r.get('counters'))
print('%.0f s' % (time.time() - t))
