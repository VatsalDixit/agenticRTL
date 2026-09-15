#!/usr/bin/env python3
"""
Check the machine and prepare the repo for the loop. Safe to run again.

    python agentic/setup.py             check everything, build what is missing
    python agentic/setup.py --check     only report, change nothing
    python agentic/setup.py --commit    also commit the kit and config files

What it checks and prepares:
  1. the repo: git, a path without spaces, rtl/vhsnunzip_unbuffered.vhd
  2. the EDA shell: ghdl and yosys with the ghdl plugin (WSL on Windows)
  3. the 45 nm liberty file (fetched if missing)
  4. the Claude Agent SDK and the claude login
  5. the stimulus corpus under .agentic/corpus
  6. the frozen hashes of the measuring instrument
  7. a self-test of the starting design with the loop's own testbench
  8. .gitignore / .gitattributes entries the loop relies on
"""

import argparse
import os
import subprocess
import sys
import urllib.request

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

from tools import CONFIG, ROOT, eda_available, eda_shell, git, git_ok   # noqa: E402

LIB_URL = ('https://raw.githubusercontent.com/The-OpenROAD-Project/'
           'OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/'
           'NangateOpenCellLibrary_typical.lib')
LIB_PATH = os.path.join(KIT, 'syn', 'lib', 'NangateOpenCellLibrary_typical.lib')


def ok(msg):
    print('  OK    %s' % msg)


