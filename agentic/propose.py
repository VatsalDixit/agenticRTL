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
                      edit files there, and may run only the check
                      (`python agentic/check.py`), the packing model
                      (`python agentic/packmodel.py`, aggregates only) and a
                      few read-only git forms. It cannot read outside the
                      worktree and it never sees the scoring stimulus. It
                      ends by writing PROPOSAL.json.

Both go through the Claude Agent SDK, which drives the local `claude`
executable and uses the Claude Code login on the machine (or an API key if
a real one is set). Nothing else in the loop talks to a model.
"""

import asyncio
import hashlib
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
                     restricted=True, meter=False, burn_rate=None, env_extra=None):
    if not SDK_OK:
        return {'status': 'error', 'text': '', 'cost_usd': 0.0, 'turns': 0,
                'error': 'claude_agent_sdk is not importable: ' + SDK_ERROR,
                'tools_used': [], 'seconds': 0.0, 'subtype': ''}
    opts_kwargs = dict(
        model=model, max_turns=max_turns, max_budget_usd=budget,
        tools=list(tools), allowed_tools=list(allowed),
        permission_mode='default' if gate else 'dontAsk',
        can_use_tool=gate, setting_sources=[],
        # The SDK merges this over the parent's environment, so only what
        # differs per session goes in (the packing model's calibration).
        env=dict(child_env(), **(env_extra or {})),
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
# The gate: the only shell commands a candidate session may run.

WRITABLE = ('rtl/', 'PROPOSAL.json', 'NOTES.md')


def track_writable(track_id, kind):
    """What a build-track session may write. A build step adds its track's
    docs folder (corrections to the spec, STEP-<n>.md); the design step
    writes only the docs: it must leave rtl/ as it found it."""
    docs = 'docs/track-%s/' % track_id
    if kind == 'design':
        return (docs, 'PROPOSAL.json', 'NOTES.md')
    return ('rtl/', docs, 'PROPOSAL.json', 'NOTES.md')


# A build-track step's unit test (agentic/check.py --unit). The name is the
# test's entity and file: rtl/unit/<name>.vhd.
UNIT_RE = re.compile(r'^python3? agentic/check\.py --unit [A-Za-z0-9_]{1,40}$')

# The packing model, with its what-if flags and nothing else. Its loop-only
# flags (--prepare, --extra, --json, --selfcheck) cannot match, and neither
# can anything chained, piped or redirected.
PACKMODEL_RE = re.compile(
    r'^python3? agentic/packmodel\.py'
    r'(?: --(?:k|l|n|litrate) \d{1,3}| --(?:hazard|split|rules) [a-z0-9]{1,10})*$')

# Read-only git, as an explicit grammar. The old rule (any git show/diff/log/
# status without shell metacharacters) let `git diff --no-index <abs path> x`
# print any file on disk (the run's state.json, the packing model's cache) and
# `--output=<path>` write any file. Here a revision cannot start with '-' or
# '/', a path is relative and under rtl/ or docs/ with no segment starting
# with '.', and every form that prints file contents needs such a pathspec, so
# the harness and its data are never shown.
_REV = r'[A-Za-z0-9_][A-Za-z0-9_/~^-]*(?:\.[A-Za-z0-9_/~^-]+)*'
_RANGE = r'%s(?:\.\.%s)?' % (_REV, _REV)
_PATH = r'(?:rtl|docs)(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*/?'
_PATHS = r'(?: %s)+' % _PATH
GIT_FORMS = tuple(re.compile(form) for form in (
    r'^git status(?: --porcelain| --short)?$',
    r'^git show %s:%s$' % (_REV, _PATH),
    r'^git show %s --stat$' % _REV,
    r'^git show %s --%s$' % (_REV, _PATHS),
    r'^git diff(?: --stat| --name-only)?(?: %s)? --%s$' % (_RANGE, _PATHS),
    r'^git log(?: --oneline| --stat| -n \d{1,4})*(?: %s)?$' % _RANGE,
    r'^git log -p(?: --oneline| -n \d{1,4})*(?: %s)? --%s$' % (_RANGE, _PATHS),
))
# Reverting your own edits inside this worktree is allowed; it is how a
# resumed session gets back to a design that compiles. No '..' segments.
CHECKOUT_RE = re.compile(r'^git checkout -- rtl(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*/?$')

GIT_FORMS_TEXT = """\
  git status [--porcelain|--short]
  git show REV:rtl/<file>             (or REV:docs/<file>)
  git show REV --stat
  git show REV -- rtl/...
  git diff [--stat|--name-only] [REV[..REV]] -- rtl/...
  git log [--oneline] [--stat] [-n N] [REV[..REV]]
  git log -p [--oneline] [-n N] [REV[..REV]] -- rtl/...
  git checkout -- rtl[/<file>]        (undo your own edits)"""


def command_kind(cmd):
    """What a session's shell command is: 'check', 'packmodel', 'git',
    'checkout', or None (refused)."""
    cmd = (cmd or '').strip()
    if cmd in PERMITTED_COMMANDS or UNIT_RE.match(cmd):
        return 'check'
    if PACKMODEL_RE.match(cmd):
        return 'packmodel'
    if any(form.match(cmd) for form in GIT_FORMS):
        return 'git'
    if CHECKOUT_RE.match(cmd):
        return 'checkout'
    return None


# ---------------------------------------------------------------------------
# The harness a session runs.
#
# A candidate worktree starts from the run's base commit, and adoptions change
# only rtl/, so without this a session would run the kit as it was when the
# run started: a new packmodel.py or a fixed measure.py would never reach it.
# Only this allowlist is copied; never the data, the tests or anything else.

SYNC_FILES = ('check.py', 'measure.py', 'analyse.py', 'probe.py', 'packmodel.py',
              'stim.py', 'oracle.py', 'tools.py', 'hacc.py', 'freeze.py',
              'config.json', 'frozen.json')
SYNC_DIRS = ('ref', 'syn', 'tb')


def sync_list(kit=KIT):
    """Kit-relative paths (forward slashes) of everything the sync copies."""
    out = [name for name in SYNC_FILES if os.path.isfile(os.path.join(kit, name))]
    for top in SYNC_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(kit, top)):
            dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')
            for name in sorted(filenames):
                if name.endswith(('.pyc', '.parquet')):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), kit)
                out.append(rel.replace('\\', '/'))
    return out


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sync_harness(worktree, manifest_path, kit=KIT):
    """Copy the allowlisted kit files into worktree/agentic where they differ,
    and record {path: sha256} of the synced harness in manifest_path (outside
    the worktree). Returns the manifest.

    The gate checks the worktree against this manifest rather than against
    the live kit, which the engineer may edit while a session runs. The
    manifest is written even when a copy fails part way, so what did land is
    accounted for.
    """
    manifest = {}
    dest = os.path.join(worktree, 'agentic')
    try:
        for rel in sync_list(kit):
            with open(os.path.join(kit, rel), 'rb') as fil:
                data = fil.read()
            target = os.path.join(dest, rel)
            try:
                with open(target, 'rb') as fil:
                    same = fil.read() == data
            except OSError:
                same = False
            if not same:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                tmp = target + '.sync.tmp'
                with open(tmp, 'wb') as fil:
                    fil.write(data)
                os.replace(tmp, target)
            manifest['agentic/' + rel] = _sha256_bytes(data)
    finally:
        os.makedirs(os.path.dirname(os.path.abspath(manifest_path)), exist_ok=True)
        tmp = manifest_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fil:
            json.dump(manifest, fil, indent=1, sort_keys=True)
        os.replace(tmp, manifest_path)
    return manifest


def read_manifest(manifest_path):
    try:
        with open(manifest_path, encoding='utf-8') as fil:
            data = json.load(fil)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def harness_clean(root, manifest=None):
    """True when agentic/ in the worktree is the harness the loop put there.

    A modified or untracked file under agentic/ is acceptable only if it is
    the scoring data's absence (agentic/data/, *.parquet: blind() removed
    them) or has exactly the bytes the sync recorded for it. -z and
    --untracked-files=all so a new folder is not collapsed into one line and
    names are not quoted.
    """
    manifest = manifest or {}
    try:
        res = subprocess.run(['git', 'status', '--porcelain', '-z',
                              '--untracked-files=all', '--', 'agentic'],
                             cwd=root, capture_output=True, timeout=60)
    except Exception:
        return False
    if res.returncode != 0:
        return False
    entries = res.stdout.decode('utf-8', 'replace').split('\0')
    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        idx += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:].replace('\\', '/')
        if status[0] in 'RC':
            idx += 1                      # the original path follows
        if path.startswith('agentic/data/') or path.endswith('.parquet'):
            continue
        want = manifest.get(path)
        full = os.path.join(root, path)
        if want and os.path.isfile(full):
            try:
                with open(full, 'rb') as fil:
                    if _sha256_bytes(fil.read()) == want:
                        continue
            except OSError:
                pass
        return False
    return True


def make_gate(worktree, manifest_path=None, writable=WRITABLE):
    """The permission gate for one candidate session.

    Bash: the check and the packing model, only while the harness inside the
    worktree is the one the loop synced (a session could otherwise edit
    check.py and run anything through the permitted command); plus the
    read-only git forms above. Edit/Write: only rtl/, the proposal and a
    notes file, or the build-track list ``writable`` (track_writable).
    Everything else is allowed (Read/Grep/Glob are already confined to the
    worktree by --restricted).
    """
    root = os.path.abspath(worktree)
    writable = tuple(writable)
    where = ', '.join(w for w in writable)
    # Read once, when the session starts: the manifest of this session's sync.
    manifest = read_manifest(manifest_path) if manifest_path else {}

    async def gate(tool_name, tool_input, _ctx):
        tool_input = tool_input or {}
        if tool_name == 'Bash':
            cmd = (tool_input.get('command') or '').strip()
            kind = command_kind(cmd)
            if kind in ('git', 'checkout'):
                return PermissionResultAllow()
            if kind is None:
                return PermissionResultDeny(
                    message=('Only these commands are permitted, exactly as written: '
                             '`python agentic/check.py` (or with --quick, or '
                             '--unit NAME for a build-track unit test); '
                             '`python agentic/packmodel.py` with any of --k K, --l L, '
                             '--n N, --hazard H, --litrate B, --split S, --rules R; '
                             'and these read-only git forms (no other flags, no pipes or '
                             'redirects; paths under rtl/ or docs/):\n' + GIT_FORMS_TEXT +
                             '\nUse the Read, Grep, Glob, Edit and Write tools for '
                             'everything else.'))
            if not harness_clean(root, manifest):
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
            if rel.startswith('..') or not any(rel == w or rel.startswith(w) for w in writable):
                return PermissionResultDeny(
                    message=('You may only write %s (paths relative to the top of the '
                             'worktree). %s is off limits.' % (where, rel)))
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
     equal a frozen reference decompressor, on whole Parquet row groups and
     on synthetic chunks. A deadlock or one wrong byte rejects the candidate.
  2. Throughput = bytes per cycle (real Parquet row groups) x f_max (from
     @@SYNTH@@). Both halves count. A change that raises
     bytes/cycle a little and lowers f_max more is a loss.
  3. Area is measured and shown, not scored. You are judged on throughput
     alone; LUTs, registers and RAMs are reported so you can see what a
     change costs. The design must still place and route on the part.

THE COMMANDS YOU MAY RUN (exactly as written, nothing else)
  python agentic/check.py           compile + simulate 8 invented shapes (one
                                    a 192 KiB chunk that wraps the 64 KiB
                                    history, as real pages do) and compare
                                    every byte with the reference. Also
                                    prints bytes/cycle and a stage profile on
                                    invented data (about 10 s).
                                    THE LOOP HAS ALREADY RUN THIS FOR YOU on
                                    the design you start from, and its output
                                    is in your brief. Running it again before
                                    you have edited anything tells you what
                                    you were already told, so do not: your
                                    first check comes after your first edit.
  python agentic/check.py --quick   the first three shapes.
  python agentic/packmodel.py [--k K] [--l L] [--n N] [--hazard window|none|strict16]
                              [--litrate B] [--split on|off] [--rules ideal|dsw4]
                                    the packing model. It replays the Snappy
                                    elements of the real scored pages through
                                    an ideal packer that issues K elements
                                    (literals or copies) per cycle into an
                                    L-byte output line from N input bytes per
                                    cycle, calibrated to the current best
                                    design's measured bytes/cycle. It prints
                                    the visible table and one geomean for the
                                    held-out tables. With no arguments: the
                                    current widths and the standard what-ifs.
                                    A new setting takes a few minutes; repeated
                                    ones are instant. Run it before building a
                                    widening, to see whether that widening can
                                    pay on its own or only together with others.
  Read-only git, in these forms only (no other flags, no pipes or redirects;
  paths are relative, under rtl/ or docs/):
@@GITFORMS@@
  When the history names an earlier attempt's branch,
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
  * Aim at the measured bottleneck and at real column data: whole Parquet
    row groups written with default settings, fed page after page. A page is
    one chunk. Pages run from a few hundred bytes to 15 MB, and 98% of the
    scored bytes are in pages longer than the 64 KiB history, so one chunk
    keeps one core busy for up to millions of cycles while its copies reach
    as far as 64 KiB back. About 6-7 bytes per element, 60-86% of elements
    are copies, overlapping copies in sequential integers. Do not tune to
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

  If your assignment names a roadmap step and you can show that step is
  empty or wrong for this design (the probe, the packing model or the RTL
  shows it cannot move bytes/cycle), write
  {"id": "none", "roadmap_step": "<id>",
   "dispute": "the proof: what you ran or read, with numbers and file:line",
   "rationale": "what you would build instead"}
  and leave rtl/ as you found it. The planner reads your proof; two proofs
  drop the step.
"""
# The git forms are written once (GIT_FORMS_TEXT, beside the gate that
# enforces them), so the brief can never promise a form the gate refuses.
SYSTEM_BRIEF = SYSTEM_BRIEF.replace('@@GITFORMS@@', GIT_FORMS_TEXT)


