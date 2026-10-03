"""prep.py -- CBUF image for tb_dpath, from a gold draw's cs.tv.

For every draw directory under GOLD (or the names given), writes
work/<draw>/mem.hex: the global compressed address (GA) memory exactly as
gold.layout() builds it (every chunk starts on a 32-byte beat; 128 zero bytes
of tail), one 32-byte beat per line as 64 upper-case hex digits, byte 0 first.
The first line is the number of beats in decimal.

    python prep.py [draw ...]          default: the 8 self-test shapes
"""
import os
import sys

SPEC = r'C:/Users/Vatsal/diag-i35/diag/work-spec'
GOLD = r'C:/Users/Vatsal/dsw4/sim/dsw4/gold'
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SPEC)
import gold  # noqa: E402

SHAPES = ['tiny-chunks', 'line-straddle-17', 'line-straddle-33', 'line-straddle-65',
          'sub-line', 'medium', 'single-chunk', 'long-chunk']


def gold_dir(draw):
    """A draw under GOLD (shapes, fuzz/fNNN) or under work/goldx (extra traces)."""
    d = os.path.join(GOLD, draw)
    return d if os.path.isdir(d) else os.path.join(HERE, 'work', 'goldx', draw)


def prep(draw):
    src = os.path.join(gold_dir(draw), 'cs.tv')
    chunks = gold.read_cs_tv(src)
    mem, _ = gold.layout(chunks)
    mem += b'\x00' * ((-len(mem)) % 32)
    out = os.path.join(HERE, 'work', draw)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'mem.hex'), 'w', encoding='ascii', newline='\n') as fil:
        fil.write('%d\n' % (len(mem) // 32))
        for i in range(0, len(mem), 32):
            fil.write(mem[i:i + 32].hex().upper())
            fil.write('\n')
    return len(mem) // 32


if __name__ == '__main__':
    for d in (sys.argv[1:] or SHAPES):
        print(d, prep(d))
