"""Streams of the round-0 review regression set -> regress/streams/<name>/
{cs.tv, out.hex} and streams/manifest.txt (cross-checked against
agentic/ref/snappy.py by gen_adv.write).

  ltmargin  full-rate 32-byte copy pieces with q = 32L+1 .. 32L+33, L = 9..12,
            W phases 0 and 17: the newest LT source line was completed as
            few commands back as the ST/LT threshold allows (review-r0-0
            gen_margin.py).
  ltmph2    the same at W phase 2 (a hazard-stopped 2-byte first command),
            L = 8, 9, 12 (review-r0-0 gen_margin2.py; at TEST_ST_LINES=8 it
            reads URAM lines before their write is visible).
  ltm678    ltmargin with L = 6, 7, 8 (fails at TEST_ST_LINES=7).
  ovl       copy source < W (finding r0-0, second assert): short literals
            followed by overlapping copies (off 1..12 < len) in every slot
            position, so slots k >= 1 are cut at their hazard limit
            n = off - d. A writer that over-sizes n there still has D >= 0;
            only the S + n <= q assert in vhsnunzip_agen catches it.
  ltstale   finding r0-0: a 48 KiB chunk first, so every history row the
            next chunk reads too early holds DEFINED data of the previous
            chunk; then the ltmph2 probe at L = 8. With TEST_ST_LINES=8 the
            pre-fix RTL emits wrong bytes with sim.log empty (no assert);
            the fixed RTL stops on the LT write-visibility assert.
"""
import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_adv import G, rnd_data, write, OUT  # noqa: E402


def margin(LS, seed):
    rng = random.Random(seed)
    cs, ps = [], []
    for L in LS:
        for phase in (0, 17):
            g = G(rng, vw=3)
            g.lit_bytes(rnd_data(rng, 64 * 32 + phase))
            for i in range(300):
                g.cp(32 * L + 1 + (i % 33), 64)
            c, p = g.out(); cs.append(c); ps.append(p)
    return cs, ps


def phase2(g, rng, L):
    g.lit_bytes(rnd_data(rng, 2))
    g.cp(2, 4)                       # hazard stop at slot 1: command 1 = 2 bytes
    g.lit_bytes(rnd_data(rng, 4096))
    for i in range(300):
        g.cp(32 * L + 1 + (i % 33), 64)


def mph2(LS, seed):
    rng = random.Random(seed)
    cs, ps = [], []
    for L in LS:
        for _ in range(2):
            g = G(rng, vw=3)
            phase2(g, rng, L)
            c, p = g.out(); cs.append(c); ps.append(p)
    return cs, ps


def stale(seed):
    rng = random.Random(seed)
    cs, ps = [], []
    g = G(rng, vw=3)
    for _ in range(12):
        g.lit_bytes(rnd_data(rng, 4096))
    c, p = g.out(); cs.append(c); ps.append(p)
    for _ in range(2):
        g = G(rng, vw=3)
        phase2(g, rng, 8)
        c, p = g.out(); cs.append(c); ps.append(p)
    return cs, ps


def ovl(seed):
    rng = random.Random(seed)
    cs, ps = [], []
    for _ in range(4):
        g = G(rng, vw=3)
        g.lit_bytes(rnd_data(rng, 16))
        for _ in range(1500):
            g.lit_bytes(rnd_data(rng, rng.randint(1, 4)))
            off = rng.randint(1, 12)
            g.cp(off, rng.randint(off + 1, min(64, off + 24)), rng.choice((1, 2)))
        c, p = g.out(); cs.append(c); ps.append(p)
    return cs, ps


def main():
    os.makedirs(OUT, exist_ok=True)
    man = []
    write('ltmargin', *margin((9, 10, 11, 12), 99), man)
    write('ltmph2', *mph2((8, 9, 12), 5), man)
    write('ltm678', *margin((6, 7, 8), 99), man)
    write('ltstale', *stale(7), man)
    write('ovl', *ovl(11), man)
    with open(os.path.join(OUT, 'manifest.txt'), 'w', newline='\n') as f:
        f.write('\n'.join(man) + '\n')


if __name__ == '__main__':
    main()