RESUME_PREFIX = """\
YOU HAVE ALREADY STARTED THIS CHANGE IN THIS WORKTREE.

The provider's usage window ran out in the middle of your last session and the
loop waited for it to reset. Nothing was thrown away: your edits are still in
rtl/, and NOTES.md is there if you wrote one.

Start with `git diff -- rtl` to see what you changed and `python agentic/check.py` to
see whether it still passes. If it fails and the cause is not obvious within a
few minutes, run `git checkout -- rtl` and rebuild the change more simply. Then
finish it and write NOTES.md and PROPOSAL.json.

Your assignment has not changed; it is repeated below.

"""

# A design session's work is in docs/, not rtl/: the generic prefix would send
# it to `git diff -- rtl` and find nothing.
DESIGN_RESUME_PREFIX = """\
YOU HAVE ALREADY STARTED THIS DESIGN IN THIS WORKTREE.

The provider's usage window ran out in the middle of your last session and the
loop waited for it to reset. Nothing was thrown away: your work is in
docs/track-@@ID@@/ (SPEC.md, steps.json), not in rtl/, and NOTES.md is there if
you wrote one. Read what you wrote, finish the spec and the step list, then
write NOTES.md and PROPOSAL.json.

Your assignment has not changed; it is repeated below.

"""


