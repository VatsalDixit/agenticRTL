# Loop v3: change plan (revised after review)

Branch `hacc-realdata`, kit in `agentic/`. This is a plan only. No code was
changed and the loop was not started. Nothing was run except reading files, one
`git check-ignore`, loading `diag-i35/diag/els.pkl` once to read its
calibration ratios, and (for the revision) a few greps and one `diff -q` of
the simulation RAM file between i35 and DSW-4. Line numbers refer to the files
at `76944da`.

The six approved changes, in the user's numbering:
1. **Area:** no area limit and no area term in the score. Throughput alone
   decides. Area is still measured and shown. **No throughput-per-LUT figure
   anywhere.**
2. **Probe:** a generic internal probe in every measurement.
3. **Packing model:** a new `agentic/packmodel.py` that sessions and the
   planner can run.
4. **Build tracks:** multi-step architecture changes on their own branch.
5. **Step sizing:** bigger budgets for track steps, and one module plus its
   unit test per step.
6. **Roadmap rules:** the roadmap becomes advice ranked by modelled gain, with
   disputes and automatic drops.

Section 9 lists every review point and what was done with it.

**This document is itself kept free of per-table held-out numbers**, because
`docs/` is readable in session worktrees (and through `git show`) once it is
committed. Per-table values are named only by where they live.

---------------------------------------------------------------------------

## 0. Facts found while planning that shape the design

**F1. Sessions run the harness committed at the start of the run, not the
current kit.**
- A candidate branch starts from the base worktree's HEAD, and adoptions change
  only `rtl/`.
- So `agentic/check.py`, `measure.py` and the rest in a session worktree are
  frozen at the run's `base_commit`. hacc-real200's base `measure.py` already
  differs from the kit's.
- Without a fix, a new `packmodel.py`, the probe and `check.py --unit` would
  never reach sessions in a resumed run.
- Fix: a harness sync of an allowlisted set of kit files (section 3.4), checked
  against hashes recorded at sync time.

**F2. How the packing model's calibration really works.**
- `ideal_par.py` stores, per table, `ratio = measured_i35 / model.run(i35
  rules)`. In `els.pkl` the ratio is 0.928 for taxi and between 0.88 and 0.97
  for the six held-out tables (per-table values: `els.pkl`, not repeated here).
- It then multiplies the ideal packer's result at the what-if widths by that
  ratio.
- The per-table measured values in `ideal_par.TABLES` equal the per-draw
  bytes/cycle of hacc-real200's best (i35) in state.json.
- So the known numbers (K2/L16/N8 8.99, K3/L32/N32 16.00, K4/L32/N32 19.28,
  from `diag/ideal2.txt`) come from calibrating against the i35-exact rule
  model and predicting with the ideal packer.
- Calibrating against the ideal packer at K2/L16/N8 instead gives a ratio of
  about 1.08 and misses the targets by about 8%. So packmodel.py keeps the
  exact family models and records which one calibrated.
- The ideal packer's K already counts elements of **any kind** per cycle
  (`ideal_par.py:31`, `took` counts literals and copies). The ambiguity is only
  in mapping a design to K (section 3.3).

**F3. Dropping the area gate causes no burst of retries in hacc-real200.**
- The `too_expensive` records with at least 5% gain are already in
  `state['retried']`: i1 `sixteen-byte-core-line` (+11.3%) and i9
  `thirtytwo-byte-line...` (+5.8%).
- The other two (i12 +1.0%, i26 +2.2% throughput) are below
  `RETRY_MIN_GAIN_PCT`.

**F4. A unit-test subfolder is invisible to both simulation and synthesis.**
- `syn/sim_draws.sh` compiles only `$RTL/*.vhd`, not subfolders.
- `hacc.py:142` lists only top-level `rtl/*.vhd` and skips `*.sim.vhd`.
- So unit tests in `rtl/unit/*.vhd` are ignored by both, and they are already
  writable under the existing `rtl/` gate rule.

**F5. Simulation folders are on the Windows file system.** Probe output file
names must not contain `:`, which `'path_name` produces, and must stay well
under 255 characters.

**F6. `--fake` still calls the paid planner model** (`plan_directions` at
loop.py:1149). The offline dry runs need a scripted planner too (section 7.3).

**F7. A read-only probe left the score unchanged in diag's run.** `diag/run.log`
(the i35 probe run on train-taxi and three held-out draws) printed 8.6843
bytes/cycle for taxi, and all four draws equal the scored values in state.json
to four decimals. That was diag's own probe and testbench, not this
instrumenter, so the plan checks the instrumenter itself (2.6, 2.4).

**F8. Test coverage today.**
- test_kit.py has 26 `test_*` functions and 51 checks. The measure map's
  baseline run gave 51 ok and 1 skip; I did not run it myself.
- No test imports propose.py, so the gate, prompt and `read_proposal` checks
  below are the first.

**F9. Frozen files.** `freeze.FROZEN` includes `syn/sim_draws.sh`, and
`preflight()` refuses to start when a frozen file changed. The plan does not
edit any frozen file except deliberately adding `probe.py` to the list (2.2).

**F10. The session git rule leaks.** `propose.py:417` allows
`^git (show|diff|log|status)\b[^|;&><`$]*$`. A reviewer confirmed with git 2.53
that `git diff --no-index <abs path> x` prints any file on disk (state.json,
the packmodel cache) and `--output=<path>` writes any file (the kit). Section
3.4 replaces the rule; the packing model's isolation depends on it.

**F11. Several save points.** Besides `append_record` (:1105), `main()`'s
`KeyboardInterrupt` and `except Exception` handlers (:1598-1607) save whatever
is in memory. Any per-iteration counter written into `state` before
`append_record` would be saved by an interrupt and then counted again when the
iteration is redone.

**F12. `analyse.rtl_widths` defaults silently.** `core_line_bytes` (8.0) and
`copy_slots` keep their defaults when the regexes miss (`analyse.py:95-146`);
only port widths go to `unresolved`. DSW-4 declares `K_SLOTS`/`LINE_B` in
`vhsnunzip_dsw4_pkg.vhd`, so it would read as K2/L8.

**F13. The simulation RAM model is the same file in i35 and DSW-4**
(`diff -q` printed nothing), so a RAM-file veto does not block DSW-4-style
designs.

---------------------------------------------------------------------------

## 1. Change 1: throughput only; area shown, not scored

### Code

**loop.py**
- **:20, docstring.** Replace "Score = -gain% + 0.15 x area growth%" with
  "Score = -gain% on the goal metric. Area is measured and reported, not
  scored."
- **:401-409, `evaluate`.**
  - Delete the area-pricing comment and set `score = -gain`.
  - The new comment says why: the user wants single-stream throughput, area
    does not limit one decompressor on the U55C, and the old floor rejected
    exactly the widening the goal needs. DSW-4 reached +228.7% with about 8x
    the LUTs.
- **:428-434.** Delete `ei`, `meas['efficiency']` and the `too_expensive`
  branch.
- **Kept so old records still read:**
  - `area_gain_pct` (:382) and `ABSOLUTE_KEYS` (:72);
  - the LUT, register, BRAM and URAM line in `state_text` (:465-469);
  - area in `progress_text` (:564);
  - the `'too_expensive'` strings at :237, :549 and :1029 (old records).
- **Old area-era records are relabelled where the planner reads them.**
  - `history_text` prints an old `too_expensive` as `too_expensive under the
    area rule removed in v3; on throughput alone it was +x%` (x is the record's
    `measured.gain_pct`, which was always the throughput gain).
  - The skills section of the planner prompt and session brief gets one fixed
    line before the skills: "Skill notes that mention efficiency, an EI floor or
    too_expensive were written under the area rule removed in v3; judge those
    ideas on throughput alone." The notes themselves are not rewritten.

**tools.py:89-95.** Remove `area_weight`, `max_area_growth_pct` and `ei_floor`.
The new comment reads: "Scoring: throughput (the goal metric) alone; area is
measured and shown, never scored."

**config.json.** Remove the same three keys. `load_config` ignores unknown
keys, so an old config.json still loads.

**propose.py:516-517, SYSTEM_BRIEF.** Replace item 3 with:
```
  3. Area is measured and shown, not scored. You are judged on throughput
     alone; LUTs, registers and RAMs are reported so you can see what a
     change costs. The design must still place and route on the part.
```
`synth_phrase()` (:463-469) keeps LUTs as a measured quantity.

**README.md:52 and :230-236.** Change the mermaid "select" node to
`score = -gain%`, and replace the "Area is priced" paragraph with the rule
above.

No per-LUT figure appears anywhere: not in `evaluate`, the prompts, report.py,
gui.py or packmodel.

### Tests (test_kit.py)
- `test_area_is_not_scored`: +5% throughput at +91% area (3577 to 6833 LUTs)
  gives outcome `candidate`, adoptable, and `score == -gain_pct` exactly.
- `test_huge_area_small_gain_still_adoptable`: +0.5% at +700% area is
  adoptable.
- `test_no_efficiency_or_per_lut_key`:
  - the keys of `measured` are exactly `{throughput_gain_pct, bpc_gain_pct,
    fmax_gain_pct, area_gain_pct, gain_pct}`;
  - a source grep finds no `efficiency`, `per_lut`, `per LUT` or `ei_floor`
    in loop.py, propose.py, report.py, gui.py, learn.py, analyse.py,
    probe.py or packmodel.py (the relabel line says "EI floor", which is
    allowed by an explicit exception in the grep);
  - loop.py never assigns the outcome `too_expensive`.
- `test_old_area_keys_are_inert`: setting `CONFIG['area_weight']=9` leaves the
  score unchanged.
- `test_area_era_records_are_relabelled`: `history_text` on a copy of
  hacc-real200's i1 record says "area rule removed in v3" and "+11.3%"; the
  plan prompt built from a copy of the run's skills.json contains the fixed
  relabel line.
- The existing `test_place_and_route_noise_floor`,
  `test_backends_are_never_compared` and `test_a_retry_is_never_retried`
  (whose fixture uses `too_expensive`) must still pass unchanged.

---------------------------------------------------------------------------

## 2. Change 2: a generic internal probe

### 2.1 New module `agentic/probe.py`

It only reads the RTL and never writes into `rtl_dir`. Its API:
```python
def find_handshakes(rtl_dir, top='vhsnunzip_unbuffered'):
    """-> {'records': {type_name: valid_field_type},
           'arches': [{'file','entity','arch','clk','reset','reset_active','end_offset',
                       'pairs': [{'name','valid','ready','style':'ready'|'pop','src':'record'|'flat'}],
                       'occupancy': [{'name','valid'}], 'skipped': [(name, why)]}]}"""
