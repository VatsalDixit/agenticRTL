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


async def _ask_async(prompt, system, cwd, model, max_turns, budget, tools,
                     allowed, gate, timeout_s, effort=None, on_text=None,
                     restricted=True):
    if not SDK_OK:
        return {'status': 'error', 'text': '', 'cost_usd': 0.0, 'turns': 0,
                'error': 'claude_agent_sdk is not importable: ' + SDK_ERROR,
                'tools_used': [], 'seconds': 0.0, 'subtype': ''}
    opts_kwargs = dict(
        model=model, max_turns=max_turns, max_budget_usd=budget,
        tools=list(tools), allowed_tools=list(allowed),
        permission_mode='default' if gate else 'dontAsk',
        can_use_tool=gate, setting_sources=[], env=child_env(),
        extra_args={'no-session-persistence': None},
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
           'error': '', 'tools_used': [], 'seconds': 0.0, 'subtype': ''}
    texts = []
    start = time.time()

    async def consume():
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        texts.append(block.text)
                        if on_text:
                            on_text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        out['tools_used'].append(block.name)
            elif isinstance(msg, ResultMessage):
                out['cost_usd'] = float(msg.total_cost_usd or 0.0)
                out['turns'] = int(msg.num_turns or 0)
                out['subtype'] = msg.subtype or ''
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
        on_text=None, restricted=True):
    """One model call, synchronous. Returns a dict (see _ask_async)."""
    return asyncio.run(_ask_async(prompt, system, cwd, model or CONFIG['helper_model'],
                                  max_turns, budget, tools, allowed, gate,
                                  timeout_s, effort, on_text, restricted))


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
            if cmd not in PERMITTED_COMMANDS:
                return PermissionResultDeny(
                    message=('Only this command is permitted, exactly as written: '
                             '`python agentic/check.py` (or with --quick). Use the '
                             'Read, Grep, Glob, Edit and Write tools for everything else.'))
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
  2. Throughput = bytes per cycle (real Parquet pages) x f_max (from Yosys/ABC
     synthesis on a 45 nm library). Both halves count. A change that raises
     bytes/cycle a little and lowers f_max more is a loss.
  3. Area is priced: score = -gain% + 0.15 x area_growth% (more above 10%
     area growth). Area is not capped, but silicon must buy throughput.

THE ONE COMMAND YOU MAY RUN (exactly as written, nothing else)
  python agentic/check.py           compile + simulate 7 invented shapes and
                                    compare every byte with the reference.
                                    Also prints bytes/cycle and a stage
                                    profile on invented data (about 15 s).
  python agentic/check.py --quick   the first three shapes.
  Any other shell command is refused. Use Read/Grep/Glob/Edit/Write for
  everything else. You cannot see the scoring data; that is deliberate.

HOW TO WORK
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
  The session has a turn cap and a dollar cap; when either is reached it
  ends at once. So keep the design compiling and passing the check between
  steps, write a first PROPOSAL.json as soon as you have decided what to
  build, and update it at the end. A half-finished edit that does not
  compile scores nothing.

WHEN YOU ARE DONE
  Write PROPOSAL.json at the top of the worktree:
  {"id": "kebab-case-name",
   "rationale": "what you changed and why it addresses the measured bottleneck",
   "expected_gain_pct": <number, real prediction of throughput change>,
   "expected_effect": "what should happen to bytes/cycle, f_max and area",
   "risk": "what you are least sure about",
   "skills_used": ["skill ids from the library you applied"]}
  expected_gain_pct is scored against the measurement afterwards; an honest
  small number is worth more than a hopeful one. If you could not produce a
  working change, write {"id": "none", "rationale": "why"} and leave the RTL
  as you found it.
"""


def build_user_prompt(ctx, direction, others):
    """The measured state plus this candidate's assignment."""
    lines = []
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

HISTORY (most recent last)
%s

Choose %d DIFFERENT directions for %d parallel candidate sessions. Rules:
- Each direction targets the measured bottleneck or a credible second one;
  never an idle stage. Prefer high-confidence skills that fit the profile.
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
    }
    try:
        out['expected_gain_pct'] = float(data.get('expected_gain_pct'))
    except (TypeError, ValueError):
        pass
    return out


def write_candidates(ctx, assignments, log):
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
            prompt=build_user_prompt(ctx, asg['direction'], others),
            system={'type': 'preset', 'preset': 'claude_code',
                    'append': SYSTEM_BRIEF},
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
                                 'error', 'subtype', 'stopped_by')},
                    'proposal': proposal})
    return out
