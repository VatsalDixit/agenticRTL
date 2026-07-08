# The optimization loop (stage 3)

Closed-loop, agent-driven improvement of `vhsnunzip_unbuffered`: an optimizer
agent edits the RTL, the harness verifies correctness and measures area/timing,
and improvements that keep the design correct are accepted. This runs only on a
design that already passes stage-1 verification.

```
        ┌─────────────────────────────────────────────────────────────┐
        │  optimizer agent (.claude/agents/rtl-*-optimizer)             │
        │  reads metrics + reports → makes ONE small rtl/ edit          │
        └───────────────┬───────────────────────────────────────────────┘
                        ▼
        bash flow/eval.sh <oss|vivado> --goal <timing|area>
        ├─ 1. CORRECTNESS GATE  flow/verify.sh  (GHDL ladder, in WSL)
        │        fail → auto `git restore rtl/`  (broken edit discarded)
        ├─ 2. MEASURE           flow/measure.sh  → flow/metrics/latest.json
        │        oss    = Yosys (WSL, ~30s, relative signal)
        │        vivado = Vivado (Windows, minutes, accurate Fmax/area)
        └─ 3. COMPARE           latest vs flow/metrics/baseline.json → verdict
                        │
             accept ────┴──── reject
             │                    │
   git commit + set_baseline   git restore rtl/
```

## Pieces

| File | Role |
|------|------|
| `.claude/agents/rtl-timing-optimizer.md` | agent spec: shorten critical path |
| `.claude/agents/rtl-area-optimizer.md`   | agent spec: reduce LUT/FF/BRAM |
| `flow/verify.sh`        | correctness gate (GHDL ladder via WSL) |
| `flow/measure.sh`       | run synthesis, normalize metrics → `flow/metrics/latest.json` |
| `flow/eval.sh`          | verify → measure → compare; auto-reverts incorrect edits |
| `flow/set_baseline.sh`  | record current committed design as the baseline |
| `flow/metrics/baseline.json` | metrics of the last accepted design (tracked) |

## Two-tier signal (speed vs accuracy)

- **`oss` (Yosys, WSL, ~30s)** — fast screen. Relative area + logic-depth proxy.
  FF/LUT counts are inflated (SRLs map to FFs) and there is no absolute Fmax.
  Use to triage ideas quickly.
- **`vivado` (Windows, minutes)** — the real signal. Accurate LUT/FF/BRAM and
  Fmax. Currently targets Kintex-7 (`xc7k160t`, `RAM_STYLE=block`) because this
  Vivado has 7-series device support installed, not UltraScale+/vu5p. Set
  `VIVADO_PART` / `VIVADO_RAMSTYLE` to retarget once US+ is installed.

Baseline (Vivado, Kintex-7): **Fmax 150.29 MHz, 1678 LUTs, 1168 FFs, 16 BRAM36.**

## Running a session

1. Make sure the baseline is current:
   `bash flow/set_baseline.sh vivado`
2. Launch an optimizer agent (from Claude Code) with a concrete goal, e.g.
   *"Use the rtl-timing-optimizer agent to shorten the critical path of
   vhsnunzip_unbuffered."* The agent will read the timing report, edit `rtl/`,
   and run `flow/eval.sh` itself.
3. When the agent reports an accepted improvement, the orchestrator (you) keeps
   it:
   ```
   bash flow/verify.sh && git add -A && git commit -m "opt: <what changed>"
   bash flow/set_baseline.sh vivado      # new baseline for the next round
   ```
   To discard instead: `git restore rtl/`.
4. Repeat, alternating timing/area goals as desired.

## Guardrails baked in

- **Nothing incorrect survives**: `eval.sh` auto-reverts any edit that fails the
  ladder, and `sim/run.sh` bounds each run with `--stop-time` + hang detection,
  so a handshake deadlock is reported as a failure rather than a false pass.
- **Interfaces are pinned**: each sub-block has its own `*_tc` testbench checking
  its exact stream I/O, so an agent cannot silently change a module boundary.
- **Golden model is fixed truth**: agents may not edit `model/`, `tb/`, or
  `vectors/` — only `rtl/`.