def instrument(rtl_dir, out_dir, window=4096, exclude=())
    # copies rtl_dir byte for byte, inserts one probe process per architecture with
    # handshakes (minus `exclude` architectures); returns
    # {'arches','pairs','occupancy','skipped'} or None when nothing was found
def read(draw_dir)        # -> {instance_path: {'cycles','hs':{name:[moved,blocked,empty]},'windows','names'}}
def summarise(probe, keep_windows=False)
def combine(probes)       # cycle-weighted fractions over the scored draws
```

**Which records count.**
- Parse every `type X is record` in `*_pkg.vhd` files and in architecture
  declarations.
- A record qualifies if it has a field `valid : std_logic` (or `boolean`).
- An array of such records is probed through `A(A'low)` and labelled `A(0)`.

**Which names are visible in an architecture.**
- Its depth-0 signals (between `is` and `begin`) plus its entity's ports.
- Signals local to a generate or block are skipped, and the reason is
  recorded.

**Types are checked per name.** The declared type of every name used is read.
A flat valid must be scalar `std_logic` or `boolean`; a ready or pop must be
scalar `std_logic` or `boolean`; anything else (`std_logic_vector`, a
subtype the parser cannot resolve, an alias) is skipped with the reason. One
odd pair never breaks the copy.

**What counts as a handshake.**
- A **pair** is a record X with an `X_ready` or `X_pop`, or an `X_valid` with
  an `X_ready` or `X_pop`. Moved = valid and ready; blocked = valid and not
  ready; empty = not valid.
- **Occupancy** is a valid with no ready partner. It counts only valid and
  not-valid, and is labelled "no ready".

**Each net is counted once.**
- A pair is probed in an architecture only if at least one of its two names
  is a local signal there. This stops leaf entities re-counting their parent's
  nets.
- A pair where both names are ports is counted only in the top entity,
  `vhsnunzip_unbuffered`, which gives co and de.

**Clock and reset.**
- Clock: `clk` or `clock`. If neither exists, the architecture is skipped.
- Reset: `reset` or `rst` (active high), or `reset_n`, `rst_n` or `aresetn`
  (active low). With none, counting runs on every cycle.

**The inserted probe process.**
- It goes before the architecture's final `end`, inside
  `-- pragma translate_off/on`, with the label `agentic_probe_p`.
- Every identifier it declares starts with `agentic_probe_`, so it cannot
  clash with a design name.
- It has no `use` clause. Every textio name is fully qualified
  (`std.textio.line`, `std.textio.write`, and so on).
- It reads signals and drives none.
- Every `file_open` uses the status form; on any status other than
  `open_ok` the probe stops writing and the simulation goes on.

**What each probe writes.**
- `agentic_probe.<path>.tot`: totals. The path comes from `'path_name`, with
  every character outside `[A-Za-z0-9_]` replaced by `.` (F5); a path longer
  than 150 characters is shortened to its first 100 characters plus a hash of
  the whole.
- The totals file is **rewritten on an adaptive schedule**: at cycle 256, then
  whenever the cycles since the last write reach `clamp(256, cycles/16,
  65536)`. This bounds the uncounted tail to at most 1/16 of the run (it only
  reads fractions), captures short draws, and costs about 160 open/close calls
  on a 3.5M-cycle draw instead of 13,700.
- `agentic_probe.<path>.win`: one header line, then one `W idx m b e ...` line
  per 4096-cycle window. **Windows are written only when a marker file
  `agentic_probe.windows` exists in the draw folder at time 0.** measure.py
  creates it only in visible draw folders, so no window data is ever written
  for held-out draws. (The testbench is frozen, so a generic cannot do this.)
- Each instance writes its own files (NUM_CORES copies, the three fifos), and
  GHDL is single-threaded, so files never interleave.

### 2.2 measure.py, freeze.py

**Instrumenting once per measurement.**
- `measure()` calls `prepare_probe(rtl_dir, build_dir)` once and passes the
  result to both of its `simulate` calls (quick draws, then slow draws), so one
  half is never probed and the other not.
- `simulate(..., probe_rtl=None)` called directly (as check.py does) prepares
  its own, so sessions see the probe on invented data too.
- `prepare_probe`:
  1. If `CONFIG.get('probe_enabled', True)` is false, or the run state has
     `probe_disabled` (2.4), return the plain `rtl_dir`.
  2. `probe.instrument(rtl_dir, build_dir/probe_rtl)` inside try/except. None
     or an exception returns the plain `rtl_dir` with `probe_status` saying
     why.

**Running the draws.**
1. After the private draw copies (:185-191), delete any `agentic_probe.*`
   files; create `agentic_probe.windows` in visible draw folders only.
2. `_run_draws(sim_rtl, ...)`.
3. **Fall back only on a build failure of the probe copy.** `_run_draws`
   raises a new `ProbeBuildError(MeasureError)` when the probe copy ends in
   COMPILE_FAIL or ELAB_FAIL. Then:
   - instrument once more with `exclude=` the architecture GHDL's log names,
     and run again;
   - if that also fails to build, log "probe copy did not compile; measured
     without the probe" and run the plain `rtl_dir`.
   Only the plain run's errors reach the caller as a verdict. A wall-clock
   timeout or any other `MeasureError` under the probe is **not** rerun: the
   probe is read-only and its measured overhead is small (2.6), so the timeout
   belongs to the design.
4. The rc-137 retry (:199-201) uses the same `sim_rtl`.
5. **Rerun a failed draw clean only when the probe could have caused it:** rc
   not 0, no deadlock verdict, and no `perf.txt`. Before the rerun, delete the
   draw's `agentic_probe.*` and outputs. Oracle mismatches and deadlocks are
   never rerun (a read-only probe cannot cause them), so a failing design costs
   no extra simulation.
6. Only on a pass, and only if the draw's final run was the probed one:
   `rec['probe'] = probe.summarise(probe.read(ddir))`, inside a try.
   `bytes_per_cycle`, cycles, `bytes_out` and the oracle are untouched.
7. In the cleanup (:250-254), remove `.win` files after reading; keep `.tot`.
8. `metrics['probe_status']` records one of `on`, `off`, `partial (skipped
   <arch>: <why>)` or `fallback: <why>`. The loop logs every non-`on` status
   (2.4).

**Elsewhere in measure.py.**
- **`measure()`** otherwise unchanged. Widths, synthesis and
  `ram_interface_problem` keep using `rtl_dir`.
- **`summarize()`** sets `out['probe'] = probe.combine(<scored draws'
  probes>)`: totals only, no windows, no per-draw names. The calls to
  `analyse.combine` and `analyse.lever` are unchanged in signature; their probe
  parts are guarded inside analyse.py (2.3), and the `out['probe']` line itself
  is in a try that logs and drops the probe on error.
- **`syn/sim_draws.sh` is not edited** (it is frozen, F9). Step 1 above
  already deletes the probe files from Python, and draw folders are private
  copies.

**freeze.py.** Add `probe.py` to `FROZEN`, because instrumented RTL now
produces every scored simulation. In the same commit: run `freeze.check()`
first and confirm it is empty, then `python agentic/freeze.py --record`, and
add a test that `freeze.check()` is empty after the change.

### 2.3 analyse.py

**`analyse(counters, cs_tv, widths, probe=None)`** adds
`report['probe'] = probe_verdict(probe)`.

**`probe_verdict(summary)`.** It orders handshakes by data flow, using instance
port maps and port directions, or declaration order as a fallback. Then the
first matching rule wins:
- **Limit:** the first stage whose output moves on at least 85% of cycles and
  is blocked on at most 5%, while its input is blocked on at least 10%. It
  returns `{'limit': S, 'kind': 'rate', 'text': ...}`.
- **Backpressure:** otherwise, the handshake with the largest blocked share, if
  at least 25%. Its consumer is named. Handshakes within 2 points of the
  largest are one stall seen through pass-through stages; the most downstream
  of them whose consumer is an inner stage is named (i35: co, cs and cd all
  wait 41.6%, and cd's consumer main_dec is named, not the wrapper).
- **Starved:** otherwise, the first stage whose input is empty on at least 50%
  of cycles while nothing downstream is blocked.
- **Windows (visible draws only):** the share of 4096-cycle windows in which
  each stage meets the limit rule.

**Every probe path is guarded.** `probe_verdict`, the probe part of `combine`
(:277-296) and the probe branch of `lever` each run inside try/except; on an
exception they log and return today's result as if no probe existed. A probe
bug can never become a measurement crash.

**`lever` (:299-337).** When a probe verdict exists, it replaces the "internal
latency" verdict at :328-332, which misread output-idle as bubbles on i35:
- for kind `rate`, the lever is `width`, with the reason: "<S> moves every
  cycle and nothing after it blocks; output idle is <S>'s rate, not bubbles";
- "latency" stays only when the same handshake shows at least 10% blocked and
  at least 30% empty.

**`describe` (:339-377)** prints one line per handshake (moved, blocked and
empty %) plus the verdict. check.py:74-79 prints `describe`, so sessions see
the probe on invented data.

**`rtl_widths` reports what it defaulted (F12).** It returns `defaulted`: the
keys it did not read from the RTL (`copy_slots`, `literal_slots`,
`elements_per_transfer`, `core_line_bytes`, `cores`, and the port widths it
did not find). Existing callers ignore the new key; packmodel calibration
uses it (3.5).

### 2.4 loop.py wiring (phase B)

- **:961.** Strip `probe` from saved candidate draws.
- **`adopt` (:998).** Best metrics keep the per-draw probe only for visible
  draws (without windows), plus the aggregate `metrics['probe']` and
  `metrics['probe_status']`.
- **`profile_text` (:500-522).** Taxi gets the full probe lines. Held-out
  draws get only the cycle-weighted aggregate over all scored draws: never per
  table and never windows.
- **`state_text` (:487).** Print the limiting stage when a probe exists, for
  example `limit: decoder (moves 97%, input waits 38%)`. Otherwise relabel the
  old text "output port idle".
- **`measure_one` (:874).** A crash inside measurement now returns
  `{'measure_error': 'measurement crashed: ...'}` instead of an `error` (which
  `evaluate` turned into `failed_compile`, counted against the skill). The
  existing never-measured path measures it again once (`retry_eligible` allows
  one retry per branch), so a kit bug is retried, not judged, and a
  deterministic crash stops after that one retry.
- **The probe is checked against a clean run before every adoption.** Before
  `adopt`, the winner's visible draws (taxi and the synthetic draws, a few
  minutes) are simulated once more with the probe off. Their counters
  (cycles, bytes_out, co_beats, de_beats, co_stall, de_bubble) must equal the
  probed run's exactly. On a mismatch:
  - `state['probe_disabled']` is set with the reason, and a banner line is
    logged;
  - every candidate of this iteration becomes `measure_error` (never
    measured), nothing is adopted, and the existing retry path measures them
    again clean next iteration.
  This should never fire; it makes F7 true for this instrumenter, not only
  for diag's.
- **On resume, the best gets a probe once (fixes the stall it is meant to
  break).** If `best.metrics` has no `probe` and the probe is not disabled,
  the loop simulates the best once (all scored draws, no synthesis), checks
  that every per-draw `bytes_per_cycle` equals the stored value to 1e-9, and
  merges only `probe`, `probe_status`, `analysis` and `lever` into
  `best.metrics`. On any difference it merges nothing and logs it. It is timed
  as `timing['probe_backfill_s']`.

### 2.5 Tests

Fast tests in `agentic/kitcheck_probe.py`:
- `test_probe_finds_i35_handshakes`:
  - the pipeline yields at least {cs, cd, el, c1, cm}, the top yields
    {co, de}, and `s1_cm` is occupancy;
  - no leaf entity re-counts a net;
  - runs on ROOT/rtl, and on diag-i35/rtl if present.
- `test_probe_finds_dsw4_handshakes` (skipped without dsw4/rtl):
  - pairs: csf and cef (pop style), cofifo `rd` (mixed signal and port), top
    co and de;
  - occupancy includes `el(0)`, `wcmd` and `ag`;
  - the exact sets found on the first run are frozen into the test.
- `test_probe_skips_bad_types`: a fixture with a `std_logic_vector` valid and
  a `boolean` ready is skipped with reasons; the other pairs are still found.
- `test_probe_identifiers_are_prefixed`: a fixture design with signals named
  `line`, `write`, `cycles` and `f` instruments without a declared name
  clash (every inserted declaration starts with `agentic_probe_`).
- `test_probe_never_touches_the_original`: file hashes in `rtl_dir` are
  unchanged, and the copy differs only between the translate pragmas.
- `test_probe_build_failure_falls_back`: with `_run_draws` faked, a
  `ProbeBuildError` leads to one retry with `exclude`, then the plain run; the
  plain run's results are returned and `probe_status` starts `fallback`.
- `test_probe_timeout_is_not_rerun`: a non-build `MeasureError` under the probe
  propagates; `_run_draws` was called once.
- `test_probe_rerun_rules`: rc≠0 with no `perf.txt` and no deadlock is rerun
  clean (and its stale `.tot` is deleted first); an oracle mismatch and a
  deadlock are not rerun.
- `test_probe_windows_only_with_marker`: `simulate`'s folder setup creates the
  marker only for `train-`/`synth-` draws.
- `test_probe_file_parser_and_verdict`: a synthetic i35-like `.tot` (cd blocked
  38%, el moved 97%, c1 and cm never blocked) names the decoder, and the lever
  is `width`.
- `test_probe_verdict_bug_is_harmless`: with `probe_verdict` monkeypatched to
  raise, `analyse.combine` and `lever` return today's verdict.
- `test_frozen_files_unchanged`: `freeze.check()` is empty and `probe.py` is in
  `FROZEN`.

Phase B tests (test_kit.py):
- `test_probe_text_hides_held_out`: no `held-` name and no window lines in
  `profile_text`.
- `test_probe_is_stripped_from_records`.
- `test_measure_crash_is_measure_error`.
- `test_probe_mismatch_blocks_adoption`: a faked clean re-run with one counter
  different sets `probe_disabled`, turns the iteration's candidates into
  `measure_error`, and adopts nothing.
- `test_probe_backfill_merges_only_on_equal_bpc` (faked `simulate`).

Slow test, skipped without the EDA shell:
- `test_probe_instrumented_i35_compiles`: the instrumented copy reaches
  ELAB_OK.

### 2.6 Offline check (free: simulation only)

A scratch script in the session scratchpad runs `measure.simulate` on
diag-i35/rtl and on dsw4/rtl, once with the probe off and once on. Draws:
train-taxi, synth-512B, synth-8192B and held-supplier.

Acceptance:
1. The counters (cycles, bytes_out, co_beats, de_beats, co_stall, de_bubble)
   and `bytes_per_cycle` are identical, and the oracle passes.
2. The i35 taxi verdict names the decoder.
3. DSW-4 writes `.tot` files with plausible lines; no `.win` file exists in the
   held-supplier folder.
4. Simulation-time overhead is at most 15%. If it is more, drop windows first
   (`probe_window_cycles: 0`), then raise the minimum flush interval.

The script prints only aggregates and the taxi lines; it does not print
held-supplier's per-draw numbers to the terminal.

---------------------------------------------------------------------------

## 3. Change 3: `agentic/packmodel.py`

### 3.1 Data and cache

**Pages.** The 7 scored tables come from `stim.page_chunks(t)[0]`, the same
whole row groups the draws use. They are parsed by `parse()`, ported unchanged
from `diag/model.py`. **Only the loop (and `--selfcheck`) ever parses Parquet**;
a session never does (3.4).

**Cache location (never inside a worktree).**
- `$AGENTIC_PACKMODEL_DIR` if set.
- Otherwise `<main checkout>/.agentic/packmodel/`, where the main checkout is
  found with `git rev-parse --path-format=absolute --git-common-dir`. This
  works from the base worktree and from candidate worktrees.
- `.gitignore` line 24 already covers it (checked with `git check-ignore`).

**Cache contents.**
- `pages-<key>/<table>.pkl`, where key = the first 12 hex digits of a sha256
  over the table files' sha256s. A rebuilt corpus never reuses stale pages.
- `raw-<key>.json`: raw model bytes/cycle per (rules, params, table).
  Calibration is applied only when printing, so each what-if is computed once
  ever.
- `calib-<commit>.json`: the calibration for one best commit (3.3), written by
  the loop.
- `current-<run>.json`: a pointer written by the loop: `{key, calib commit}`.
  A session's packmodel reads only this to find the page key and calibration;
  it never hashes table files (which `blind()` removed from its worktree).

**Concurrency and memory.**
- Every JSON write is atomic (temp file in the same folder, then `os.replace`
  with a short retry on Windows sharing errors). `raw-<key>.json` writes
  re-read and merge before replacing. A corrupt or unreadable JSON file is
  treated as empty and rebuilt.
- One packmodel computes at a time: a lock file (`lock`, created with
  `O_CREAT|O_EXCL`, holding pid and time; stale after 30 minutes) is held only
  while computing missing entries. A second caller waits up to 10 minutes and
  then prints the fixed "busy, try again" line. Cached answers need no lock.
- The pool has `CONFIG.get('packmodel_jobs', 3)` workers, and each task loads
  one table's pickle. (Loading the whole diag `els.pkl` in one process took
  1.43 GB, measured by a reviewer; per-table loads bound a worker to its
  table.)