# ---------------------------------------------------------------------------
# Build tracks: an architecture change too big for one session, built step by
# step on its own branch. The first step designs it; each later step builds
# one module plus its unit test.

TRACK_BRIEF = """\

THIS SESSION IS ONE STEP OF A BUILD TRACK
  This brief replaces "one change, fully carried through" with: build ONE
  module (or one interface change) plus its unit test, exactly as the step
  says. Track @@ID@@: @@GOAL@@. You start from the track branch @@BRANCH@@, not
  from the best design; the earlier steps are already in it.
  Your step (@@N@@ of @@M@@, kind @@KIND@@): @@TEXT@@
@@JUDGED@@
  Unit test: put it in rtl/unit/<name>.vhd (entity <name>, no ports). Run it with
    python agentic/check.py --unit <name>
  which compiles it with rtl/ and runs it in a folder holding golden data
  from invented shapes:
    cs.tv         the compressed chunks (the testbench's format)
    expected.hex  their decompressed bytes: "<16 hex digits> <count>" per 8
                  bytes (the last line of a chunk is zero-padded), then EOC
    elements.tv   every Snappy element, one per line: pos kind hdr len offset
                  (kind 0 = literal, 1 = copy), then EOC after each chunk
  It passes when the simulation ends by itself (std.env.finish) without an
  assertion of severity error or failure. rtl/unit/ is never compiled into
  the scored simulation or into synthesis.
  The whole design must still pass `python agentic/check.py` when you
  finish: if the new module is not complete yet, wire it in behind the old
  path or leave it unconnected.
  THE SPEC (docs/track-@@ID@@/SPEC.md):
@@SPEC@@
  NOTES FROM EARLIER STEPS:
@@NOTES@@
  If the spec is wrong for this step, write the correction in
  docs/track-@@ID@@/STEP-@@N@@.md and say so in PROPOSAL.json. The step's kind
  and prediction are fixed by the track; you may add "predicted_bpc" with
  your own estimate, which is logged.
"""

