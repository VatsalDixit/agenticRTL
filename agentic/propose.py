#!/usr/bin/env python3
"""
Ask Claude for RTL candidates.

Two kinds of call:

  plan_directions()   one cheap call, no tools. Reads the profile, the skill
                      library and the history, and returns N different
                      directions to try this iteration (so the N candidates
                      do not all attempt the same thing).

  write_candidates()  N coding sessions in parallel, one per direction, each
                      inside its own git worktree. The session may read and
                      edit files there, and may run exactly one command:
                      `python agentic/check.py`. It cannot read outside the
                      worktree and it never sees the scoring stimulus. It
                      ends by writing PROPOSAL.json.

Both go through the Claude Agent SDK, which drives the local `claude`
executable and uses the Claude Code login on the machine (or an API key if
a real one is set). Nothing else in the loop talks to a model.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time

KIT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT)

from tools import CONFIG   # noqa: E402

try:
    import warnings
    from claude_agent_sdk import (            # noqa: E402
        AssistantMessage, ClaudeAgentOptions, PermissionResultAllow,
        PermissionResultDeny, ResultMessage, TextBlock, ToolUseBlock, query)
    from claude_agent_sdk import ClaudeSDKError   # noqa: E402
    try:
        # The CLI emits this whenever the usage window's state changes. It
        # carries how much of the window is gone and when it resets, which is
        # better than parsing "resets 5am" out of an error message.
        from claude_agent_sdk import HookMatcher         # noqa: E402
        from claude_agent_sdk import RateLimitEvent      # noqa: E402
    except ImportError:                       # older SDK: no window signal
        RateLimitEvent = ()                   # isinstance(x, ()) is always False
        HookMatcher = None
    try:
        # The warning is about Read/Edit/etc. being auto-approved, which is
        # exactly what we want; Bash is deliberately left out so it is gated.
        from claude_agent_sdk.types import CanUseToolShadowedWarning
        warnings.simplefilter('ignore', CanUseToolShadowedWarning)
    except Exception:
        pass
    SDK_OK = True
    SDK_ERROR = ''
except Exception as exc:                       # pragma: no cover
    SDK_OK = False
    SDK_ERROR = str(exc)

# Refusing the built-in tools by name was tried and reverted: with two fresh
# prompts and no shared cache, passing --disallowed-tools ADDED about 29,000
# tokens to a call (35,847 against 6,602 cache-write tokens for the same
# question), because naming them pulls their schemas in. Passing no tools at
# all already keeps them out. The first measurement that said otherwise had
# compared a cold cache against a warm one.

PERMITTED_COMMANDS = (
    'python agentic/check.py',
    'python agentic/check.py --quick',
    'python3 agentic/check.py',
    'python3 agentic/check.py --quick',
)

# Phrases the provider uses when it is refusing for capacity reasons. Kept
# specific: a bare 'limit' also matches a Node heap-limit crash, which is not
# something waiting fixes.
LIMIT_RE = re.compile(r'usage limit|session limit|rate limit|hit your limit|'
                      r'resets at|resets \d|rate_limit|overloaded|quota|'
                      r'error code: (?:429|529)|\b(?:429|529)\b.*(?:limit|error)',
                      re.I)
SOFT_STOPS = ('error_max_turns', 'error_max_budget_usd')


def reset_wait_seconds(text, now=None):
    """Seconds until the reset time named in a limit message, or None.

    The CLI says e.g. "You've hit your session limit - resets 7:10am
    (Asia/Kolkata)". The clock time is read as local time; if it is already
    past, it means tomorrow.
    """
    import datetime as _dt
    m = re.search(r'resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)', text or '', re.I)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == 'pm':
        hour += 12
    minute = int(m.group(2) or 0)
    now = now or _dt.datetime.now()
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += _dt.timedelta(days=1)
    return int((when - now).total_seconds())


def child_env():
    """The child's environment.

    A short placeholder ANTHROPIC_API_KEY would outrank the Claude Code login
    and every call would fail with 401. The SDK merges its env on top of the
    parent's, so a key left out of the dict is still inherited; it has to be
    removed from this process's own environment. This is a standalone process,
    so that is safe.
    """
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if key and len(key) < 40:
        os.environ.pop('ANTHROPIC_API_KEY', None)
    for name in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_CHILD_SESSION'):
        os.environ.pop(name, None)
    return {}


def availability():
    if not SDK_OK:
        return False, 'claude_agent_sdk is not importable: %s' % SDK_ERROR
    return True, 'claude_agent_sdk'


def extract_json(text):
    """The outermost JSON object in a reply, or None."""
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    start = text.find('{')
    end = text.rfind('}')
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            continue
    return None


def _looks_like_limit(text):
    return bool(LIMIT_RE.search(text or ''))


USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens',
              'cache_read_input_tokens')

# Dollars per million tokens: input, output, cache write, cache read. The
# write rate is the one-hour rate, twice the five-minute rate, because that is
# what the sessions actually use: every cache_creation figure they report is
# ephemeral_1h. With the five-minute rate the estimate came out 30% under the
# bill. Reading still costs a fraction of writing, which is why the loop
# attacks thinking rather than prompt size.
PRICES = {
    'fable': (10.0, 50.0, 25.0, 0.25),
    'opus': (5.0, 25.0, 12.5, 0.50),
    'sonnet': (2.0, 10.0, 5.0, 0.20),
    'haiku': (1.0, 5.0, 2.5, 0.10),
}


def estimate_cost(usage, model):
    """Roughly what a session has spent so far, in dollars."""
    if not usage:
        return 0.0
    key = next((k for k in PRICES if k in (model or '').lower()), 'sonnet')
    pin, pout, pwrite, pread = PRICES[key]
    return (usage.get('input_tokens', 0) * pin
            + usage.get('output_tokens', 0) * pout
            + usage.get('cache_creation_input_tokens', 0) * pwrite
            + usage.get('cache_read_input_tokens', 0) * pread) / 1e6


def add_usage(total, usage):
    """Accumulate one turn's token counts.

    Rough: the per-message usage the CLI sends is not a running total (it
    under-reports what was written and over-reports cache traffic against the
    figures the same run reports at the end), so this is only used to count
    turns and to see that a session is alive. The totals that get recorded
    come from the ResultMessage.
    """
    if not usage:
        return
    for key in USAGE_KEYS:
        val = usage.get(key)
        if isinstance(val, (int, float)):
            total[key] = total.get(key, 0) + int(val)
    total['turns'] = total.get('turns', 0) + 1


async def _ask_async(prompt, system, cwd, model, max_turns, budget, tools,
                     allowed, gate, timeout_s, effort=None, on_text=None,
                     restricted=True, meter=False, burn_rate=None):
    if not SDK_OK:
        return {'status': 'error', 'text': '', 'cost_usd': 0.0, 'turns': 0,
                'error': 'claude_agent_sdk is not importable: ' + SDK_ERROR,
                'tools_used': [], 'seconds': 0.0, 'subtype': ''}
    opts_kwargs = dict(
        model=model, max_turns=max_turns, max_budget_usd=budget,
        tools=list(tools), allowed_tools=list(allowed),
        permission_mode='default' if gate else 'dontAsk',
        can_use_tool=gate, setting_sources=[], env=child_env(),
        # strict-mcp-config keeps the machine's MCP servers out of the child.
        # Whatever connectors the user has (mail, drive, calendar) are useless
        # to a session that may only edit rtl/ and run one command, but their
        # schemas were being sent with every call: measured at 28,891 tokens,
        # which on the expensive model is about $0.72 of cache write per
        # session before a single turn of work, plus the per-turn re-reads.
        # Same question, same tools: 41,862 cache-write tokens without it,
        # 12,971 with it.
        extra_args={'no-session-persistence': None, 'strict-mcp-config': None},
    )
    if cwd:
        opts_kwargs['cwd'] = cwd
        if restricted:
            opts_kwargs['extra_args']['restricted'] = None
    if system:
        opts_kwargs['system_prompt'] = system
    if effort:
        opts_kwargs['effort'] = effort
    opts = ClaudeAgentOptions(**opts_kwargs)

    out = {'status': 'ok', 'text': '', 'cost_usd': 0.0, 'turns': 0,
           'error': '', 'tools_used': [], 'seconds': 0.0, 'subtype': '',
           'usage': None, 'model_usage': None, 'turn_usage': {},
           'rate_limit': None, 'session_id': '',
           # How long the session spent working the design out before it
           # changed anything. This is the number the design map and the
           # handed-over notes are meant to move, so it is measured.
           'first_edit_s': None, 'tool_counts': {}}
    texts = []
    start = time.time()

    if meter and HookMatcher is not None:
        # Three of four sessions on the expensive model were killed by the
        # dollar cap in the middle of a change, because nothing told them
        # where they stood. A session cannot be told its spend (the running
        # figures the CLI sends are not totals), so it is told its time, and
        # the time it is measured against is whichever runs out first: the
        # wall-clock cap, or the dollar cap at the rate this model has been
        # burning in this run.
        said = {'at': 0.0}
        cap_min = timeout_s / 60.0
        if burn_rate and budget:
            cap_min = min(cap_min, budget / burn_rate)

        async def wrapup(_input_data, _tool_use_id, _ctx):
            now = time.time()
            minutes = (now - start) / 60.0
            if minutes < 0.75 * cap_min or now - said['at'] < 120:
                return {}
            said['at'] = now
            return {'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'additionalContext':
                    'BUDGET: %.0f minutes gone, and this session ends at about '
                    '%.0f (whichever comes first, its $%.2f cap or its %.0f '
                    'minute cap). Stop opening new ground. Get the check '
                    'passing on what you have, then write NOTES.md and '
                    'PROPOSAL.json within the next three minutes. A '
                    'half-finished change scores nothing; a small finished one '
                    'scores.' % (minutes, cap_min, budget, timeout_s / 60.0)}}

        opts.hooks = {'PreToolUse': [HookMatcher(hooks=[wrapup])]}

    async def consume():
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                add_usage(out['turn_usage'], msg.usage)
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        texts.append(block.text)
                        if on_text:
                            on_text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        out['tools_used'].append(block.name)
                        out['tool_counts'][block.name] =                             out['tool_counts'].get(block.name, 0) + 1
                        if block.name == 'Read':
                            ranged = bool((block.input or {}).get('offset')
                                          or (block.input or {}).get('limit'))
                            key = 'Read(range)' if ranged else 'Read(whole file)'
                            out['tool_counts'][key] = out['tool_counts'].get(key, 0) + 1
                        if block.name in ('Edit', 'Write', 'MultiEdit')                                 and out['first_edit_s'] is None:
                            out['first_edit_s'] = round(time.time() - start, 1)
            elif RateLimitEvent and isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                out['rate_limit'] = {'status': info.status,
                                     'utilization': info.utilization,
                                     'resets_at': info.resets_at,
                                     'window': info.rate_limit_type,
                                     'seen_at': time.time()}
            elif isinstance(msg, ResultMessage):
                out['cost_usd'] = float(msg.total_cost_usd or 0.0)
                out['turns'] = int(msg.num_turns or 0)
                out['subtype'] = msg.subtype or ''
                out['session_id'] = msg.session_id or ''
                out['usage'] = dict(msg.usage) if msg.usage else None
                out['model_usage'] = ({k: dict(v) for k, v in msg.model_usage.items()}
                                      if msg.model_usage else None)
                if msg.result and not texts:
                    texts.append(msg.result)
                if msg.is_error:
                    err = msg.result or ''
                    if msg.errors:
                        err = err + ' ' + ' '.join(str(e) for e in msg.errors)
                    out['error'] = err.strip()[:600]
                    out['status'] = 'limit' if _looks_like_limit(err) else 'error'
                    if getattr(msg, 'api_error_status', None) in (429, 529):
                        out['status'] = 'limit'

    try:
        await asyncio.wait_for(consume(), timeout=timeout_s)
    except asyncio.TimeoutError:
        out['status'] = 'timeout'
        out['error'] = 'no result within %d s' % timeout_s
    except Exception as exc:              # ProcessError, CLI errors, anything
        text = str(exc)
        if out['status'] == 'ok':
            out['status'] = 'limit' if _looks_like_limit(text) else 'error'
        if not out['error']:
            out['error'] = text[:600]
    out['text'] = '\n'.join(texts)
    out['seconds'] = round(time.time() - start, 1)
    # Hitting the turn or dollar cap is a soft stop: the CLI reports it as an
    # error and exits 1, but the work so far is in the worktree and may well
    # be complete. Keep it as 'budget' so the caller still reads the proposal.
    err_low = (out['error'] or '').lower()
    if out['subtype'] in SOFT_STOPS or 'maximum budget' in err_low \
            or 'maximum number of turns' in err_low or 'max turns' in err_low:
        out['status'] = 'budget'
        out['stopped_by'] = out['subtype'] or 'cap'
    return out


def ask(prompt, system=None, cwd=None, model=None, max_turns=1, budget=3.0,
        tools=(), allowed=(), gate=None, timeout_s=900, effort=None,
        on_text=None, restricted=True, meter=False, burn_rate=None):
    """One model call, synchronous. Returns a dict (see _ask_async)."""
    return asyncio.run(_ask_async(prompt, system, cwd, model or CONFIG['helper_model'],
                                  max_turns, budget, tools, allowed, gate,
                                  timeout_s, effort, on_text, restricted, meter,
                                  burn_rate))


def ask_many(jobs):
    """Run several _ask_async jobs concurrently. jobs = [dict(kwargs)]."""
    async def run_all():
        return await asyncio.gather(*[_ask_async(**job) for job in jobs],
                                    return_exceptions=True)
    results = asyncio.run(run_all())
    out = []
    for res in results:
        if isinstance(res, Exception):
            out.append({'status': 'error', 'error': str(res)[:600], 'text': '',
                        'cost_usd': 0.0, 'turns': 0, 'tools_used': [],
                        'seconds': 0.0})
        else:
            out.append(res)
    return out


# ---------------------------------------------------------------------------
# The gate: the only shell command a candidate session may run.

WRITABLE = ('rtl/', 'PROPOSAL.json', 'NOTES.md')


def make_gate(worktree):
    """The permission gate for one candidate session.

    Bash: only the check command, and only while the harness copy inside the
    worktree is untouched (a session could otherwise edit check.py and run
    anything through the permitted command). Edit/Write: only rtl/, the
    proposal and a notes file. Everything else is allowed (Read/Grep/Glob are
    already confined to the worktree by --restricted).
    """
    root = os.path.abspath(worktree)

    def _harness_clean():
        try:
            res = subprocess.run(['git', 'status', '--porcelain', '--', 'agentic'],
                                 cwd=root, capture_output=True, text=True, timeout=60)
        except Exception:
            return False
        if res.returncode != 0:
            return False
        for line in res.stdout.splitlines():
            path = line[3:].strip().replace('\\', '/')
            # The scoring data is deliberately absent from a candidate
            # worktree; its absence is not a modification of the harness.
            if path.startswith('agentic/data/') or path.endswith('.parquet'):
                continue
            if path:
                return False
        return True

    async def gate(tool_name, tool_input, _ctx):
        tool_input = tool_input or {}
        if tool_name == 'Bash':
            cmd = (tool_input.get('command') or '').strip()
            # Read-only git views of other branches (a rejected attempt's
            # files) are allowed; nothing with pipes, redirects or chaining.
            if re.match(r'^git (show|diff|log|status)\b[^|;&><`$]*$', cmd):
                return PermissionResultAllow()
            # Reverting your own edits inside this worktree is allowed; it is
            # how a resumed session gets back to a design that compiles.
            if re.match(r'^git checkout -- rtl(/[\w./-]+)?$', cmd):
                return PermissionResultAllow()
            if cmd not in PERMITTED_COMMANDS:
                return PermissionResultDeny(
                    message=('Only these commands are permitted: '
                             '`python agentic/check.py` (or with --quick), and read-only '
                             '`git show`, `git diff`, `git log` without pipes or redirects. '
                             'Use the Read, Grep, Glob, Edit and Write tools for everything else.'))
            if not _harness_clean():
                return PermissionResultDeny(
                    message=('agentic/ has been modified in this worktree. Restore it '
                             '(it is the measuring harness) before running the check.'))
            return PermissionResultAllow()
        if tool_name in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
            path = tool_input.get('file_path') or tool_input.get('path') or ''
            try:
                rel = os.path.relpath(os.path.abspath(path), root).replace('\\', '/')
            except ValueError:
                rel = '..'
            if rel.startswith('..') or not any(rel == w or rel.startswith(w) for w in WRITABLE):
                return PermissionResultDeny(
                    message=('You may only write under rtl/, plus PROPOSAL.json and '
                             'NOTES.md at the top of the worktree. %s is off limits.' % rel))
            return PermissionResultAllow()
        return PermissionResultAllow()

    return gate


async def bash_gate(tool_name, tool_input, _ctx):
    """Gate without a worktree: only the check command."""
    if tool_name != 'Bash':
        return PermissionResultAllow()
    cmd = ((tool_input or {}).get('command') or '').strip()
    if cmd in PERMITTED_COMMANDS:
        return PermissionResultAllow()
    return PermissionResultDeny(message='Only `python agentic/check.py` is permitted.')


# ---------------------------------------------------------------------------
# Prompts

def synth_phrase():
    """What f_max and area come from, as the brief tells a session."""
    if CONFIG['synth_backend'] == 'hacc':
        return ('Vivado synthesis, place and route on an FPGA, part %s; area\n'
                '     is counted in LUTs, and the history RAM is real URAM, so\n'
                '     paths through it are timed' % CONFIG['hacc_part'])
    return 'Yosys/ABC synthesis on a 45 nm library'


SYSTEM_BRIEF = """\
You are an expert digital designer improving the RTL of vhsnunzip, a VHDL-2008
hardware Snappy decompressor, inside a git worktree. Your job this session is
to make ONE well-reasoned change to the design and describe it. Another
program will measure it; you will not.