- The loop runs packmodel through `tools.run` with a timeout, which kills the
  whole process tree.

**Isolation.** Sessions run with `--restricted`, so Read, Glob and Grep cannot
open the cache; the git rule that could print it is replaced (3.4, F10). Only
the hash-verified packmodel.py reads it, and it prints only aggregates.

### 3.2 Models

`ideal(chunks, K, L, N, hazard='window', litrate=None, split='off')` is ported
from `ideal_par.ideal`, with three switches. K is elements of any kind per
cycle, as in ideal_par (F2).

| switch | values | meaning |
|---|---|---|
| `hazard` | `window` | the current rule: a copy may not read bytes written in the same cycle |
| | `none` | no hazard rule (an upper bound) |
| | `strict16` | the i35 rule: an offset under 16 never pairs |
| `litrate` | 1-128 | how fast a literal's bytes stream in past the window; default `min(N, L)` |
| `split` | `off` | a long copy goes alone for `ceil(len/L)` cycles |
| | `on` | smodel's segment rule: a cut copy's remainder stays at the head and shares the next command |

The family models, used as exact calibration denominators:
- `i35(chunks)`, ported unchanged from `model.run`.
- `dsw4(chunks, K, B, N)`, ported from `work-spec/smodel.run_chunk` with its
  DEFAULT settings. Also selectable for what-ifs with `--rules dsw4`.

**Running.** A `multiprocessing.Pool` over (rules, params, table) tasks,
behind an `if __name__ == '__main__'` guard (Windows uses spawn). The loop
calls it as a subprocess.

### 3.3 Calibration and output

**Which model calibrates (stable within a design family).**
- The loop maps the best design to `(rules, K, L, N)`:
  1. `packmodel_widths` in config, **only if its `commit` equals the best
     commit** (`{"commit", "rules", "K", "L", "N"}`), so a hand override never
     carries over to the next adopted design;
  2. else i35 family (the package has `cp2_val`, and K, L and N were read, not
     defaulted): rules `i35`, (K, L, N) = (`copy_slots`, `core_line_bytes`,
     `in_bytes`), the diag convention behind the known numbers;
  3. else, if `elements_per_transfer`, `core_line_bytes` and `in_bytes` were
     all read (not in `defaulted`): rules `CONFIG['packmodel_calib_rules']`
     (default `ideal`) at K = `elements_per_transfer`;
  4. else uncalibrated: raw model numbers, labelled "uncalibrated" in every
     line.
- `ratio_t = measured_t / denom_t`, where `denom_t` is the family model (i35 or
  dsw4) when there is one, otherwise the ideal model at the design's own
  (K, L, N).
- The prediction is `ratio_t x ideal_t(what-if)`, and the geomean is taken
  over the 7 tables.
- **A calibration change is logged**, not hidden. When the best commit changes
  and the rules or ratio change, the loop logs `packing model calibration
  changed: <old rules, mean ratio> -> <new>`, with the standard what-ifs under
  both. Numbers made under different calibrations are never compared: roadmap
  modelled gains are recomputed per best commit, and a track's step predictions
  are fixed numbers made under the calibration the track recorded when it was
  opened (5.1).