TRACK_JUDGED_THROUGHPUT = """\
  Predicted bytes/cycle after this step: @@PRED@@. It passes if every
  draw is byte-exact and the measured bytes/cycle is at least
  @@TOL@@% of that."""

TRACK_JUDGED_CORRECTNESS = """\
  This step is checked for correctness only: every draw
  byte-exact (or, if you change only rtl/unit/, your unit
  tests must pass)."""

DESIGN_BRIEF = """\

THIS SESSION DESIGNS A BUILD TRACK. Do not change rtl/.
  Track @@ID@@: @@GOAL@@ (why: @@WHY@@).
  Write docs/track-@@ID@@/SPEC.md: the target architecture (stages, records,
  widths, handshakes, RAMs), why it pays (run agentic/packmodel.py at the
  target widths and quote its aggregate), the interfaces between the new
  modules, and an order of building in which every step leaves a design
  that passes the check. Write docs/track-@@ID@@/steps.json:
    [{"text": "...", "kind": "golden|ports|throughput", "module": "rtl/...",
      "widths": {"K": k, "L": l, "N": n, "rules": "ideal|dsw4"} (throughput steps),
      "predicted_bpc": <number for throughput steps, else null>}]
  1 to @@MAXSTEPS@@ steps, the last of kind throughput with the
  target bytes/cycle, which must beat the current best's @@BASE@@.
  Predictions come from the packing model at the widths each step has
  reached; a step that widens one dimension alone is expected to gain
  about nothing, so predict that honestly. A prediction below 90% of the
  packing model at the declared widths is rejected.
  SPEC.md must be at least 1500 characters. The planner's first sketch of
  the steps, which you may change:
@@PLANNED@@
  When you are done, PROPOSAL.json is {"id": "track-@@ID@@-design",
  "rationale": "..."}; NOTES.md as usual.
"""