THE DESIGN
  rtl/                     the design. Top entity: vhsnunzip_unbuffered.
  Pipeline (all in rtl/): pre_decoder -> decoder (+ decoder_long) -> cmd_gen_1
  -> cmd_gen_2 -> pipeline datapath with a history RAM (vhsnunzip_ram).
  rtl/vhsnunzip_int_pkg.vhd holds the record types that move between stages.

WHAT YOU MAY CHANGE
  rtl/*.vhd                any file, and you may add new files under rtl/.
  Keep the top entity name vhsnunzip_unbuffered and its port NAMES
  (clk, reset, co_valid, co_ready, co_data, co_cnt, co_last, de_valid,
  de_ready, de_dvalid, de_data, de_cnt, de_last). Port WIDTHS may change.
  The measuring testbench reads the widths out of your RTL, so declare the
  top-level ports with literal ranges, e.g. std_logic_vector(127 downto 0)
  or (LINE_BYTES*8-1 downto 0) with LINE_BYTES a plain integer constant in
  the same file. Count convention: a cnt field that is exactly log2(width)
  bits wide means 0 = all bytes valid (the original 8-byte ports use this);
  a wider cnt field is a literal count. co_data must stay a multiple of 8
  bytes. Do not change the simulation RAM's latency (CMD_STAGES /
  RESP_STAGES in vhsnunzip_ram.sim.vhd): synthesis uses a fixed memory, so a
  faster simulated RAM would be scored but never built.
  If you change the RAM interface, keep the entity vhsnunzip_ram with ports
  a_cmd/a_resp/b_cmd/b_resp of the record types ram_command/ram_response,
  because synthesis swaps in a memory stub with that interface.

WHAT YOU MUST NOT CHANGE
  agentic/ (the harness), vectors/, model/, tb/. The unit testbenches in tb/
  compare against a model of the ORIGINAL micro-architecture, so they are
  allowed to break; they are not used to judge you. Only the bytes that come
  out of the top entity are judged. The worktree is exactly as intended: do
  not try to restore, fetch or recreate any file that seems to be missing.

HOW YOU ARE JUDGED
  1. Correctness: compressed Snappy chunks in, and every output byte must
     equal a frozen reference decompressor, on real Parquet pages and on
     synthetic chunks. A deadlock or one wrong byte rejects the candidate.
  2. Throughput = bytes per cycle (real Parquet pages) x f_max (from
     @@SYNTH@@). Both halves count. A change that raises
     bytes/cycle a little and lowers f_max more is a loss.
  3. Area is priced: score = -gain% + 0.15 x area_growth% (more above 10%
     area growth). Area is not capped, but silicon must buy throughput.

THE ONE COMMAND YOU MAY RUN (exactly as written, nothing else)
  python agentic/check.py           compile + simulate 7 invented shapes and
                                    compare every byte with the reference.
                                    Also prints bytes/cycle and a stage
                                    profile on invented data (about 15 s).
                                    THE LOOP HAS ALREADY RUN THIS FOR YOU on
                                    the design you start from, and its output
                                    is in your brief. Running it again before
                                    you have edited anything tells you what
                                    you were already told, so do not: your
                                    first check comes after your first edit.
  python agentic/check.py --quick   the first three shapes.
  Also allowed, read-only and without pipes or redirects: `git show`,
  `git diff`, `git log`. When the history names an earlier attempt's branch,
  `git show <branch>:rtl/<file>` shows its version of a file; reuse what was
  right in it and fix what was wrong. Any other shell command is refused.
  Use Read/Grep/Glob/Edit/Write for everything else. You cannot see the
  scoring data; that is deliberate.

HOW TO WORK
  * A map of the design, generated from this worktree's RTL, is in the brief
    below: sizes, the records passed between stages, the module tree, and the
    line ranges of every process. Start from it. When it tells you a process
    runs from line 455 to 733, read those lines with Read's offset and limit
    instead of pulling in the whole 900-line file: a file you read stays in
    front of you for the rest of the session and is paid for again every turn,
    so reading four whole files to find one process is most of what a session
    wastes. Read wider when you need to; just start narrow. The map can be
    stale about anything you change, and the code always wins over the map.
  * Read the RTL before changing it. Understand the stage the profile names.
  * Make the change in small verified steps. Run the check after each step.
    An earlier attempt elaborated cleanly and drove X on undriven byte lanes;
    only simulation found it. Do not finish with a failing check.
  * Aim at the measured bottleneck and at real column data (pages of a few
    hundred bytes to 64 KiB, mixed literals and copies, copy offsets from 1
    to thousands, overlapping copies in sequential integers). Do not tune to
    the invented shapes.
  * Keep the change coherent: one idea, fully carried through every stage it
    touches (record types, both command generators, the datapath, the top).
  * If your assigned direction turns out to be wrong after reading the RTL,
    say so in the rationale and do the best change you can justify instead.

BUDGET
  This session may spend @@BUDGET@@; when either runs out it ends at once,
  mid-edit, and whatever is in rtl/ is what gets measured. You will be told
  when three quarters of it is gone. So keep the design compiling and passing the check between
  steps, write a first PROPOSAL.json as soon as you have decided what to
  build, and update it at the end. A half-finished edit that does not
  compile scores nothing.

WHEN YOU ARE DONE
  First write NOTES.md at the top of the worktree, under exactly these four
  headings, about 400 words in total:
    ## How it works
      What you worked out about the parts of the design you read: what a stage
      does per cycle, what limits it, how the records and handshakes fit
      together. Write it for the next session, which will start from your notes
      instead of rediscovering this. Name files and line numbers.
    ## What I tried
      The change you built, in a few sentences.
    ## Why it worked or failed
      What the check showed, and what you would do differently.
    ## Facts
      One line per fact that is true of this design whatever anyone tries next,
      each one something you verified rather than assumed. These are kept and
      handed to every later session, so a wrong line here costs more than a
      missing one.
  Then write PROPOSAL.json at the top of the worktree:
  {"id": "kebab-case-name",
   "rationale": "what you changed and why it addresses the measured bottleneck",
   "expected_gain_pct": <number, real prediction of throughput change>,
   "expected_effect": "what should happen to bytes/cycle, f_max and area",
   "risk": "what you are least sure about",
   "primary_skill": "the ONE skill id your change is a test of, or null if
     none fits. This is the only id scored against your result, so name the
     mechanism you actually built, not the one you were assigned.",
   "skills_used": ["any other skill ids you applied"]}
  expected_gain_pct is scored against the measurement afterwards; an honest
  small number is worth more than a hopeful one.

  Declining is the last resort, not an option to reach for after reading.
  A structural change (wider line, wider port, more elements per cycle) is
  hard and touches several files; that is exactly why this session exists
  and why it has a large budget. Plan it, build it in verified steps, and
  keep going while the check passes. Only if the change is genuinely
  impossible or the measured profile contradicts the assignment, write
  {"id": "none", "rationale": "why"} and leave the RTL as you found it.
"""


RESUME_PREFIX = """\
YOU HAVE ALREADY STARTED THIS CHANGE IN THIS WORKTREE.

The provider's usage window ran out in the middle of your last session and the
loop waited for it to reset. Nothing was thrown away: your edits are still in
rtl/, and NOTES.md is there if you wrote one.

Start with `git diff` to see what you changed and `python agentic/check.py` to
see whether it still passes. If it fails and the cause is not obvious within a
few minutes, run `git checkout -- rtl` and rebuild the change more simply. Then
finish it and write NOTES.md and PROPOSAL.json.

Your assignment has not changed; it is repeated below.

"""


def build_user_prompt(ctx, direction, others, resumed=False):
    """The measured state plus this candidate's assignment."""
    lines = []
    if resumed:
        lines.append(RESUME_PREFIX)
    lines.append('GOAL: %s' % ctx['goal_text'])
    lines.append('ITERATION %d of %d. Best design so far vs the original: %s'
                 % (ctx['iteration'], ctx['max_iters'], ctx['progress_text']))
    lines.append('')
    lines.append('CURRENT DESIGN, MEASURED')
    lines.append(ctx['state_text'])
    lines.append('')
    lines.append('STAGE PROFILE (rate vs ceiling read from the RTL)')
    lines.append(ctx['profile_text'])
    lines.append('')
    if ctx.get('guide_text'):
        lines.append('A MAP OF THE DESIGN, GENERATED FROM THE RTL YOU HAVE')
        lines.append('Use its line numbers to read the part you need instead of '
                     'reading whole files. It describes the design only. Where it '
                     'disagrees with the code, the code is right.')
        lines.append('')
        lines.append(ctx['guide_text'])
        lines.append('')
    lines.append('WHAT THE PROFILE SAYS TO MOVE: %s -- %s'
                 % (ctx['lever']['lever'], ctx['lever']['reason']))
    lines.append('')
    lines.append('SKILL LIBRARY (what has worked and what has not)')
    lines.append(ctx['skills_text'])
    lines.append('')
    lines.append('HISTORY OF THIS RUN (most recent last)')
    lines.append(ctx['history_text'] or '(nothing tried yet)')
    lines.append('')
    lines.append('YOUR ASSIGNMENT FOR THIS SESSION')
    lines.append('  focus:      %s' % direction.get('focus', ''))
    lines.append('  hypothesis: %s' % direction.get('hypothesis', ''))
    if direction.get('skill_ids'):
        lines.append('  skills:     %s' % ', '.join(direction['skill_ids']))
    if direction.get('where_to_look'):
        lines.append('  look at:    %s' % direction['where_to_look'])
    if others:
        lines.append('')
        lines.append('OTHER CANDIDATES RUNNING IN PARALLEL (do not duplicate them):')
        for od in others:
            lines.append('  - %s' % od.get('focus', ''))
    lines.append('')
    if ctx.get('check_text'):
        lines.append('THE CHECK ALREADY PASSES ON THIS DESIGN. The loop ran '
                     '`python agentic/check.py` for you on exactly this RTL:')
        lines.append(ctx['check_text'])
        lines.append('')
        lines.append('So do not run the check until you have edited something. '
                     'Read the map and the RTL that matters for your focus, then '
                     'make the change, then check it.')
    else:
        lines.append('Start by running `python agentic/check.py` once to see the '
                     'design work, then read the RTL that matters for your focus, '
                     'then make the change.')
    return '\n'.join(lines)


PLAN_SYSTEM = """\
You are the analysis agent of an RTL optimisation loop for vhsnunzip, a
VHDL-2008 hardware Snappy decompressor. You do not edit code. You read the
measured profile, the skill library and the history, and you choose what N
parallel RTL-writing sessions should each attempt this iteration. Reply with
JSON only."""


def build_plan_prompt(ctx, n):
    return """\
GOAL: %s
ITERATION %d of %d. Progress so far: %s

CURRENT DESIGN, MEASURED
%s

STAGE PROFILE
%s

LEVER: %s -- %s

SKILL LIBRARY
%s

FACTS SESSIONS HAVE VERIFIED ABOUT THIS DESIGN
%s

HISTORY (most recent last)
%s

Choose %d DIFFERENT directions for %d parallel candidate sessions. Rules:
- Each direction targets the measured bottleneck or a credible second one;
  never an idle stage. Prefer high-confidence skills that fit the profile.
- Direction 1 MUST raise the per-cycle ceiling of the stage the lever names
  (a structural change: wider line, wider port, more elements per cycle),
  not a timing tweak, unless the history shows that exact change failing
  three times. Timing tweaks are for the other directions. Spell out the
  steps: which records, which stages, which files, in what order.
- A stage can usually be relieved two ways: more BYTES PER COMMAND (a wider
  line or port) or more ELEMENTS PER CYCLE (another slot, dual issue). They
  are different mechanisms and they do not have the same track record. Read
  the FACTS section before choosing: if it records a mechanism that has been
  measured to pay from this design state, one direction MUST be that
  mechanism, even when the profile names a stage the other one would relieve.
  A stage being tightest says where the limit is, not which mechanism moves
  it. Never spend both directions on the same mechanism family.
- Do not repeat a direction that failed in the history unless you say what
  is different this time. Never choose an AVOID skill.
- Diversify: different mechanisms, not three flavours of one idea. Include
  at most one aggressive/architectural direction and at least one cheap,
  low-risk one.
- Each direction must be concrete enough that a coding session can start:
  name the stage, the mechanism and the files to read.

Reply with JSON only:
{"directions": [
  {"focus": "one line, the change to make",
   "hypothesis": "why it should raise real-data throughput, with a predicted %% gain",
   "skill_ids": ["ids from the library"],
   "where_to_look": "files/entities to read first",
   "risk": "what could go wrong"}
 ],
 "note": "one or two sentences on the overall state"}
""" % (ctx['goal_text'], ctx['iteration'], ctx['max_iters'], ctx['progress_text'],
       ctx['state_text'], ctx['profile_text'], ctx['lever']['lever'],
       ctx['lever']['reason'], ctx['skills_text'],
       ctx.get('facts_text') or '(none recorded yet)',
       ctx['history_text'] or '(nothing tried yet)', n, n)


FALLBACK_DIRECTIONS = [
    {'focus': 'Raise the per-cycle capacity of the stage the profile names as binding',
     'hypothesis': 'Relieving the binding stage moves the geomean; widening an idle stage does not.',
     'skill_ids': ['relieve-the-binding-stage'], 'where_to_look': 'rtl/vhsnunzip_int_pkg.vhd, rtl/vhsnunzip_cmd_gen_2.vhd, rtl/vhsnunzip_pipeline.vhd'},
    {'focus': 'Find and remove handshake bubbles between stages (output idle cycles with no input stall)',
     'hypothesis': 'Idle output cycles with no backpressure are latency, not capacity; a cheap fix.',
     'skill_ids': ['remove-inter-stage-bubbles'], 'where_to_look': 'rtl/vhsnunzip_pipeline.vhd, rtl/vhsnunzip_cmd_gen_1.vhd'},
    {'focus': 'Let the decoder retire more Snappy elements per cycle on small-element data',
     'hypothesis': 'Real column data has 5-9 byte elements; the element rate binds before the byte rate.',
     'skill_ids': ['retire-more-elements-per-cycle'], 'where_to_look': 'rtl/vhsnunzip_decoder.vhd, rtl/vhsnunzip_cmd_gen_1.vhd'},
]


def plan_directions(ctx, n, log):
    """N directions for this iteration. Falls back to fixed ones on failure."""
    res = ask(build_plan_prompt(ctx, n), system=PLAN_SYSTEM,
              model=CONFIG['helper_model'], max_turns=1, budget=2.0,
              timeout_s=600, effort='medium')
    data = extract_json(res.get('text', '')) if res.get('status') == 'ok' else None
    dirs = []
    if data and isinstance(data.get('directions'), list):
        for d in data['directions']:
            if isinstance(d, dict) and d.get('focus'):
                sids = d.get('skill_ids') or []
                if isinstance(sids, str):
                    sids = [sids]
                if not isinstance(sids, list):
                    sids = []
                dirs.append({'focus': str(d.get('focus', ''))[:400],
                             'hypothesis': str(d.get('hypothesis', ''))[:600],
                             'skill_ids': [str(s)[:60] for s in sids][:6],
                             'where_to_look': str(d.get('where_to_look', ''))[:300],
                             'risk': str(d.get('risk', ''))[:300]})
        if data.get('note'):
            log('  planner: %s' % str(data['note'])[:300])
    if res.get('status') != 'ok':
        log('  planner call failed (%s): %s' % (res.get('status'), res.get('error', '')[:200]))
    while len(dirs) < n:
        dirs.append(dict(FALLBACK_DIRECTIONS[len(dirs) % len(FALLBACK_DIRECTIONS)]))
    return dirs[:n], res


NOTES_CAP = 6000


def read_notes(worktree):
    """What the session wrote down about the design, capped."""
    path = os.path.join(worktree, 'NOTES.md')
    if not os.path.isfile(path):
        return ''
    try:
        with open(path, encoding='utf-8', errors='replace') as fil:
            return fil.read()[:NOTES_CAP]
    except OSError:
        return ''


def notes_section(notes, wanted):
    """One '## heading' section out of a notes file, by keyword."""
    if not notes:
        return ''
    body, keep = [], False
    for line in notes.splitlines():
        if line.lstrip().startswith('#'):
            keep = wanted.lower() in line.lower()
            continue
        if keep:
            body.append(line)
    return '\n'.join(body).strip()


def fact_lines(notes, cap=6):
    """The '## Facts' bullets, one string each."""
    out = []
    for line in notes_section(notes, 'fact').splitlines():
        line = line.strip().lstrip('-*').strip()
        if len(line) > 12:
            out.append(line[:300])
    return out[:cap]


def read_proposal(worktree):
    path = os.path.join(worktree, 'PROPOSAL.json')
    if not os.path.isfile(path):
        return None
    text = ''
    try:
        with open(path, encoding='utf-8', errors='replace') as fil:
            text = fil.read()
        data = json.loads(text)
    except (OSError, ValueError):
        data = extract_json(text) if text else None
    if not isinstance(data, dict):
        return None
    out = {
        'id': re.sub(r'[^a-z0-9-]+', '-', str(data.get('id', 'unnamed')).lower()).strip('-')[:60] or 'unnamed',
        'rationale': str(data.get('rationale', ''))[:2000],
        'expected_gain_pct': None,
        'expected_effect': str(data.get('expected_effect', ''))[:800],
        'risk': str(data.get('risk', ''))[:600],
        'skills_used': [str(s)[:60] for s in (data.get('skills_used') or [])][:8],
        'primary_skill': (str(data['primary_skill'])[:60]
                          if data.get('primary_skill') else None),
    }
    try:
        out['expected_gain_pct'] = float(data.get('expected_gain_pct'))
    except (TypeError, ValueError):
        pass
    return out


def burn_rate(state, model):
    """Dollars a minute this model has actually cost in this run, or None."""
    rates = []
    for it in (state or {}).get('iterations', []):
        for cand in it.get('candidates', []):
            sess = cand.get('session') or {}
            if cand.get('model') != model or not sess.get('cost_usd'):
                continue
            if (sess.get('seconds') or 0) > 60:
                rates.append(sess['cost_usd'] / (sess['seconds'] / 60.0))
    if not rates:
        return None
    rates.sort()
    return rates[len(rates) // 2]


def write_candidates(ctx, assignments, log, rate=None):
    """Run one coding session per assignment, all at once.

    assignments = [{'label': ..., 'worktree': path, 'direction': {...}}]
    Returns one dict per assignment with the session result and the proposal.
    """
    jobs = []
    for idx, asg in enumerate(assignments):
        others = [a['direction'] for j, a in enumerate(assignments) if j != idx]
        label = asg['label']

        def on_text(text, label=label):
            first = text.strip().splitlines()[0] if text.strip() else ''
            if first:
                log('    %s | %s' % (label, first[:110]))

        jobs.append(dict(
            prompt=build_user_prompt(ctx, asg['direction'], others,
                                     resumed=bool(asg.get('resumed'))),
            meter=True, burn_rate=rate,
            system={'type': 'preset', 'preset': 'claude_code',
                    'append': SYSTEM_BRIEF.replace(
                        '@@BUDGET@@', 'about $%.0f and %d minutes'
                        % (float(CONFIG['session_budget_usd']),
                           int(CONFIG['session_timeout_min']))).replace(
                        '@@SYNTH@@', synth_phrase())},
            cwd=asg['worktree'], model=CONFIG['model'],
            max_turns=int(CONFIG['session_max_turns']),
            budget=float(CONFIG['session_budget_usd']),
            tools=('Read', 'Edit', 'Write', 'Glob', 'Grep', 'Bash'),
            # Edit/Write/Bash are left out of the auto-approve list so every
            # call reaches the gate (an allowed tool never does).
            allowed=('Read', 'Glob', 'Grep'),
            gate=make_gate(asg['worktree']),
            timeout_s=int(CONFIG['session_timeout_min']) * 60,
            effort=CONFIG.get('effort') or None,
            on_text=on_text, restricted=True))

    results = ask_many(jobs)
    out = []
    for asg, res in zip(assignments, results):
        proposal = read_proposal(asg['worktree'])
        out.append({'label': asg['label'], 'direction': asg['direction'],
                    'session': {k: res.get(k) for k in
                                ('status', 'cost_usd', 'turns', 'seconds',
                                 'error', 'subtype', 'stopped_by', 'usage',
                                 'turn_usage', 'model_usage', 'rate_limit',
                                 'session_id', 'first_edit_s', 'tool_counts')},
                    'proposal': proposal})
    return out