**Output.** This is the only form it prints:
```
packing model, calibrated to <commit[:10]> (K2/L16/N8, i35 rules; measured/model 0.93)
K4 L32 N32 hazard=window split=off rules=ideal
  taxi (visible)                 18.2 B/cycle
  held-out tables, geomean of 6  19.5 B/cycle
  all scored tables, geomean     19.3 B/cycle   (+99% bytes/cycle vs the current best's 9.71)
```
- Held-out tables are never named or printed per table, and no page bytes are
  printed. The calibration ratio shown is the mean over all 7 tables.
- There is no f_max, area or per-LUT figure.
- **Any exception outside `--selfcheck`** prints one fixed line, `packing model
  unavailable (internal error)`, and exits 3: no traceback, no paths, no
  table names.

**With no arguments** it prints the standard set:
- the current widths;
- each of K+1, 2L and 2N alone;
- K3/L32/N32, K4/L32/N16, K4/L32/N32 and K6/L64/N32;
- `--rules dsw4` at K4/B32/N32.

**Argument limits.** K 1-16; L in {8, 16, 32, 64, 128}; N in {8, 16, 32, 64};
litrate 1-128. Anything else exits with code 2.

**Calibration source.** `AGENTIC_CALIB` (a commit, set by the loop per
session) overrides `current-<run>.json`; `AGENTIC_RUN` names the run. The
header line always shows the commit it calibrated to, so a mismatch is
visible.

**Library functions:** `prompt_summary(run, cfgs=None)`, `predict(run, cfg,
calib=None)`, `write_calibration(run, best_metrics, commit, widths)`,
`write_pointer(run, key, commit)` and `selfcheck()`.

**Per-table values never appear in kit source.** `selfcheck()` and the slow
test read the measured per-table values at run time from hacc-real200's
state.json (or a calib file); no held-out number is hard-coded in
packmodel.py, the kitchecks or test_kit.py.

### 3.4 Gate, harness sync and session access (propose.py)

**Packmodel gate.** Add one strict regex:
```python
PACKMODEL_RE = re.compile(
    r'^python3? agentic/packmodel\.py'
    r'(?: --(?:k|l|n|litrate) \d{1,3}| --(?:hazard|split|rules) [a-z0-9]{1,10})*$')
```
- At :423 the test becomes `cmd not in PERMITTED_COMMANDS and not
  PACKMODEL_RE.match(cmd)` and not a git form below → deny.
- The deny text (:424-428) lists the packmodel and git forms.

**Git gate (F10).** Replace the rule at :417 with explicit forms. Tokens:
- `REV` = `[A-Za-z0-9_][A-Za-z0-9_/~^-]*(?:\.[A-Za-z0-9_/~^-]+)*` (no leading
  `-` or `/`, never two dots in a row);
- `RANGE` = `REV(?:\.\.REV)?`;
- `PATH` = `(?:rtl|docs)(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*/?` (relative, under
  `rtl/` or `docs/`, no segment starting with `.`).

Allowed:
```
git status [--porcelain|--short]
git show REV:PATH
git show REV --stat
git show REV -- PATH [PATH...]
git diff [--stat|--name-only] [RANGE] -- PATH [PATH...]
git log [--oneline|--stat|-n N]... [RANGE]
git log -p [--oneline|-n N]... [RANGE] -- PATH [PATH...]
```
- A pathspec is required wherever file contents are printed, so a diff of
  `agentic/` (kit or data) is never shown.
- Everything else is denied, including `--no-index`, `--output`,
  `--ext-diff`, `--textconv`, `-a`/`--text`, `-C`, `-c`, absolute paths and
  `..` path segments: none of them can match the grammar.
- The SYSTEM_BRIEF's command section lists the allowed git forms.

**Harness sync (an allowlist).** A new `sync_harness(worktree, manifest_path)`
copies only these kit files into `worktree/agentic/`, and only where the bytes
differ:
- `check.py`, `measure.py`, `analyse.py`, `probe.py`, `packmodel.py`,
  `stim.py`, `oracle.py`, `tools.py`, `hacc.py`, `freeze.py`, `config.json`,
  `frozen.json`, and the folders `ref/`, `syn/`, `tb/`;
- never `data/`, `__pycache__`, `*.pyc`, `*.parquet`, `test_kit.py`,
  `kitcheck_*.py`, or any other file.

It writes `{relative path: sha256}` of what it synced to `manifest_path`, which
lives in the iteration folder, outside the worktree.
- `write_candidates` calls it before each session.
- The loop calls it for `base_dir` inside a new `prepare_base()` helper, which
  every `starting_check` call goes through, after every `git reset --hard` of
  the base (adopt fallback, resume reset, track adoption, reconcile).
- This fixes F1.

**`_harness_clean` (:393-409).** It runs `git status --porcelain -z
--untracked-files=all -- agentic` (so folders are not collapsed and names are
not quoted). A modified or untracked file under `agentic/` is acceptable if
it:
- is under `agentic/data/`, or
- is a `*.parquet` file, or
- has the sha256 recorded for it in **that session's manifest** (not the live
  kit, which the engineer may edit during a session).

A tracked kit file outside the allowlist that the sync did not touch is
unchanged by definition and is not listed. Anything else still blocks Bash.
The sync never lands in a candidate commit, because `commit_candidate` adds
only `rtl` (plus `docs/track-<id>` for tracks).

**Environment.**
- The loop sets `os.environ['AGENTIC_RUN']` at startup.
- Per session, the SDK options' env (built from `child_env()`, which the SDK
  merges over the parent's) adds `AGENTIC_CALIB=<commit>`: the best commit for
  normal sessions, the track's recorded calibration commit for track sessions
  (5.1).

**SYSTEM_BRIEF (:519-538).** Retitle the section "THE COMMANDS YOU MAY RUN"
and add:
```
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
```

### 3.5 Loop wiring (phase B)

**`write_calibration`** runs after the baseline (:1490), on resume after the
base reset (:1445), and in `adopt` (:998). It:
- maps the best to `(rules, K, L, N)` as in 3.3, using `rtl_widths(...)
  ['defaulted']` (F12) so defaulted widths are never used;
- records the measured bytes/cycle per table from the scored real draws into
  `calib-<commit>.json`;
- writes `current-<run>.json`;
- logs a calibration change (3.3).

**`build_ctx`** adds `packmodel_text`:
- computed once per best commit via `tools.run` with a 15-minute timeout;
- the result, **success or failure**, is cached per best commit in `Run`
  (memory) and in `.agentic/packmodel/text-<commit>.txt`, so a failure costs
  one attempt per best commit, not one per iteration; a failure reads
  "(packing model unavailable: …)";
- the same subprocess call also evaluates every `[model K/L/N]` tag in the
  roadmap (4.2), so tags never run the model inside the loop process;
- timed as `timing['packmodel_s']`, which is added to TIMING_ORDER.

**Prompts.** The planner prompt gets a "PACKING MODEL (bytes/cycle the real
pages allow at other widths; aggregate only)" section after LEVER. The session
brief gets the same section after STAGE PROFILE.

### 3.6 Tests

Fast tests in `kitcheck_packmodel.py`:
- `test_packmodel_parse_matches_model` (a hand-built chunk).
- `test_packmodel_ideal_tiny`: hand-computed cycle counts, and `hazard=none` ≥
  `window`.
- `test_packmodel_output_hides_held_out`: each held-out table gets a unique
  number in the fixture, and none of those numbers or names appears in the
  output.
- `test_packmodel_error_line_is_fixed`: an injected exception prints only the
  fixed line and exits 3.
- `test_packmodel_session_mode_never_parses`: with `AGENTIC_PACKMODEL_DIR` set
  to a fixture holding `current-<run>.json`, cached raw results and no pages,
  it answers a cached what-if and prints the fixed line for an uncached one
  that would need pages (no `stim.table_path` call; checked by monkeypatch).
- `test_packmodel_cache_writes_are_atomic_and_merge`: two writers' entries both
  survive; a truncated JSON file is treated as empty.
- `test_packmodel_calibration_mapping`: i35 widths map to rules i35; a
  defaulted K maps to uncalibrated; a `packmodel_widths` override with another
  commit is ignored.
- `test_gate_allows_packmodel_only_strictly` (guarded on the SDK being
  importable):
  - allowed: the bare command and `--k 4 --l 32 --n 32 --rules dsw4`;
  - denied: `; rm -rf rtl`, `| cat`, `--dump`, `python agentic/stim.py` and
    `$(x)`.
- `test_gate_git_forms` (SDK guard):
  - allowed: `git status`, `git show HEAD:rtl/vhsnunzip_pkg.vhd`,
    `git diff HEAD~1..HEAD -- rtl/`, `git log --oneline -n 20`,
    `git log -p -n 3 -- rtl/`;
  - denied: `git diff --no-index /etc/x rtl/a.vhd`, `git log --output=../x`,
    `git diff --output=x -- rtl/`, `git diff --ext-diff -- rtl/`,
    `git show --textconv HEAD:rtl/a`, `git diff -a -- rtl/`,
    `git show HEAD:agentic/data/x.parquet`, `git show HEAD:../x`,
    `git show HEAD:/abs`, `git diff HEAD` (no pathspec), `git -C .. status`.
- `test_harness_clean_accepts_synced_kit` (on a throwaway git repo): a file
  matching the manifest is clean; an edited one, a new `agentic/evil.py`, or a
  file in an untracked new folder (`agentic/x/y.py`) blocks; a kit file edited
  after the sync does not block that session.
