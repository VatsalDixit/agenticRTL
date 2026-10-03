"""Adversarial cs.tv streams for the DSW-4 B2 review (hazards / byte-exactness).

    python gen_adv.py            -> streams/<name>/{cs.tv,out.hex}, streams/manifest.txt

Every chunk is cross-checked against agentic/ref/snappy.py (via gold.ref_decompress).
"""
import os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, r'C:/Users/Vatsal/diag-i35/diag/work-spec')
import fuzz_tv  # noqa: E402
from fuzz_tv import ChunkGen, varint, out_hex, stim, ref_decompress  # noqa: E402

OUT = os.path.join(HERE, 'streams')


def rnd_data(rng, n):
    return bytes(rng.randrange(256) for _ in range(n))


class G(ChunkGen):
    def __init__(self, rng, vw=3, tag11=False):
        ChunkGen.__init__(self, rng, tag11, vw)

    def lit_bytes(self, d, hdr=None):
        n = len(d)
        if hdr is None:
            hdr = 1 if n <= 60 else (2 if n <= 256 else (3 if n <= 65536 else 4))
        nb = hdr - 1
        if nb == 0 and n > 60:
            nb = 1
        while nb and (n - 1) >> (8 * nb):
            nb += 1
        if nb == 0:
            self.body.append((n - 1) << 2)
        else:
            self.body.append((59 + nb) << 2)
            self.body += (n - 1).to_bytes(nb, 'little')
        self.body += d
        self.plain += d

    def cp(self, off, n, form=2):
        assert 1 <= off <= len(self.plain) and off <= 65535 and 1 <= n <= 64
        if form == 1 and not (4 <= n <= 11 and off < 2048):
            form = 2
        if form == 1:
            self.body += bytes((0x01 | ((n - 4) << 2) | ((off >> 8) << 5), off & 0xff))
        elif form == 2:
            self.body += bytes((0x02 | ((n - 1) << 2), off & 0xff, off >> 8))
        else:
            self.body += bytes((0x03 | ((n - 1) << 2), off & 0xff, off >> 8, 0, 0))
        for _ in range(n):
            self.plain.append(self.plain[-off])

    def out(self):
        return varint(len(self.plain), self.vw) + bytes(self.body), bytes(self.plain)


def dense_mix(g, rng, nel):
    """Hazard-dense elements: short offsets, recent sources, ST/LT edges."""
    for _ in range(nel):
        p = len(g.plain)
        r = rng.random()
        if r < 0.12:
            g.lit_bytes(rnd_data(rng, rng.randint(1, 8)), rng.choice((1, 1, 1, 2, 3)))
        elif r < 0.17:
            g.lit_bytes(rnd_data(rng, rng.randint(9, 80)))
        elif r < 0.40:
            g.cp(min(p, rng.randint(1, 31)), rng.randint(1, 64), rng.choice((1, 2)))
        elif r < 0.60:
            g.cp(min(p, rng.randint(32, 800)), rng.choice((32, 64, rng.randint(1, 64))), rng.choice((1, 2)))
        elif r < 0.72:
            g.cp(min(p, rng.randint(1000, 1100)), rng.choice((32, 64, rng.randint(1, 64))))
        elif r < 0.80:
            g.cp(min(p, rng.randint(370, 430)), rng.choice((32, 64, rng.randint(1, 64))))
        elif r < 0.90:
            g.cp(min(p, 65535, rng.choice((65535, 65534, 65504, 65503, rng.randint(2048, 65535)))),
                 rng.randint(1, 64))
        else:
            g.cp(min(p, rng.choice((1, 2, 4, 8, 16, 32))), rng.randint(1, 64), rng.choice((1, 2)))


def fill_to(g, rng, target, near):
    """Cheap filler (long-offset 64-byte copies) up to `target`, with a dense
    hazard mix in +-near around every multiple of 2^18."""
    while len(g.plain) < target:
        p = len(g.plain)
        to_wrap = (-p) % (1 << 18)
        since = p % (1 << 18)
        if to_wrap < near or (since < near and p >= (1 << 18)):
            dense_mix(g, rng, 1)
            continue
        r = rng.random()
        if r < 0.85:
            g.cp(min(p, rng.randint(40000, 65535)), 64)
        elif r < 0.95:
            g.cp(min(p, rng.randint(1, 2000)), rng.randint(1, 64))
        else:
            g.lit_bytes(rnd_data(rng, rng.randint(1, 120)))
    # trim is not possible; accept overshoot


