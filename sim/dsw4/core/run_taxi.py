"""B2c: simulate the integrated design on real-data draws (default
train-taxi) with the plain harness testbench (kit agenticRTL_v2_real),
exactly as B1 was measured (sim/dsw4/b1/run_taxi.py)."""
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
res = measure.simulate(RTL, os.path.join(HERE, 'work', 'build_taxi'), draws, widths, jobs=1)
for r in res:
    print(r['name'], 'pass' if r['oracle_pass'] else r['problem'], r.get('bytes_per_cycle'), r.get('counters'))
print('%.0f s' % (time.time() - t))