def _fill(text, values):
    for key, val in values.items():
        text = text.replace('@@%s@@' % key, str(val))
    return text


def track_system_text(track):
    """TRACK_BRIEF or DESIGN_BRIEF for one track session, filled in from
    direction['track'] (what loop.track_direction put there)."""
    tid = track.get('id', '')
    if track.get('kind') == 'design':
        planned = '\n'.join('    %d. [%s] %s%s' % (
            j, s.get('kind'), (s.get('text') or '')[:300],
            (' (predicted %.2f B/cycle)' % s['predicted_bpc'])
            if isinstance(s.get('predicted_bpc'), (int, float)) else '')
            for j, s in enumerate(track.get('planned') or [], 1)) or '    (none)'
        return _fill(DESIGN_BRIEF, {
            'ID': tid, 'GOAL': track.get('goal', ''), 'WHY': track.get('why', ''),
            'MAXSTEPS': int(CONFIG.get('track_max_steps', 6)),
            'BASE': '%.2f B/cycle' % (track.get('base_bpc') or 0), 'PLANNED': planned})
    if track.get('kind') == 'throughput':
        judged = _fill(TRACK_JUDGED_THROUGHPUT, {
            'PRED': ('%.2f' % track['predicted_bpc'])
            if isinstance(track.get('predicted_bpc'), (int, float)) else 'not given',
            'TOL': '%.0f' % (100.0 - float(CONFIG.get('track_bpc_tolerance_pct', 10.0)))})
    else:
        judged = TRACK_JUDGED_CORRECTNESS
    indent = lambda text: '\n'.join('    ' + ln for ln in (text or '(none)').splitlines())
    return _fill(TRACK_BRIEF, {
        'ID': tid, 'GOAL': track.get('goal', ''), 'BRANCH': track.get('branch', ''),
        'N': track.get('n'), 'M': track.get('m'), 'KIND': track.get('kind'),
        'TEXT': track.get('text', ''), 'JUDGED': judged,
        'SPEC': indent(track.get('spec')), 'NOTES': indent(track.get('prev_notes'))})


def build_user_prompt(ctx, direction, others, resumed=False):
    """The measured state plus this candidate's assignment."""
    lines = []
    track = direction.get('track') or None
    if resumed and track and track.get('kind') == 'design':
        lines.append(DESIGN_RESUME_PREFIX.replace('@@ID@@', track.get('id', '')))
    elif resumed:
        lines.append(RESUME_PREFIX)
    lines.append('GOAL: %s' % ctx['goal_text'])
    lines.append('ITERATION %d of %d. Best design so far vs the original: %s'
                 % (ctx['iteration'], ctx['max_iters'], ctx['progress_text']))
    lines.append('')
    if ctx.get('roadmap_text'):
        lines.append('ROADMAP FROM THE ENGINEER (advice ranked by modelled gain, not orders.')
        lines.append('If your assignment names a roadmap step and you find that step cannot')
        lines.append('move bytes/cycle on this design, decline with a proof: see WHEN YOU ARE '
                     'DONE.)')
        lines.append(ctx['roadmap_text'])
        lines.append('')
    lines.append('CURRENT DESIGN, MEASURED')
    lines.append(ctx['state_text'])
    lines.append('')
    lines.append('STAGE PROFILE (rate vs ceiling read from the RTL)')
    lines.append(ctx['profile_text'])
    lines.append('')
    if ctx.get('packmodel_text'):
        lines.append(PACKMODEL_HEADER)
        lines.append(ctx['packmodel_text'])
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
    if direction.get('roadmap_step'):
        lines.append('  roadmap:    STEP %s' % direction['roadmap_step'])
    if track:
        lines.append('  track:      %s, step %s of %s (%s); the step is in the system '
                     'brief' % (track.get('id'), track.get('n'), track.get('m'),
                                track.get('kind')))
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