def chunk_wrap(rng, size, near=3000, endkind=None):
    g = G(rng, vw=3)
    g.lit_bytes(rnd_data(rng, 4096))
    fill_to(g, rng, size, near)
    if endkind == 'out32':
        k = (-len(g.plain)) % 32
        if k:
            g.lit_bytes(rnd_data(rng, k))
    return g.out()


def chunk_exact(rng, size):
    """Output length exactly `size` (copies of 64 then a tail)."""
    g = G(rng, vw=3)
    g.lit_bytes(rnd_data(rng, 2048))
    while len(g.plain) < size - 4096:
        p = len(g.plain)
        if (-p) % (1 << 18) < 3000:
            dense_mix(g, rng, 1)
        else:
            g.cp(min(p, rng.randint(30000, 65535)), 64)
    while len(g.plain) < size:
        n = min(64, size - len(g.plain))
        if rng.random() < 0.5:
            g.cp(min(len(g.plain), rng.randint(1, 1100)), n)
        else:
            g.lit_bytes(rnd_data(rng, min(n, 60)))
    assert len(g.plain) == size
    return g.out()


def chunk_stlt(rng, nel, centre):
    """Full-rate commands with offsets around the ST/LT threshold `centre`."""
    g = G(rng, vw=3)
    g.lit_bytes(rnd_data(rng, 2048))
    for _ in range(nel):
        p = len(g.plain)
        r = rng.random()
        if r < 0.75:
            g.cp(min(p, centre + rng.randint(-40, 70)), rng.choice((64, 64, 32, rng.randint(1, 64))))
        elif r < 0.85:
            g.lit_bytes(rnd_data(rng, rng.randint(1, 3)))
        else:
            g.cp(min(p, rng.randint(1, 31)), rng.randint(1, 64))
    return g.out()


def chunk_recent(rng, nel):
    """Every copy reads bytes written 1..30 commands ago at full rate."""
    g = G(rng, vw=3)
    g.lit_bytes(rnd_data(rng, 64))
    for _ in range(nel):
        p = len(g.plain)
        r = rng.random()
        if r < 0.7:
            g.cp(min(p, rng.randint(1, 960)), rng.choice((64, 64, 33, 32, 31, rng.randint(1, 64))), rng.choice((1, 2)))
        elif r < 0.85:
            g.cp(min(p, rng.randint(1, 31)), 64)
        else:
            g.lit_bytes(rnd_data(rng, rng.randint(1, 40)), rng.choice((1, 1, 2, 5)))
    return g.out()


def chunk_rep(rng, nel):
    """Chains of overlapping copies with offsets 1..31, many consecutive."""
    g = G(rng, vw=3)
    g.lit_bytes(rnd_data(rng, rng.randint(1, 40)))
    for _ in range(nel):
        p = len(g.plain)
        off = min(p, rng.randint(1, 31))
        k = rng.randint(1, 6)
        for _ in range(k):
            g.cp(min(len(g.plain), off if rng.random() < 0.6 else rng.randint(1, 31)),
                 rng.choice((64, 63, 33, 32, 31, rng.randint(1, 64))), rng.choice((1, 2, 2)))
        if rng.random() < 0.4:
            g.lit_bytes(rnd_data(rng, rng.randint(1, 5)))
    return g.out()


def chunk_cbuf(rng, nel):
    """Literals around the CBUF window size, then copies into them."""
    g = G(rng, vw=3)
    for _ in range(nel):
        r = rng.random()
        if r < 0.3:
            n = rng.choice((880, 895, 896, 897, 900, 927, 928, 960, 991, 992, 993, 1023, 1024, 1025, 1056, 2048,
                            rng.randint(800, 1200)))
            g.lit_bytes(rnd_data(rng, n), rng.choice((2, 3, 4, 5)))
        elif r < 0.6:
            for _ in range(rng.randint(1, 8)):
                g.lit_bytes(rnd_data(rng, rng.randint(1, 4)), rng.choice((1, 1, 1, 5)))
        elif g.plain:
            for _ in range(rng.randint(1, 4)):
                g.cp(min(len(g.plain), rng.randint(1, 1200)), rng.randint(1, 64))
    if not g.plain:
        g.lit_bytes(b'x')
    return g.out()


