"""Mutation check for tb_front: each mutant must FAIL on at least one run.

    python mutate.py
Builds each mutant in mut/<name>/ (copies of the rtl files, one edit) and runs
it on a few draws until a run fails.
"""
import os
import shutil
import sys

import run_front as rf

MUTANTS = {
    # name: (file, old, new)
    'no_cbuf_credit': ('vhsnunzip_cbuf.vhd', "ready_s  <= credit_ok_r and", "ready_s  <= '1' and"),
    'no_csf_full': ('vhsnunzip_cbuf.vhd', "ready_s  <= credit_ok_r and not csf_full_r and not cef_full_r;",
                    "ready_s  <= credit_ok_r;"),
    'cef_minus1': ('vhsnunzip_cbuf.vhd', "gb_r + resize(co.endi, 32) + 1", "gb_r + resize(co.endi, 32)"),
    'credit_late': ('vhsnunzip_cbuf.vhd', "to_unsigned(CBUF_WIN - 64, 32)", "to_unsigned(CBUF_WIN + 160, 32)"),
    'far3_len': ('vhsnunzip_parse_serial.vhd',
                 'when "011"  => flen(15 downto 0) := unsigned(b(2)) & unsigned(b(1));',
                 'when "011"  => flen(7 downto 0) := unsigned(b(1));'),
    'far5_len': ('vhsnunzip_parse_serial.vhd',
                 "when others => flen := unsigned(b(4)) & unsigned(b(3)) & unsigned(b(2)) & unsigned(b(1));",
                 "when others => flen := unsigned(b(1)) & unsigned(b(2)) & unsigned(b(3)) & unsigned(b(4));"),
    'varint4': ('vhsnunzip_parse_serial.vhd', 'elsif b(3)(7) = \'0\' then vlen := "100";',
                'elsif true then vlen := "100";'),
    'arrive_1': ('vhsnunzip_parse_serial.vhd', "ga_sdiff(gb, p_r + 5) >= 0", "ga_sdiff(gb, p_r + 1) >= 0"),
    'no_cred': ('vhsnunzip_parse_serial.vhd', "if cred_r /= 0 then", "if true then"),
    'row_wrap': ('vhsnunzip_parse_serial.vhd', "r(j) := q(9 downto 5) + 1;", "r(j) := q(9 downto 5);"),
}
DRAWS = ['tiny-chunks', 'line-straddle-33', 'long-chunk', 'f000', 'f001', 'f011', 'f037', 'f095']
CFGS = ['fast', 'tight']


def main():
    names = sys.argv[1:] or list(MUTANTS)
    survivors = []
    for name in names:
        fil, old, new = MUTANTS[name]
        mdir = os.path.join(rf.HERE, 'mut', name).replace('\\', '/')
        os.makedirs(mdir, exist_ok=True)
        for f in rf.FILES:
            shutil.copy(rf.RTL + '/' + f, mdir + '/' + f)
        src = open(mdir + '/' + fil).read()
        assert old in src, (name, old)
        open(mdir + '/' + fil, 'w', newline='\n').write(src.replace(old, new, 1))
        shutil.copy(rf.HERE + '/tb_front.vhd', mdir + '/tb_front.vhd')
        srcs = ' '.join(rf.sp(mdir + '/' + f) for f in rf.FILES + ['tb_front.vhd'])
        r = rf.tools.eda_shell('cd %s && mkdir -p work && ghdl -i --std=08 --workdir=work %s && '
                               'ghdl -m --std=08 --workdir=work -o tb_front tb_front'
                               % (rf.sp(mdir), srcs), timeout=1800)
        if r.rc != 0:
            print('%-16s BUILD FAILED\n%s' % (name, r.text[-1500:]))
            survivors.append(name)
            continue
        killed = None
        for d in DRAWS:
            dd = rf.GOLD + '/' + d if d in rf.SHAPES else rf.GOLD + '/fuzz/' + d
            for c in CFGS:
                gen = ' '.join('-g%s=%s' % kv for kv in rf.CFGS[c].items())
                r = rf.tools.eda_shell('cd %s && ./tb_front -gDIR=%s %s --ieee-asserts=disable'
                                       % (rf.sp(mdir), rf.sp(dd), gen), timeout=1800)
                ok = 'FRONT_PASS' in r.text and 'FRONT_FAIL' not in r.text and r.rc == 0
                if not ok:
                    why = [l for l in r.text.splitlines() if 'FAIL' in l or 'assertion' in l]
                    killed = '%s/%s: %s' % (d, c, (why[0] if why else r.text[-200:])[:220])
                    break
            if killed:
                break
        print('%-16s %s' % (name, ('KILLED by ' + killed) if killed else 'SURVIVED'), flush=True)
        if not killed:
            survivors.append(name)
    print('MUTANTS %d, survived %d %s' % (len(names), len(survivors), survivors))


if __name__ == '__main__':
    main()
