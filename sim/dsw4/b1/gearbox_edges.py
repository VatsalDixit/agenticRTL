"""B1 gearbox edge cases: empty chunks, every input-beat residue and every
output-line residue, back-to-back 1-beat chunks. Runs through the harness
testbench (measure.simulate) and the oracle."""
import os, random, sys
sys.path.insert(0, r'C:/Users/Vatsal/diag-i35/agentic')
import analyse, measure, stim
from ref import snappy
HERE = os.path.dirname(os.path.abspath(__file__))
rtl = r'C:/Users/Vatsal/dsw4/rtl'
random.seed(1)
chunks = []
ins, outs = set(), set()
def add(data):
    c = snappy.compress_raw(data)
    assert snappy.decompress_raw(c) == data
    chunks.append(c); ins.add(len(c) % 32); outs.add(len(data) % 32)
add(b'')
for L in range(0, 140):
    add(bytes(random.randrange(256) for _ in range(L)))       # literal-heavy
    add(b'')                                                  # empty between
    add((b'abcdefg' * 40)[:L])                                 # copy-heavy
for L in (255, 256, 257, 1000, 1023, 1024, 1025, 4096):
    add(bytes(random.randrange(256) for _ in range(L)))
    add((b'xy' * 3000)[:L])
add(b'')
print('chunks', len(chunks), 'in residues', len(ins), 'out residues', len(outs))
d = os.path.join(HERE, 'edges'); os.makedirs(d, exist_ok=True)
stim.write_cs_tv([c if c else b'\x00' for c in chunks], os.path.join(d, 'cs.tv'))
w = analyse.rtl_widths(rtl); print(w)
res = measure.simulate(rtl, os.path.join(HERE, 'build'), [(stim.Draw('synthetic', 'edges', chunk=0), d, len(chunks))], w, timeout=1800)
for r in res:
    print(r['name'], 'ok' if r['oracle_pass'] else 'FAIL', r['bytes_per_cycle'], r['problem'])
sys.exit(0 if all(r['oracle_pass'] for r in res) else 1)