def chunk_small(rng, comp_mod, out_len):
    """Small chunk; out_len output bytes; compressed length padded so that
    len(comp) % 32 == comp_mod where possible (by non-minimal headers)."""
    for _ in range(200):
        g = G(rng, vw=rng.choice((1, 1, 2, 5)) if out_len < 128 else rng.choice((2, 5)))
        while len(g.plain) < out_len:
            rem = out_len - len(g.plain)
            if g.plain and rng.random() < 0.5:
                g.cp(min(len(g.plain), rng.randint(1, 40)), min(rem, rng.randint(1, 64)), rng.choice((1, 2, 2)))
            else:
                g.lit_bytes(rnd_data(rng, min(rem, rng.randint(1, 60))), rng.choice((1, 1, 2, 3, 5)))
        c, p = g.out()
        if out_len == 0 or len(c) % 32 == comp_mod:
            return c, p
    return c, p


def check(chunks, plains, name):
    for i, (c, p) in enumerate(zip(chunks, plains)):
        ref, _ = ref_decompress(c)
        assert ref == p, '%s chunk %d: generator and reference disagree' % (name, i)


def write(name, chunks, plains, man):
    check(chunks, plains, name)
    d = os.path.join(OUT, name)
    os.makedirs(d, exist_ok=True)
    stim.write_cs_tv(chunks, os.path.join(d, 'cs.tv'))
    with open(os.path.join(d, 'out.hex'), 'w', encoding='ascii', newline='\n') as f:
        f.write(out_hex(plains))
    man.append('%s %d %d %d' % (name, len(chunks), sum(map(len, chunks)), sum(map(len, plains))))
    print(man[-1], flush=True)


def main():
    man = []
    rng = random.Random(1234)
    # 1. W wrap: one chunk across 2^18, then small, empty, another across 2^18 at a different phase.
    cs, ps = [], []
    for c, p in (chunk_wrap(rng, 300000), chunk_small(rng, 5, 77), chunk_small(rng, 0, 0),
                 chunk_wrap(rng, 270017, endkind='out32'), chunk_recent(rng, 200)):
        cs.append(c); ps.append(p)
    write('wrap1', cs, ps, man)
    # 2. exactly 2^18, 2^18 + 1, 2*2^18 + 17 output bytes.
    cs, ps = [], []
    for sz in ((1 << 18), (1 << 18) + 1, (1 << 19) + 17):
        c, p = chunk_exact(rng, sz); cs.append(c); ps.append(p)
    write('wrap2', cs, ps, man)
    # 3. a ~1.1 MB chunk (four wraps) followed by a tiny chunk.
    c, p = chunk_wrap(rng, 1100000, near=2500)
    c2, p2 = chunk_small(rng, 31, 33)
    write('wrap3', [c, c2], [p, p2], man)
    # 4. ST/LT boundary at the default (1024) and TEST_ST_LINES=12 (384) thresholds.
    cs, ps = [], []
    for cen in (1024, 1056, 992, 384, 416, 352):
        c, p = chunk_stlt(rng, 1500, cen); cs.append(c); ps.append(p)
    write('stlt', cs, ps, man)
    # 5. recent sources (1..30 commands back) at full rate.
    cs, ps = [], []
    for _ in range(4):
        c, p = chunk_recent(rng, 2500); cs.append(c); ps.append(p)
    write('recent', cs, ps, man)
    # 6. overlapping chains.
    cs, ps = [], []
    for _ in range(4):
        c, p = chunk_rep(rng, 800); cs.append(c); ps.append(p)
    write('repchain', cs, ps, man)
    # 7. CBUF window literals.
    cs, ps = [], []
    for _ in range(3):
        c, p = chunk_cbuf(rng, 120); cs.append(c); ps.append(p)
    write('cbuf', cs, ps, man)
    # 8. chunk ends: every compressed-end phase and output length mod 32, empties, back to back.
    for s in range(3):
        cs, ps = [], []
        for k in range(64):
            if rng.random() < 0.15:
                for _ in range(rng.randint(1, 7)):
                    c, p = chunk_small(rng, 0, 0); cs.append(c); ps.append(p)
            out_len = rng.choice((k % 33, 32 * rng.randint(1, 4), 32 * rng.randint(1, 4) + rng.randint(1, 31)))
            c, p = chunk_small(rng, k % 32, out_len); cs.append(c); ps.append(p)
        write('ends%d' % s, cs, ps, man)
    with open(os.path.join(OUT, 'manifest.txt'), 'w', newline='\n') as f:
        f.write('\n'.join(man) + '\n')


if __name__ == '__main__':
    main()