def bad(msg):
    print('  FAIL  %s' % msg)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--commit', action='store_true')
    args = ap.parse_args()
    fix = not args.check
    failures = 0

    print('1. repository')
    if ' ' in ROOT:
        bad('the path %s has a space in it; move the repo to a path without spaces' % ROOT)
        failures += 1
    else:
        ok('path %s' % ROOT)
    if git_ok(['rev-parse', '--git-dir']):
        ok('git repository, HEAD %s' % git(['rev-parse', '--short', 'HEAD']))
    else:
        bad('not a git repository; run: git init && git add -A && git commit -m baseline')
        failures += 1
    top = os.path.join(ROOT, 'rtl', 'vhsnunzip_unbuffered.vhd')
    if os.path.exists(top):
        ok('design found at rtl/vhsnunzip_unbuffered.vhd')
    else:
        bad('no rtl/vhsnunzip_unbuffered.vhd; the kit expects the vhsnunzip repo layout')
        failures += 1

    print('2. EDA shell (%s)' % ('WSL distro %s' % CONFIG['wsl_distro']
                                  if sys.platform == 'win32' else 'local bash'))
    good, why = eda_available()
    if good:
        ok(why)
        res = eda_shell("yosys -m ghdl -p 'help ghdl' >/dev/null 2>&1 && echo PLUGIN_OK", timeout=120)
        if 'PLUGIN_OK' in res.out:
            ok('yosys ghdl plugin loads')
        else:
            bad('yosys cannot load the ghdl plugin (install the OSS CAD Suite, which bundles it)')
            failures += 1
    else:
        bad(why)
        print('        install: on Windows enable WSL (Ubuntu), then inside it download the')
        print('        OSS CAD Suite from https://github.com/YosysHQ/oss-cad-suite-build/releases')
        print('        and unpack it to %s (or set eda_suite in agentic/config.json)' % CONFIG['eda_suite'])
        failures += 1

    print('3. liberty file')
    if os.path.exists(LIB_PATH):
        ok('%s (%.1f MB)' % (LIB_PATH, os.path.getsize(LIB_PATH) / 1e6))
    elif fix:
        try:
            os.makedirs(os.path.dirname(LIB_PATH), exist_ok=True)
            urllib.request.urlretrieve(LIB_URL, LIB_PATH)
            ok('fetched %s' % LIB_PATH)
        except Exception as exc:
            bad('could not fetch the liberty file: %s' % exc)
            failures += 1
    else:
        bad('missing %s' % LIB_PATH)
        failures += 1

    print('4. Claude')
    try:
        import claude_agent_sdk  # noqa: F401
        ok('claude_agent_sdk %s' % getattr(claude_agent_sdk, '__version__', ''))
    except Exception as exc:
        bad('claude_agent_sdk not importable (%s); run: pip install claude-agent-sdk' % exc)
        failures += 1
    try:
        res = subprocess.run(['claude', '--version'], capture_output=True, text=True,
                             timeout=60, shell=(sys.platform == 'win32'))
        if res.returncode == 0:
            ok('claude CLI %s' % res.stdout.strip())
        else:
            bad('claude CLI not working; install with: npm i -g @anthropic-ai/claude-code, then run claude and log in')
            failures += 1
    except Exception as exc:
        bad('claude CLI not found (%s); install with: npm i -g @anthropic-ai/claude-code' % exc)
        failures += 1
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if key and len(key) < 40:
        ok('ignoring a %d-character placeholder ANTHROPIC_API_KEY (the login is used)' % len(key))
    elif key:
        ok('ANTHROPIC_API_KEY is set and will be used for billing')
    else:
        ok('no API key; the claude login on this machine is used')

    print('5. stimulus corpus')
    import stim
    missing = []
    for table in stim.TRAIN_TABLES + stim.HELD_OUT_TABLES:
        try:
            stim.table_path(table)
        except IOError:
            missing.append(table)
    if missing:
        bad('Parquet tables missing: %s. They travel with the agentic/data folder '
            '(not with git); copy the whole agentic/ folder from the source repo, '
            'or generate them with the old flow/make_test_parquet.py --table all '
            'into test_data/.' % ', '.join(missing))
        failures += 1
    else:
        ok('all %d Parquet tables present' % len(stim.TRAIN_TABLES + stim.HELD_OUT_TABLES))
        try:
            import measure
            draws = measure.prepare_corpus()
            ok('%d draws under %s' % (len(draws), measure.corpus_dir()))
        except Exception as exc:
            bad('could not build the corpus: %s' % exc)
            failures += 1

    print('6. frozen instrument')
    import freeze
    if not os.path.exists(freeze.HASHES):
        if fix:
            freeze.record()
            ok('recorded agentic/frozen.json')
        else:
            bad('agentic/frozen.json missing; run without --check to record it')
            failures += 1
    else:
        probs = freeze.check()
        if probs:
            for p in probs:
                bad(p)
            print('        if you changed these on purpose: python agentic/freeze.py --record')
            failures += 1
        else:
            ok('all frozen files intact')

    print('7. self-test of the starting design')
    if failures == 0 or fix:
        res = subprocess.run([sys.executable, os.path.join(KIT, 'check.py'), '--quick'],
                             capture_output=True, text=True, timeout=900, cwd=ROOT)
        tail = [l for l in res.stdout.splitlines() if l.strip()][-3:]
        if res.returncode == 0:
            ok('the design passes the loop testbench on invented data')
        else:
            bad('self-test failed: %s' % ' | '.join(tail)[:300])
            failures += 1
    else:
        print('        skipped')

    print('8. git hygiene')
    gi = os.path.join(ROOT, '.gitignore')
    text = open(gi, encoding='utf-8').read() if os.path.exists(gi) else ''
    if '.agentic/' in text:
        ok('.gitignore ignores .agentic/')
    elif fix:
        with open(gi, 'a', encoding='utf-8', newline='\n') as fil:
            fil.write('\n# agentic loop scratch (worktrees, corpus, run records)\n.agentic/\n')
        ok('added .agentic/ to .gitignore')
    else:
        bad('.gitignore does not ignore .agentic/')
        failures += 1
    ga = os.path.join(ROOT, '.gitattributes')
    text = open(ga, encoding='utf-8').read() if os.path.exists(ga) else ''
    if '*.sh' in text and 'eol=lf' in text:
        ok('.gitattributes pins *.sh to LF')
    elif fix:
        with open(ga, 'a', encoding='utf-8', newline='\n') as fil:
            fil.write('*.sh text eol=lf\n*.tv text eol=lf\n*.lib text eol=lf\n')
        ok('added LF rules to .gitattributes')
    else:
        bad('.gitattributes does not pin *.sh to LF (bash in WSL chokes on CRLF)')
        failures += 1

    if args.commit and failures == 0:
        git(['add', '-A', 'agentic', '.gitignore', '.gitattributes'])
        if not git_ok(['diff', '--cached', '--quiet']):
            git(['commit', '-q', '-m', 'Add the agentic optimisation loop kit'])
            ok('committed the kit')
        else:
            ok('kit already committed')
    elif failures == 0 and not git_ok(['ls-files', '--error-unmatch', 'agentic/loop.py']):
        print()
        print('The kit is not committed yet. The loop needs it in git (candidates start')
        print('from HEAD). Run:  python agentic/setup.py --commit')
        failures += 1

    print()
    if failures:
        print('%d problem(s). Fix them and run this again.' % failures)
        return 1
    print('Everything is ready. Start with:')
    print('  python agentic/loop.py --goal "increase throughput by 50%" --iters 100')
    return 0


if __name__ == '__main__':
    sys.exit(main())