- `test_sync_allowlist_is_closed_and_clean`:
  - every module imported by an allowlisted file is stdlib or itself
    allowlisted;
  - no synced file contains a held-out table name within 40 characters of a
    decimal number (`stim.py`'s table-name tuple is the one exception).
- `test_blinded_worktree_has_no_held_out_numbers`: the same scan over a
  blinded checkout's tracked files outside `rtl/` (this plan included). If an
  existing doc fails, `blind()` gets that file added to its removal list in the
  same commit.

Slow test, run when the page cache exists or `AGENTIC_SLOW_TESTS=1`:
- `test_packmodel_reproduces_diag`: with i35 calibration from hacc-real200's
  best per-table values (read at run time), it reproduces 8.99, 16.00 and 19.28
  within 3%.

Offline check: `python agentic/packmodel.py --selfcheck` (the gate does not
allow this flag). It exits non-zero on a miss and records cold and warm wall
time and peak memory; the target is under 5 minutes cold with 3 workers.

---------------------------------------------------------------------------

## 4. Change 6: roadmap rules (phase B)

### 4.1 State
```json
"roadmap": {"steps": {"2": {"title": "...", "text_sha": "...", "modelled_bpc": 14.06,
  "modelled_gain_pct": 44.8, "status": "open|disputed|dropped",
  "disputes": [{"iteration": 39, "label": "c1", "proof": "<=2000 chars"}],
  "zero_results": [{"iteration": 41, "label": "c1", "gain_pct": 0.3}],
  "dropped_at": null, "drop_reason": ""}}}
```
Created on load with `setdefault`, next to `setdefault('tracks', [])`.

### 4.2 Parsing (replaces the raw `Run.roadmap_text`)

**`parse_roadmap(text)`.**
- Headings match `^STEP\s+(\w[\w.-]*)\s*[:,]\s*(.*)$`. hacc-real200's file has
  `STEP 4, if step 2-3 ...` (comma), which the colon-only form folded into
  step 3.
- A modelled gain is read from a tag:
  - `[model K<k>/L<l>/N<n>]`, optionally with `rules=dsw4`, resolved from the
    cached packmodel run for the current best (3.5); or
  - `[model +x%]` or `[model y B/cycle]`, taken literally.
- A step with no tag, or a tag the model could not resolve, is "unmodelled"
  and ranks last.

**Step ids are normalised everywhere** (roadmap file, planner, session):
`str(x).strip()`, a leading `STEP` removed case-insensitively, then stripped
again; so `"STEP 2"`, `"2"` and `2` are the same id. An unknown id is logged
("planner named unknown roadmap step X; ignored") and cleared, never a
`KeyError`.

**`sync_roadmap_state`.**
- New step ids are added.
- A dropped step reopens, with its counters cleared, if the engineer edits its
  text (`text_sha` changes). That is how the engineer overrules a drop.
- Steps removed from the file stay in state but are not shown.

**`roadmap_brief`.** Open and disputed steps come first, ranked by modelled
gain, each with its body. Then dropped steps, one line each with the reason:
```
STEP 2 [open; modelled 14.1 B/cycle = +45% bytes/cycle]: two copies per cycle ...
STEP 1 [DISPUTED once; unmodelled]: all-position speculative decode ...
    proof from i39 c1: "the decoder already emits one element per cycle ..."
STEP 3 [DROPPED at i44 after 2 results within 1%: -0.2%, +0.4%]
```

### 4.3 Recording disputes and drops (loop.py)

**Events are applied only at the save point (F11).** `run_iteration` collects
roadmap events in a local list, `events`. `append_record(..., events)` applies
them to `state['roadmap']` (and track events, 5.5) immediately before
`state['iterations'].append(record)`, in the same save. An interrupt before
that save persists none of them, so a redone iteration cannot count a dispute
twice. The record keeps a copy as `roadmap_events` for the report.

**Disputes.** A candidate yields a dispute event when all of these hold:
- its outcome is `no_proposal`;
- its direction has a `roadmap_step`;
- its proposal has a non-empty **`dispute`** field whose normalised
  `roadmap_step` equals the direction's (a bare rationale is not a dispute);
- its session status is `ok` (not budget, timeout, limit or error) and it is
  not truncated.

The proof is dispute + rationale + the notes' "Why it worked or failed",
capped at 2000 characters. When applied, the step becomes `disputed`, and the
loop logs `ROADMAP STEP <id> DISPUTED by i<k> <label>: ...`.

**Zero results.** A candidate yields a zero-result event when:
- the direction has a `roadmap_step`;
- the outcome is candidate, no_gain or regressed;
- `|gain_pct| < roadmap_zero_pct` (default 1.0);
- the session was not truncated, and the candidate is not a `retry-*`.

Compile, correctness and measurement failures do not count.

**Drop.** When applying events brings a step to 2 disputes or 2 zero results:
- the step becomes `dropped`;
- the loop logs a prominent `ROADMAP STEP <id> DROPPED: <reason>` between `=`
  banner lines and updates the status line.

**Dropped steps assigned anyway.** If the planner assigns a dropped step, the
loop clears `roadmap_step`, keeps the direction, and logs it.

### 4.4 Prompt changes (propose.py)

**Delete :731-736** (the rule that direction 1 must carry out the next roadmap
step and that it overrides the skill library) and replace it with:
```
- The ROADMAP is the engineer's advice, ranked by modelled gain, not an
  order. Prefer its highest-ranked OPEN step when the profile and the
  packing model agree it moves the binding limit. A DISPUTED step comes
  with a session's proof that it is empty or wrong: assign it again only
  if you say what that proof missed. DROPPED steps must not be assigned.
  The skill library's AVOID marks hold for roadmap steps too. When a
  direction carries out a roadmap step, put the step id in roadmap_step.
```

**Replace :739-743** with:
```
- One direction should raise the per-cycle capacity of the stage the probe
  names as the limit (a structural change: more elements per cycle, a
  wider line or port). Check it with the PACKING MODEL first: if that
  widening alone models under +3% because another limit takes over, do not
  assign it alone. Assign the combination the model says pays, or open a
  TRACK when the combination is more than one session can build. Timing
  tweaks are for the other directions.
```

**Plan JSON schema.** Each direction gets `roadmap_step` and
`modelled_gain_pct`. `plan_directions` (:796-808) whitelists both: a string of
at most 20 characters or null (normalised, 4.2), and a float or null.

**Session ROADMAP header (:641-643):**
```
ROADMAP FROM THE ENGINEER (advice ranked by modelled gain, not orders.
If your assignment names a roadmap step and you find that step cannot
move bytes/cycle on this design, decline with a proof: see WHEN YOU ARE DONE.)
```
The assignment block also shows `roadmap: STEP <id>`.

**SYSTEM_BRIEF decline paragraph (:605-611)** gains:
```
If your assignment names a roadmap step and you can show that step is
empty or wrong for this design (the probe, the packing model or the RTL
shows it cannot move bytes/cycle), write
{"id": "none", "roadmap_step": "<id>",
 "dispute": "the proof: what you ran or read, with numbers and file:line",
 "rationale": "what you would build instead"}
and leave rtl/ as you found it. The planner reads your proof; two proofs
drop the step.
```

**`read_proposal`** whitelists `roadmap_step` (up to 20 characters,
normalised) and `dispute` (up to 2000).

### 4.5 Tests
- `test_roadmap_parses_steps_and_models`: the hacc-real200 text gives steps
  1, 2, 3 and 4 (step 4's comma heading included), unmodelled; a tagged step
  ranks first.
- `test_roadmap_step_ids_normalise`: `"STEP 2"`, `"2"` and `2` match; an
  unknown id is cleared and logged.
- `test_dispute_is_recorded_and_two_drop`.
- `test_rationale_alone_is_not_a_dispute`.
- `test_truncated_session_is_not_a_dispute`.
- `test_two_zero_results_drop`: a correctness failure, a truncated session and
  a `retry-*` candidate do not count.
- `test_interrupt_does_not_double_count`: an iteration that produced a dispute
  event and then raised `KeyboardInterrupt` saves state with no dispute; the
  redone iteration's dispute makes the count 1.
- `test_edited_dropped_step_reopens`.
- `test_old_state_without_roadmap_or_tracks_loads`: runs on a copy of
  hacc-real200's state.json in `.agentic/ktest`, and asserts the original's
  mtime is unchanged.
- `test_plan_prompt_has_no_override_rule`: no "MUST carry out", "overrides the
  library" or "Area is priced".
- `test_read_proposal_keeps_dispute_fields`.

---------------------------------------------------------------------------

## 5. Change 4: build tracks (phase C)

### 5.1 State and config
```json
"tracks": [{"id": "k4-l32-n32", "branch": "agentic-track/hacc-real200/k4-l32-n32",
  "head": "<sha>", "base_commit": "<best when opened>", "base_bpc": 9.71,
  "calib_commit": "<best when opened>", "goal": "...", "why": "...",
  "opened_iteration": 54,
  "status": "open|final_pending|adopted|abandoned|finished", "reason": "",
  "iterations_used": 0, "current": 0, "never_measured_streak": 0,
  "design_failures": 0,
  "steps": [{"n": 0, "kind": "design", "text": "...", "predicted_bpc": null,
             "widths": null, "source": "planner|spec", "status": "pending|passed|failed",
             "ref": null,
             "attempts": [{"iteration","label","branch","commit","outcome",
                           "measured_bpc","f_max_mhz","reason","cost_usd"}],
             "notes": ""}],
  "final": null}]
```
- `head` is the source of truth; on resume the branch is reset to it.
- `calib_commit` is the calibration the track was opened under; its sessions
  get `AGENTIC_CALIB=<calib_commit>` and `judge_design` checks predictions
  against it, so the track's predictions are not moved by later adoptions.
- The branch name `agentic-track/<run>/<id>` is not nested under the run
  branch's name. A passed step also gets its own ref
  `agentic-track/<run>/<id>-s<n>` (5.5); ids ending in `-s<digits>` are
  rejected so the two can never collide.
- **Step list layout.** `steps = [design step] + spec steps`. The design step
  is `n = 0`; after it passes, `current = 1`. The spec may hold 1 to
  `track_max_steps` steps, so the list holds at most `track_max_steps + 1`.
- **Step kinds:**
  - `design`: writes SPEC.md and steps.json, and leaves rtl/ untouched;
  - `golden` and `ports`: checked for correctness only;
  - `throughput`: checked against its prediction. The last step must be of
    this kind.
- **The kind always comes from the loop-owned step list.** A session's
  `step_kind` in PROPOSAL.json is shown in the log and never used to judge.
- **Load-time check.** For every open track, `0 <= current < len(steps)` and
  `steps[current]` exists; otherwise the track is abandoned with reason "state
  inconsistent: current=<c>, steps=<n>" and a banner. `track_direction` is
  never reached with a bad index.

New config keys (tools.py DEFAULT_CONFIG, each with a comment, and
config.json):

| key | default | why |
|---|---|---|
| `tracks_enabled` | true | kill switch |
| `track_max_attempts_per_step` | 2 | failed attempts on one step before the track is abandoned |
| `track_max_iterations` | 8 | total iterations a track may use, re-measures included (DSW-4 took about 4 build steps plus a design) |
| `track_max_never_measured` | 2 | never-measured results in a row on one step before the next one counts as a failed attempt |
| `track_bpc_tolerance_pct` | 10.0 | a non-final throughput step passes at ≥ 0.9 × its prediction |
| `track_max_steps` | 6 | cap on the steps.json length |
| `track_synth_steps` | false | synthesise non-final steps for an f_max reading (shown, never judged) |
| `track_session_budget_usd` | 40.0 | change 5 |
| `track_session_timeout_min` | 120 | change 5 |
| `roadmap_dispute_limit` / `roadmap_zero_limit` / `roadmap_zero_pct` | 2 / 2 / 1.0 | change 6 |
| `probe_enabled` / `probe_window_cycles` | true / 4096 | change 2 |
| `packmodel_calib_rules` / `packmodel_widths` / `packmodel_jobs` | "ideal" / null / 3 | change 3 (`packmodel_widths` is `{commit, rules, K, L, N}` or null) |

### 5.2 Opening a track

**Planner JSON.** It may add:
- `"open_track": {"id", "goal", "why", "steps": [{"text", "kind",
  "predicted_bpc"}]}`, where `why` cites the packing model;
- or `"abandon_track": "<reason>"`.

`plan_directions(ctx, n, log, track_open)` returns `(dirs, res, track_cmd)` and
sanitises the request:
- the id is kebab-case, at most 40 characters, does not end in `-s<digits>`,
  and is made unique against state;
- there are 1 to `track_max_steps` steps, each kind is from the list, and
  `predicted_bpc` is a float or null.

**The planner is always asked for n directions, ranked.** While a track slot
runs, it takes the place of the lowest-ranked direction. Whenever the track
slot does not run a session this iteration (the track was just abandoned,
the slot could not be prepared, or the track only needs a re-evaluation,
5.4), that direction runs instead. No slot is ever left empty.

**New planner section** (from `tracks_text(state)`):
```
TRACKS (multi-step builds)
<open track: id, goal, steps with status, predictions vs measured, attempts left,
 iterations left; or the last abandoned/finished track with its reason; or "none">
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
"abandon_track": "reason".
```

**In `run_iteration` (:1149).**
- When the planner returns `open_track` and none is open, the loop:
  1. builds the track object in memory (`head = base_commit = calib_commit =
     best`, `base_bpc` = best bytes/cycle);
  2. runs `git branch -f <branch> <best>` (state is the truth, so a branch
     left by a crashed earlier attempt is simply moved);
  3. logs `TRACK <id> OPENED`;
  4. adds an `open` event; the track enters `state['tracks']` only in
     `append_record` (F11). Until then the iteration uses the in-memory
     object.
- If the usage window ends the iteration with pending slots (:1225-1227), the
  pending dict stores the unrecorded track object, and resume restores it.
- **In `--dry-run`, no track is ever created**: the loop logs "would open
  track <id>" and goes on.
- With one candidate slot and an open track, the planner is skipped.

### 5.3 The track slot

**Preparing it.** `prepare_track_slot` wraps everything that touches git
(worktree, branch, `git show` of the spec) in try/except. On a `GitError` it
logs `TRACK <id> slot suspended this iteration: <error>`, the slot goes to the
spare direction, and `run_iteration` does not return `'error'`. Two
suspensions in a row abandon the track with the reason.

**`new_assignment(..., track=None)`.**
- The label is `t<n>a<j>` (step n, attempt j) and the branch is
  `agentic-cand/<run>/i<k>-t<n>a<j>`. A redo of iteration k reuses the same
  label only for the same attempt, whose commit was never recorded.
- `start` is `track['head']`.
- The assignment carries `track_id`, `step` and `kind`.

**`track_direction(track)`.**
- Builds `focus` and `hypothesis` from the step text and prediction.
- `direction['track']` holds the track fields, plus:
  - the spec, read with `git show <head>:docs/track-<id>/SPEC.md` and capped
    at 12000 characters;
  - the notes of the passed steps, capped at 6000.

**Re-measuring a never-measured step.** If a step's last attempt was never
measured and its branch exists, the slot measures that commit again with no
session and no cost. This **counts toward `iterations_used`**, and after
`track_max_never_measured` (2) never-measured results in a row, the next one
counts as a failed attempt. In an iteration where the track slot measures a
step, `retry_candidates` is skipped (retries wait one iteration), so the
number of designs simulated at once never exceeds the number of slots (the
WSL memory limit behind the rc-137 kills).

**Pending and resume (:1225-1227, :1129-1131).** Slot dicts carry `track_id`,
`step`, `kind` and the label. On resume, a slot whose track was abandoned
meanwhile is discarded.

**Commits.**
- `commit_candidate(path, msg, extra_paths)` also adds `docs/track-<id>`.
- Track slots never go through the `no_proposal` test at loop.py:1219; their
  outcome comes from the judging below.

**Writable paths.** `make_gate(worktree, writable=...)`:
- build steps: `rtl/`, `docs/track-<id>/`, PROPOSAL.json, NOTES.md;
- the design step: `docs/track-<id>/`, PROPOSAL.json, NOTES.md.

### 5.4 Judging a slot (pure functions in loop.py)

**Session-level results first** (`judge_session`):

| session result | outcome | attempt? | iterations_used |
|---|---|---|---|
| `limit`, no commit | kept pending, resumed after the reset (as the all-limit path does) | no | no |
| `error`, `timeout` or `budget`, no commit | `track_session_failed` | no | +1 |
| any status with a commit | judged below (a truncated session's committed work is real) | yes | +1 |
| `ok`, no commit, build step | `track_step_failed` ("no change made") | yes | +1 |

A design session resumed after a limit gets `DESIGN_RESUME_PREFIX`, which
says its work is in `docs/track-<id>/`, not in `rtl/`.

**`judge_design`** (never measured, never simulated):
- `SPEC.md` with at least 1500 characters, and a `steps.json` that:
  - has 1 to `track_max_steps` steps with valid kinds;
  - gives every throughput step `widths` (`{K, L, N, rules}`) and a
    `predicted_bpc` above 0;
  - ends with a throughput step.
- **Predictions cannot be sandbagged.** Each throughput step's
  `predicted_bpc` must be at least 0.9 × `packmodel.predict` at its declared
  widths under the track's `calib_commit` (one cached subprocess call for all
  steps). The final step's prediction must exceed `base_bpc × (1 +
  min_gain_pct/100)`.
- Pass → `track_design_done`; the spec's steps replace the planner's
  (`steps = [design] + spec`, `current = 1`).
- Fail → `track_design_invalid` with the reason; `design_failures` += 1.

**Measuring a build step.**
- **Golden and ports steps whose top-level `rtl/*.vhd` are byte-identical to
  `head`** (only `rtl/unit/` or docs changed) are not simulated on the draws:
  the loop runs `check.py --unit <name>` for each unit file the step added or
  changed, and the step passes when all pass.
- Every other step: `measure.measure(..., synth=is_final or
  CONFIG['track_synth_steps'])`. `measure_one` gets a `synth` argument.
- **Non-final steps are judged from simulation only** (`oracle_pass` per draw
  and `bytes_per_cycle`). Synthesis, when it runs, is information: a synthesis
  failure or an unreachable host is logged next to the step and does not
  change its verdict.

**`judge_step(step, metrics, tol)`** (non-final steps):

| condition | outcome |
|---|---|
| measure_error (simulators killed, kit crash) | `track_never_measured` (not an attempt; see the streak rule) |
| compile error or any draw not byte-exact | `track_step_failed` |
| golden or ports step, byte-exact | `track_step_passed` |
| throughput step, bpc ≥ 0.9 × predicted | `track_step_passed` |
| throughput step, bpc < 0.9 × predicted | `track_step_short` |

A step above 1.1 × its prediction still passes and is logged as "model
pessimistic". f_max is recorded and never judged.

**The final step.**
- A byte-exact final step **always** goes to `evaluate` against the current
  best, on throughput only, plus the RAM veto (below) against the track's base
  and the best. Its prediction is only logged ("final step at 0.85 of its
  prediction").
- Synthesis is needed here for throughput; an unreachable host makes it
  `track_never_measured`, bounded by the streak rule.
- If `evaluate` says adoptable, the candidate enters selection through an
  explicit `track_final` path (below). If it wins, `adopt` runs; the ff-merge
  fails, so it does `reset --hard`, and the track becomes `adopted`. The log
  says so when the best moved while the track was open, and names the
  adoptions it replaces.
- **If it is adoptable but a sibling wins the same iteration**, the track
  becomes `final_pending` with `final = {commit, branch, metrics}`. At the
  start of the next iteration the loop calls `evaluate(final.metrics, new
  best)` again (no session, no measurement, since throughput is absolute); if
  it still wins it is adopted then, otherwise the track becomes `finished`.
- If it is correct but not better than the best, the track becomes
  `finished`, with the reason.
- A final step that is not byte-exact is `track_step_failed`, as above.

**No other track candidate can ever be adopted.** Track candidates bypass
`score_candidate`'s adoptable list: every non-final track candidate has
`adoptable=False` forced and is never placed in `adoptable`; only the
`track_final` path adds one. A test checks a non-final step that beats the
best by +50% is not adopted.

**RAM veto, unchanged; the model is logged.** The veto that refuses a
candidate (`ram_latency_changed`) stays exactly as before v3: the
`CMD_STAGES`/`RESP_STAGES` literals of `vhsnunzip_ram.sim.vhd`, compared with
the best's and (for tracks) with the track's base. A stronger veto (a hash of
the whole model and of its port list) was built and reverted after review: the
brief lets no rule but the area gate change what is adopted, and its
whitespace handling refused a candidate that only wrote `a:=b` for `a := b`.
That hash (`loop.ram_model`, tokens only, so comments, case and whitespace
never change it) is logged as a NOTE when an adoptable candidate edits the
model, for a person to judge.

**Exclusions.** Track candidates are kept out of:
- `retry_eligible`, `advantages`, `record_outcomes` and the learner group;
- `NOT_MEASURED` gains the `track_*` outcomes.

A partial step that measures about 0 must not demote a mechanism. Facts from
track notes are still kept.

### 5.5 Advance, abandon, restart safety

**`advance_track`** runs before `make_record` and returns track events; it
changes nothing in `state` itself. On a pass, the events say:
- the step becomes `passed`, with its notes ("how it works" plus "what I
  tried", capped at 1500 characters);
- a ref `agentic-track/<run>/<id>-s<n>` is created at the step's commit
  (`git branch -f`, before the save), so the commit stays reachable even when
  the candidate branch is later deleted;
- `head` moves to the commit and `current` advances.

On a failure it counts the attempt. `iterations_used` goes up once per
iteration in which the slot ran a session or a measurement.

**Abandon** when:
- a step has `track_max_attempts_per_step` (2) failed attempts;
- or `iterations_used` reaches `track_max_iterations` (8), **checked after the
  slot of this iteration is judged**, so a final step judged in the eighth
  iteration still counts;
- or the planner sent `abandon_track`;
- or the design was judged invalid twice;
- or the slot was suspended twice in a row (5.3);
- or the load-time check failed (5.1).

The reason is recorded (for example "step 3 short twice: 12.1, 12.6 vs 15.0
predicted"), the loop logs `TRACK <id> ABANDONED: <reason>` with a banner, and
the planner sees it.

**Saving (F11).** All track events (open, attempt, pass, abandon, final) are
applied to `state['tracks']` inside `append_record`, immediately before the
record is appended, in the one save. The record gets a `track` entry. Only
after that save does the loop move the branch with
`git branch -f <branch> <head>`. An interrupt saves no track change.

**`reconcile_tracks`** runs on resume, after :1445. It resets each open
track's branch to `head` (`git branch -f`), so state is always the truth.
- A crash after the save: the branch catches up.
- A crash before the save: the iteration is redone. `head` was not saved, so it
  still points at a commit that has a step ref or is the base.

**`Run.save()` cannot crash the loop through the report.** `report.write_report`
inside `save` (loop.py:609) is wrapped in try/except that logs the error; the
state file is already written by then.

**History, report and gui.**
- `history_text`: lines like `i57 t2a1 track <id> step 2 (ports): passed,
  byte-exact`.
- report.py:143: a CSS class `track`, so these outcomes are not shown as
  "bad", plus a Tracks table.
- gui.py:213: track points are drawn hollow and never join the best series.

### 5.6 Tests (no sessions, no simulator)
- `test_track_state_defaults`.
- `test_track_load_check_abandons_bad_index`.
- `test_open_track_from_plan` (the sanitiser; `-s3` id rejected).
- `test_planner_always_gives_n_directions`: with a track open the last
  direction is replaced by the track slot; with the track abandoned or
  suspended in the same iteration, it runs.
- `test_judge_design`: good spec passes; a missing `widths`, a sandbagged
  prediction (below 0.9 × a faked model) and a final prediction not above the
  base each fail.
- `test_judge_session_table` (the session-level table, including the limit
  case kept pending).
- `test_judge_step_rules` (table-driven: golden passes; 0.92× passes; 0.85× is
  short; an oracle failure fails; measure_error is not an attempt; a synthesis
  failure on a non-final step does not change the verdict).
- `test_unit_only_step_is_not_measured`: a ports step that changes only
  `rtl/unit/` is judged by `check.py --unit` (faked) and never calls
  `measure.measure`.
- `test_never_measured_streak`: the third never-measured result in a row is a
  failed attempt, and each one counts toward `iterations_used`.
- `test_track_abandons_after_two_failures`.
- `test_track_abandons_after_eight_iterations` (and a final step judged in the
  eighth iteration is still scored).
- `test_non_final_step_never_adopted`: +50% over the best, not adopted.
- `test_final_step_scored_against_best`: at +12% the track wins over a +3%
  normal candidate; at -5% it becomes `finished`; at 0.85× its prediction but
  +20% over the best it is adopted.
- `test_final_pending_rescored`: losing to a sibling gives `final_pending`; the
  next iteration's re-evaluation adopts it or finishes it.
- `test_track_events_only_at_save`: an interrupt after `open_track` and a step
  pass leaves `state['tracks']` unchanged.
- `test_step_ref_survives_branch_delete` (throwaway repo): after a pass, the
  `-s<n>` ref holds the commit when the `agentic-cand` branch is deleted.
- `test_track_candidates_do_not_touch_skills`.
- `test_reconcile_tracks` (on a throwaway repo under
  `.agentic/ktest/trackrepo`).
- `test_open_track_with_leftover_branch` (throwaway repo): a branch already
  exists with that name; opening succeeds.
- `test_slot_prep_git_error_suspends`.
- `test_pending_slot_keeps_track_context` (including an unrecorded opened
  track).
- `test_dry_run_creates_no_track`.
- `test_ram_veto_hash` (comment-only change passes; a stage or port change
  vetoes).
- `test_save_survives_report_error`.
- `test_gate_writable_for_tracks` (SDK guard).
- `test_report_and_gui_accept_track_outcomes` (the gui part is skipped without
  tkinter).

---------------------------------------------------------------------------

## 6. Change 5: step sizing (phase C)

### propose.py

**`write_candidates`.** For each assignment with a `direction['track']`:
- it uses `track_session_budget_usd` and `track_session_timeout_min` in the
  @@BUDGET@@ text (:925-927), the budget (:931) and the timeout (:937). The
  meter hook already takes these per job;
- it passes the gate's writable list for the step kind;
- it sets `AGENTIC_CALIB=<track calib_commit>` in that session's env;
- the system prompt is SYSTEM_BRIEF plus TRACK_BRIEF for build steps, or plus
  DESIGN_BRIEF for the design step.

**Elsewhere.**
- `burn_rate` skips track candidates, so their bigger budgets do not skew the
  rate for normal sessions.
- loop.py:1201-1202 logs both budgets when a track slot runs.
- **The usage-window pre-wait (:1524) scales with the slots.** Its threshold
  becomes `min(60, longest timeout among this iteration's slots)` minutes
  instead of a fixed 25. It is capped at 60 because a session cut off by the
  limit is kept pending and resumed (5.4), so waiting up to two hours would
  cost more than it saves.
- `read_proposal` whitelists `step_kind` (informational only, 5.1) and
  `predicted_bpc`.

TRACK_BRIEF:
```
THIS SESSION IS ONE STEP OF A BUILD TRACK
  This brief replaces "one change, fully carried through" with: build ONE
  module (or one interface change) plus its unit test, exactly as the step
  says. Track <id>: <goal>. You start from the track branch <branch>, not
  from the best design; the earlier steps are already in it.
  Your step (<n> of <m>, kind <kind>): <text>
  <throughput>  Predicted bytes/cycle after this step: <p>. It passes if every
                draw is byte-exact and the measured bytes/cycle is at least
                90% of that.
  <golden/ports> This step is checked for correctness only: every draw
                byte-exact (or, if you change only rtl/unit/, your unit
                tests must pass).
  Unit test: put it in rtl/unit/<name>.vhd (entity <name>). Run it with
    python agentic/check.py --unit <name>
  which compiles it with rtl/ and runs it in a folder holding golden data
  from invented shapes:
    cs.tv         the compressed chunks (the testbench's format)
    expected.hex  their decompressed bytes
    elements.tv   every Snappy element, one per line: pos kind hdr len offset
  It passes when the simulation ends without an assertion failure.
  The whole design must still pass `python agentic/check.py` when you
  finish: if the new module is not complete yet, wire it in behind the old
  path or leave it unconnected.
  THE SPEC (docs/track-<id>/SPEC.md):
  <spec>
  NOTES FROM EARLIER STEPS:
  <prev_notes>
  If the spec is wrong for this step, write the correction in
  docs/track-<id>/STEP-<n>.md and say so in PROPOSAL.json. The step's kind
  and prediction are fixed by the track; you may add "predicted_bpc" with
  your own estimate, which is logged.
```

DESIGN_BRIEF:
```
THIS SESSION DESIGNS A BUILD TRACK. Do not change rtl/.
  Track <id>: <goal> (why: <planner's why>).
  Write docs/track-<id>/SPEC.md: the target architecture (stages, records,
  widths, handshakes, RAMs), why it pays (run agentic/packmodel.py at the
  target widths and quote its aggregate), the interfaces between the new
  modules, and an order of building in which every step leaves a design
  that passes the check. Write docs/track-<id>/steps.json:
    [{"text": "...", "kind": "golden|ports|throughput", "module": "rtl/...",
      "widths": {"K": k, "L": l, "N": n, "rules": "ideal|dsw4"} (throughput steps),
      "predicted_bpc": <number for throughput steps, else null>}]
  1 to <track_max_steps> steps, the last of kind throughput with the
  target bytes/cycle, which must beat the current best's <base_bpc>.
  Predictions come from the packing model at the widths each step has
  reached; a step that widens one dimension alone is expected to gain
  about nothing, so predict that honestly. A prediction below 90% of the
  packing model at the declared widths is rejected.
```

### check.py `--unit NAME` (phase C owns check.py)
- **Validation.** NAME must match `[A-Za-z0-9_]{1,40}` and
  `rtl/unit/NAME.vhd` must exist.
- **Golden data**, written to `.agentic/selftest/unit/` from the invented
  shapes only:
  - `cs.tv`, via `stim.write_cs_tv`;
  - `expected.hex`, from the reference decompressor;
  - `elements.tv`, via `packmodel.parse` (pure parsing of invented bytes; no
    cache, no Parquet).
- **Run.** Compile `rtl/*.vhd` (minus `*.syn.vhd`) plus the unit file through
  `tools.eda_shell` and a new `agentic/syn/unit.sh` (not frozen; it never
  touches scored draws), then print PASS or FAIL with the log tail.
- **Gate.** Add the regex `^python3? agentic/check\.py --unit
  [A-Za-z0-9_]{1,40}$`.
- **Sync.** `syn/unit.sh` is in the synced `syn/` folder.
- **Tests.** Argument validation (fast); a trivial unit testbench run under
  GHDL (slow, guarded).

---------------------------------------------------------------------------

## 7. Order of implementation and file ownership

| phase | runs | owns (only these files) | depends on |
|---|---|---|---|
| 0 | serial, small | `test_kit.py` (collector hook only) | none |
| A1 probe | parallel with A2 | new `probe.py`, `measure.py`, `analyse.py`, `freeze.py` + `frozen.json`, new `kitcheck_probe.py` | 0 |
| A2 packmodel + gate | parallel with A1 | new `packmodel.py`, `propose.py` (git and packmodel gate rules, deny text, `sync_harness`, `_harness_clean`, the commands section of SYSTEM_BRIEF), new `kitcheck_packmodel.py` | 0 |
| B scoring + roadmap + wiring | after A1 and A2 | `loop.py`, `propose.py`, `tools.py`, `config.json`, `README.md`, `test_kit.py` | A1, A2 |
| C tracks + step sizing | after B | `loop.py`, `propose.py`, `check.py`, new `syn/unit.sh`, `tools.py`, `config.json`, `report.py`, `gui.py`, `test_kit.py` | B |

No phase edits `syn/sim_draws.sh` or any other frozen file; A1 adds
`probe.py` to the frozen list and records hashes deliberately (2.2).

**Phase 0.**
- `test_kit.main` also runs the `test_*` functions of every
  `agentic/kitcheck_*.py`, after setting `mod.check = check`. There is still
  one FAILED list.
- This lets A1 and A2 add checks without both editing test_kit.py.

**A1 and A2 share no file.**
- A1 reads its config with `CONFIG.get(...)`; the defaults land in tools.py in
  phase B. `measure.py`'s `ProbeBuildError` and `prepare_probe` are A1's.
- A2 needs no config beyond `CONFIG.get('packmodel_jobs', 3)`.
- All loop.py wiring for both waits for phase B (including `measure_one`'s
  `measure_error`, the adoption-time clean check and the resume backfill).

**Phase B order:**
1. change 1, with the area-era relabelling;
2. probe wiring (2.4) and `Run.save()`'s guarded report;
3. packmodel wiring: `AGENTIC_RUN`, `write_calibration`, the pointer file,
   `prepare_base()` around every `starting_check`, per-session
   `AGENTIC_CALIB`, the fake planner and the `AGENTIC_DRAWS` guard (7.3);
4. change 6, with events applied in `append_record`.

**Phase C order:**
1. config keys;
2. pure track functions, with their tests;
3. propose changes (briefs, gate, budgets, burn_rate, pre-wait);
4. `check.py --unit`;
5. `run_iteration` integration;
6. report and gui.

**Commits.** One commit per change. Each must leave `python
agentic/test_kit.py` passing; run it, and quote its last line in the commit
message.

### 7.3 Offline dry runs (no paid calls, no loop start)
1. **Probe:** the check in section 2.6, after A1.
2. **Packmodel:** `--selfcheck`, then, from the main checkout, one run that
   writes `current-<run>.json` for a scratch run name. Then a no-argument run
   inside a scratch worktree of the hacc-real200 base commit after `blind()`
   and `sync_harness`, with `AGENTIC_RUN` set to the scratch name. This proves
   it finds the cache through the pointer with no `agentic/data`, never parses
   Parquet there, prints no held-out names, and that the gate regex accepts the
   exact command.
3. **Loop dry run.**
   - `--fake` gets a scripted planner (`fake_plan_directions`) that returns the
     fallback directions and, on iteration 1, an `open_track`.
   - `fake_write_candidates` handles track slots: a design step writes a
     minimal SPEC.md and steps.json; a build step makes a comment-only edit.
   - **`AGENTIC_DRAWS` is a test-only setting with guards.** It is honoured
     only with `--fake` on a new run. A non-fake start or any `--resume` with
     it set refuses to run. Every run stores its draw names in
     `state['draw_names']` at the baseline (for old state files, derived from
     the baseline record's draws), and every resume refuses when the corpus
     draw names differ.
   - Run: `AGENTIC_SYNTH_BACKEND=yosys AGENTIC_DRAWS=synth-512B,synth-8192B
     python agentic/loop.py --run ktest-v3 --fake --iters 3`.
   - Check: the track opens, the design step is judged without measurement,
     pending/resume works, and Ctrl-C followed by `--resume` reconciles the
     branch and does not double-count events.
   - Afterwards delete the `agentic-cand/ktest-v3/*` and
     `agentic-track/ktest-v3/*` branches and the run folder.
4. **Real run, read-only.** On a copy of hacc-real200's state.json in
   `.agentic/ktest/`, run the load path: the `setdefault`s, the track load
   check, `sync_roadmap_state` against its roadmap.md (steps 1-4), the
   `draw_names` derivation, and `write_calibration` into a scratch directory.
   Check that the original's mtime is unchanged.

---------------------------------------------------------------------------

## 8. Risks and decisions left to the user
- **A track adoption can discard later normal adoptions.** If the best moved
  while the track was open, adopting the track resets to the track's commit.
  Rebasing the track instead would break its step predictions, so the plan
  only logs it (naming the adoptions replaced). Is that acceptable?
- **What K means.** The model's K is elements of any kind per cycle. For i35
  the diag convention K = `copy_slots` (2) is kept, because the i35-exact
  model is the calibration denominator and the known numbers use it. Designs
  outside the i35 and DSW-4 families use `elements_per_transfer` when it was
  read from the RTL; otherwise calibration needs `packmodel_widths` set by
  hand for that commit, or the model runs uncalibrated and says so.
- **Calibration jumps between design families.** When the best moves to a new
  family, the ratio changes (about 0.93 under i35 rules vs about 1.08 under
  ideal rules at the same widths). The plan logs the change and never compares
  numbers across calibrations; it does not try to remove the jump, because the
  new design's own measured/model ratio is the better estimate.
- **Cost.** A $40, 120-minute track session alongside a normal candidate
  roughly doubles the spend per iteration, against the shared 5-hour Max
  window.
- **Simulation at adoption and resume.** The clean check before every adoption
  adds a few minutes (visible draws only); the one-time probe backfill on
  resume costs one simulation of the best on all scored draws.
- **Probe overhead.** If the offline check shows more than 15% simulation
  time, drop windows first (`probe_window_cycles: 0`).
- **hacc-real200's roadmap has no modelled gains,** so all its steps start
  "unmodelled" and rank equally. Adding `[model ...]` tags (for example STEP 2
  `[model 14.06 B/cycle]`, from its own text) is the engineer's edit. This plan
  does not change that file.

---------------------------------------------------------------------------

## 9. Review points and how they were handled

Two reviews: **S** (state and control flow, 25 points) and **I** (isolation and
measurement, 15 points). All points were accepted except where noted.

| point | handling |
|---|---|
| S1, I2: frozen `sim_draws.sh` | Not edited; Python deletes probe files (2.2). `probe.py` added to `FROZEN` with a deliberate re-record (I2). |
| S2: several save points | Roadmap and track events buffered and applied in `append_record` (F11, 4.3, 5.5). |
| S3: non-final step adoptable | Forced `adoptable=False`; only the `track_final` path enters selection (5.4). |
| S4, I7: final step lost | Final step always evaluated against the best; `final_pending` when a sibling wins (5.4). |
| S5: endless re-measure | Re-measures count toward `iterations_used`; streak cap; retries skipped while the track measures (5.3). I chose skipping retries over fewer simulators: simpler, and retries lose only one iteration. |
| S6: synthesis blocks steps | `track_synth_steps` false; non-final steps judged on simulation only (5.4). |
| S7: session failures, resume prefix, pre-wait | `judge_session` table, `DESIGN_RESUME_PREFIX`, pre-wait scaled (5.4, 6). Partly changed: the pre-wait is capped at 60 minutes rather than the full 120, because the limit path already resumes cut-off sessions. |
| S8: design/docs-only routing | Design never measured; unit-only golden/ports steps judged by `check.py --unit` rather than passed blind (5.4). Changed from the suggestion: passing without any check would let a broken unit test through. |
| S9: git errors | `git branch -f`; slot suspension instead of `'error'` (5.2, 5.3). |
| S10: unreferenced step commit | Per-step ref `-s<n>` and attempt number in the label (5.3, 5.5). |
| S11, I10: probe fallback scope | `ProbeBuildError` only; clean rerun only for rc≠0 with no deadlock and no `perf.txt`; stale files deleted; instrumented once per `measure()` (2.2). |
| S12: probe bug scored | Guards in analyse.py; `measure_one` crash → `measure_error` (2.3, 2.4). |
| S13: no probe on resume | One-time backfill with a bytes/cycle equality check (2.4). |
| S14, I5: defaulted widths | `rtl_widths` returns `defaulted`; override bound to a commit (2.3, 3.3). |
| S15, I4: cache key in sessions, races | Pointer file, atomic merged writes, lock, `tools.run` (3.1). |
| S16: live-kit comparison, porcelain | Per-session manifest; `-z --untracked-files=all` (3.4). |
| S17: sync undone by reset | `prepare_base()` before every `starting_check` (3.4). |
| S18: roadmap regex, ids | Comma headings; id normalisation; unknown ids ignored (4.2). |
| S19: weak dispute evidence | Explicit matching `dispute` field; truncated and retry candidates excluded (4.3). |
| S20, I13: `AGENTIC_DRAWS` | `--fake` new runs only; stored draw names checked on resume (7.3). |
| S21: dry run persists track | No track in `--dry-run` (5.2). |
| S22: step index | Defined layout, load-time check, abandon checked after judging (5.1, 5.5). |
| S23: report crash | `write_report` guarded in `save` (5.5). |
| S24: wasted slot after abandon | Planner always lists n directions; the spare one runs (5.2). |
| S25: packmodel failure cost, tags | Success and failure cached per best commit; tags resolved in the same subprocess (3.5, 4.2). |
| I1: git rule leaks | Replaced by an explicit grammar with required pathspecs; gate tests (3.4). |
| I3: per-table numbers in synced files | Allowlist sync, closure and number-scan tests; numbers read at run time. Also applied to this document: per-table held-out values removed from F2 and F7, and a scan over blinded worktrees (3.6). |
| I6: calibration stability | `calib-<commit>.json`, track's own `calib_commit`, logged changes, family models as denominators (3.3, 5.1). Partly rejected: "define K as elements of any kind" is already true of the ideal packer (`ideal_par.py:31`); the fix is the explicit design-to-K mapping, and the cross-family jump is logged rather than removed. |
| I8: gamed predictions and kinds | Kind from the loop's list; declared widths; predictions checked against the model; final prediction must beat the base (5.1, 5.4). |
| I9: weak RAM veto | Normalised file hash plus port list (5.4); F13 shows it does not block DSW-4. |
| I11: probe file I/O | Adaptive flush schedule instead of keeping files open (works in any VHDL standard, bounds the tail to 1/16); status-form `file_open`; long paths hashed; windows only with a marker in visible draw folders (2.1). |
| I12: one bad pair breaks the probe | Type checks, prefixed identifiers, retry without the failing architecture, `probe_status`, clean check before every adoption (2.1, 2.2, 2.4). |
| I14: area-era records | `history_text` relabel and a fixed note before the skills (1). The notes in skills.json are not rewritten: they are the learner's text. |
| I15: packmodel memory and cache | 3 workers, one table per task, lock, atomic writes (3.1). |