PACKMODEL_HEADER = ('PACKING MODEL (bytes/cycle the real pages allow at other widths; '
                    'aggregate only)')

TRACKS_HELP = """\
A track is for an architecture change that the packing model says pays only
when several parts change together, and that one session cannot build and
verify. Its first step is a design session that writes
docs/track-<id>/SPEC.md and the step list. Each later step builds one module
plus its unit test and must be byte-exact on every draw; a throughput step
must also reach 90% of its predicted bytes/cycle (golden-model and port steps
are checked for correctness only). Steps are judged against their own
prediction, never adopted on their own; only the finished track is compared
with the best, on throughput. At most one track is open. Always list N
directions, best first: while a track is open its step takes the last slot.
To open one, add "open_track"; to give up an open one, add
"abandon_track": "reason"."""


def build_plan_prompt(ctx, n):
    return """\
GOAL: %s
ITERATION %d of %d. Progress so far: %s

CURRENT DESIGN, MEASURED
%s

STAGE PROFILE
%s

LEVER: %s -- %s

%s
%s

ROADMAP FROM THE ENGINEER (advice ranked by modelled gain, not orders)
%s

TRACKS (multi-step builds)
%s
%s

SKILL LIBRARY
%s

FACTS SESSIONS HAVE VERIFIED ABOUT THIS DESIGN
%s

HISTORY (most recent last)
%s

Choose %d DIFFERENT directions for %d parallel candidate sessions. Rules:
- The ROADMAP is the engineer's advice, ranked by modelled gain, not an
  order. Prefer its highest-ranked OPEN step when the profile and the
  packing model agree it moves the binding limit. A DISPUTED step comes
  with a session's proof that it is empty or wrong: assign it again only
  if you say what that proof missed. DROPPED steps must not be assigned.
  The skill library's AVOID marks hold for roadmap steps too. When a
  direction carries out a roadmap step, put the step id in roadmap_step.
- Each direction targets the measured bottleneck or a credible second one;
  never an idle stage. Prefer high-confidence skills that fit the profile.
- One direction should raise the per-cycle capacity of the stage the probe
  names as the limit (a structural change: more elements per cycle, a
  wider line or port). Check it with the PACKING MODEL first: if that
  widening alone models under +3%% because another limit takes over, do not
  assign it alone. Assign the combination the model says pays, or open a
  TRACK when the combination is more than one session can build. Timing
  tweaks are for the other directions. Spell out the steps: which records,
  which stages, which files, in what order.
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
   "risk": "what could go wrong",
   "roadmap_step": "the roadmap step id this direction carries out, or null",
   "modelled_gain_pct": <the packing model's bytes/cycle gain for it, or null>}
 ],
 "note": "one or two sentences on the overall state",
 "open_track": (optional; only when no track is open)
   {"id": "kebab-case-name", "goal": "the target architecture in one line",
    "why": "what the packing model says it pays, and why one session cannot build it",
    "steps": [{"text": "...", "kind": "golden|ports|throughput",
               "predicted_bpc": <number or null>}]},
 "abandon_track": (optional) "why the open track should stop"}
""" % (ctx['goal_text'], ctx['iteration'], ctx['max_iters'], ctx['progress_text'],
       ctx['state_text'], ctx['profile_text'], ctx['lever']['lever'],
       ctx['lever']['reason'], PACKMODEL_HEADER,
       ctx.get('packmodel_text') or '(not available)',
       ctx.get('roadmap_text') or '(none)',
       ctx.get('tracks_text') or 'none', TRACKS_HELP, ctx['skills_text'],
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


def norm_step_id(value):
    """A roadmap step id as every part of the loop compares it: 'STEP 2',
    '2' and 2 are the same id. At most 20 characters; '' for none."""
    if value is None or isinstance(value, bool):
        return ''
    text = str(value).strip()
    text = re.sub(r'^step\b', '', text, flags=re.I).strip().rstrip(':,').strip()
    if text.lower() in ('none', 'null'):
        return ''
    return text[:20]


def _float_or_none(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) < 1e6 else None


TRACK_KINDS = ('golden', 'ports', 'throughput')
# A passed step gets the ref <track branch>-s<n>; an id ending like that could
# collide with another track's step ref.
TRACK_ID_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
_STEP_SUFFIX = re.compile(r'-s\d+$')


def sanitize_track_steps(steps, max_steps):
    """[{'text', 'kind', 'predicted_bpc', 'widths'}] from a planner's or a
    design session's step list, or (None, reason). The last step must be a
    throughput step: only it is compared with the best."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
        return None, 'needs 1 to %d steps' % max_steps
    out = []
    for j, st in enumerate(steps, 1):
        if not isinstance(st, dict) or not str(st.get('text') or '').strip():
            return None, 'step %d has no text' % j
        kind = str(st.get('kind') or '').strip().lower()
        if kind not in TRACK_KINDS:
            return None, 'step %d has kind %r (not golden, ports or throughput)' % (
                j, st.get('kind'))
        pred = _float_or_none(st.get('predicted_bpc'))
        if pred is not None and pred <= 0:
            pred = None
        widths = st.get('widths') if isinstance(st.get('widths'), dict) else None
        out.append({'text': str(st['text']).strip()[:1200], 'kind': kind,
                    'predicted_bpc': pred, 'widths': widths,
                    'module': str(st.get('module') or '')[:200]})
    if out[-1]['kind'] != 'throughput':
        return None, 'the last step must be of kind throughput'
    return out, ''


def sanitize_track_request(req, max_steps=None):
    """An 'open_track' request from the planner, made safe, or (None, why).
    The id is kebab-case, at most 40 characters, and never ends in -s<digits>;
    the loop makes it unique against the run's tracks."""
    max_steps = int(max_steps or CONFIG.get('track_max_steps', 6))
    if not isinstance(req, dict):
        return None, 'open_track is not an object'
    tid = re.sub(r'[^a-z0-9-]+', '-', str(req.get('id') or '').lower()).strip('-')
    tid = re.sub(r'-+', '-', tid)[:40].strip('-')
    while _STEP_SUFFIX.search(tid):
        tid = _STEP_SUFFIX.sub('', tid)
    if not tid or not TRACK_ID_RE.match(tid):
        return None, 'open_track has no usable id'
    steps, why = sanitize_track_steps(req.get('steps'), max_steps)
    if steps is None:
        return None, 'open_track %s: %s' % (tid, why)
    return {'id': tid, 'goal': str(req.get('goal') or '').strip()[:400],
            'why': str(req.get('why') or '').strip()[:800], 'steps': steps}, ''


def track_command(data):
    """{'open': request} / {'abandon': reason} / {} from a plan reply."""
    out = {}
    if not isinstance(data, dict):
        return out
    if isinstance(data.get('abandon_track'), str) and data['abandon_track'].strip():
        out['abandon'] = data['abandon_track'].strip()[:400]
    if isinstance(data.get('open_track'), dict):
        out['open'] = data['open_track']
    return out


FAKE_TRACK = {'id': 'fake-wide', 'goal': 'a scripted track for testing the loop',
              'why': 'scripted (--fake): exercises the track path, models nothing',
              'steps': [{'text': 'golden model of the new element format', 'kind': 'golden',
                         'predicted_bpc': None},
                        {'text': 'the widened back end', 'kind': 'throughput',
                         'predicted_bpc': None}]}


def fake_plan_directions(n, iteration=None, track_open=False):
    """The fallback directions, with no model call: what --fake plans with,
    so testing the loop never spends anything. On iteration 1, with no track
    open, it also opens a scripted track, so the track path is exercised."""
    dirs = [dict(FALLBACK_DIRECTIONS[j % len(FALLBACK_DIRECTIONS)], roadmap_step=None,
                 modelled_gain_pct=None) for j in range(n)]
    cmd = {}
    if iteration == 1 and not track_open and CONFIG.get('tracks_enabled', True):
        cmd = {'open': json.loads(json.dumps(FAKE_TRACK))}
    return dirs, {'status': 'fake', 'text': 'scripted directions (--fake)',
                  'cost_usd': 0.0}, cmd


def plan_directions(ctx, n, log, track_open=False):
    """N directions for this iteration, plus the planner's track command
    ({'open': request} or {'abandon': reason} or {}). Falls back to fixed
    directions on failure. Always n directions, best first: while a track
    is open, its step takes the last slot, and that direction runs whenever
    the track slot runs no session."""
    res = ask(build_plan_prompt(ctx, n), system=PLAN_SYSTEM,
              model=CONFIG['helper_model'], max_turns=1, budget=2.0,
              timeout_s=600, effort='medium')
    data = extract_json(res.get('text', '')) if res.get('status') == 'ok' else None
    cmd = track_command(data)
    if track_open:
        cmd.pop('open', None)
    elif 'abandon' in cmd:
        cmd.pop('abandon')
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
                             'risk': str(d.get('risk', ''))[:300],
                             'roadmap_step': norm_step_id(d.get('roadmap_step')) or None,
                             'modelled_gain_pct': _float_or_none(d.get('modelled_gain_pct'))})
        if data.get('note'):
            log('  planner: %s' % str(data['note'])[:300])
    if res.get('status') != 'ok':
        log('  planner call failed (%s): %s' % (res.get('status'), res.get('error', '')[:200]))
    while len(dirs) < n:
        dirs.append(dict(FALLBACK_DIRECTIONS[len(dirs) % len(FALLBACK_DIRECTIONS)]))
    return dirs[:n], res, cmd


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
        # A decline that disputes the roadmap step the session was given.
        'roadmap_step': norm_step_id(data.get('roadmap_step')) or None,
        'dispute': str(data.get('dispute') or '')[:2000],
        # A track step's own view of its kind and bytes/cycle: logged, never
        # used to judge (the loop's step list fixes both).
        'step_kind': str(data.get('step_kind') or '')[:20] or None,
        'predicted_bpc': _float_or_none(data.get('predicted_bpc')),
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
            # A track session runs on a larger budget for longer; its rate
            # would skew the time a normal session is told it has.
            if cand.get('track'):
                continue
            if (sess.get('seconds') or 0) > 60:
                rates.append(sess['cost_usd'] / (sess['seconds'] / 60.0))
    if not rates:
        return None
    rates.sort()
    return rates[len(rates) // 2]


def session_limits(track, ctx):
    """(budget in dollars, timeout in minutes, calibration commit) for one
    session. A track step builds a module and its unit test, which does not
    fit a normal session; its packing model stays calibrated to the design
    the track opened on, so its step predictions keep their meaning."""
    if track:
        return (float(CONFIG.get('track_session_budget_usd', 40.0)),
                int(CONFIG.get('track_session_timeout_min', 120)),
                track.get('calib_commit') or ctx.get('best_commit'))
    return (float(CONFIG['session_budget_usd']), int(CONFIG['session_timeout_min']),
            ctx.get('best_commit'))


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

        # Bring the worktree's harness up to the current kit (check.py,
        # packmodel.py, ...). The manifest sits next to the worktree, outside
        # it, and is what this session's gate checks agentic/ against.
        manifest = asg.get('harness_manifest') or (
            os.path.normpath(asg['worktree']) + '.harness.json')
        try:
            sync_harness(asg['worktree'], manifest)
        except Exception as exc:
            log('  %s: could not bring the harness up to date (%s); the session '
                'may find its check refused' % (label, str(exc)[:160]))

        track = asg['direction'].get('track') or None
        budget, timeout_min, calib = session_limits(track, ctx)
        system = SYSTEM_BRIEF.replace(
            '@@BUDGET@@', 'about $%.0f and %d minutes' % (budget, timeout_min)).replace(
            '@@SYNTH@@', synth_phrase())
        writable = WRITABLE
        if track:
            # The track's own brief after the general one, and its own write
            # list: the design step may write only the track's docs.
            system += track_system_text(track)
            writable = track_writable(track.get('id', ''), track.get('kind'))
        jobs.append(dict(
            prompt=build_user_prompt(ctx, asg['direction'], others,
                                     resumed=bool(asg.get('resumed'))),
            meter=True, burn_rate=None if track else rate,
            system={'type': 'preset', 'preset': 'claude_code', 'append': system},
            cwd=asg['worktree'], model=CONFIG['model'],
            max_turns=int(CONFIG['session_max_turns']),
            budget=budget,
            tools=('Read', 'Edit', 'Write', 'Glob', 'Grep', 'Bash'),
            # Edit/Write/Bash are left out of the auto-approve list so every
            # call reaches the gate (an allowed tool never does).
            allowed=('Read', 'Glob', 'Grep'),
            gate=make_gate(asg['worktree'], manifest, writable),
            timeout_s=timeout_min * 60,
            effort=CONFIG.get('effort') or None,
            on_text=on_text, restricted=True,
            # The packing model calibrated to the design this session starts
            # from (a track's: the best when it opened); AGENTIC_RUN comes
            # with the loop's own environment.
            env_extra=({'AGENTIC_CALIB': calib} if calib else None)))

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
