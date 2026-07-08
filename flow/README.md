# The agentic RTL loop

This directory holds the orchestration for the design loop. The guiding
principle is **cheap signal first**: functional correctness is verified on every
iteration (seconds), and expensive synthesis for timing/area runs only after the
design is known-correct.

```
                 ┌─────────────────────────────────────────────┐
                 │  1. VERIFY  (fast, every iteration)          │
   edit RTL ───▶ │  gen_vectors.py → GHDL testbench ladder      │
                 │  unit → integration → top, stop at 1st fail  │
                 └───────────────┬─────────────────────────────┘
                                 │ correct?
                        no ◀──────┤──────▶ yes
                        │                   │
                 (fix RTL, loop)            ▼
                                 ┌─────────────────────────────────────┐
                                 │  2. SYNTHESIZE  (slow, gated)        │
                                 │  Vivado → timing.log, utilization.log│
                                 │  parse WNS→f_max, LUT/Reg/BRAM/URAM  │
                                 └───────────────┬─────────────────────┘
                                                 ▼
                                 ┌─────────────────────────────────────┐
                                 │  3. OPTIMIZE  (subagents, later)     │
                                 │  timing agent  ▸ critical-path edits │
                                 │  area agent    ▸ resource reduction  │
                                 │  both re-enter step 1 to re-verify   │
                                 └─────────────────────────────────────┘
```

## Stage 1 — functional verification (implemented)

`flow/run_verify.sh` is the inner loop:

- Regenerates `vectors/*.tv` from the Python golden model (`model/gen_vectors.py`)
  — optionally with a new `--seed` or `--input` to fuzz.
- Runs the testbench ladder bottom-up via `sim/run.sh`, **stopping at the first
  failure** so the broken stage is pinpointed:

  | stage | testbench | checks |
  |-------|-----------|--------|
  | unit  | `pre_decoder_tc` | byte pre-decoder framing |
  | unit  | `decoder_tc`, `decoder_long_tc` | element decoding |
  | unit  | `cmd_gen_1_tc`, `cmd_gen_2_tc` | command generation |
  | integ | `pipeline_tc` | full datapath end-to-end |
  | top   | `unbuffered_tc` | streaming core, compressed→decompressed |

The golden model (`model/emu/`) is the reference: any RTL change must keep the
hardware output matching the model's serialized `*.tv` transfers.

## Stage 2 — synthesis (open-source flow working; see `syn/`)

Gated behind an all-pass from stage 1. Produces the area/timing feedback signal
via Yosys (open-source), natively in WSL:

```bash
bash syn/synth_oss.sh     # -> syn/oss_build/summary.txt
```

`summary.txt` reports mapped area (LUT/FF/CARRY/BRAM) and a relative timing
proxy (`ltp`, longest register-to-register logic depth). **Caveats** (see
`syn/README.md`): area is a *relative* signal (Yosys maps SRLs to FFs, inflating
counts vs Vivado), and there is no absolute Fmax on open-source UltraScale+.
Vivado (`syn/synthesize.tcl`) remains the source of truth for absolute
area/Fmax on `xcvu5p` when available.

## Stage 3 — optimization agents (implemented)

Specialized agents edit `rtl/` to improve timing or area; the harness verifies
and measures every candidate, auto-reverting anything incorrect. Full design and
usage in **[OPTIMIZATION.md](OPTIMIZATION.md)**.

- **`.claude/agents/rtl-timing-optimizer`** — shortens the critical path (higher
  Fmax / lower WNS).
- **`.claude/agents/rtl-area-optimizer`** — reduces LUT/FF/BRAM.

Both drive `flow/eval.sh <oss|vivado> --goal <timing|area>`, which runs the
stage-1 ladder as a hard gate, measures with the chosen tool, and prints a
verdict against `flow/metrics/baseline.json`. Vivado gives the accurate signal
(currently Kintex-7, since UltraScale+ device support isn't installed);
Yosys is the fast screen.
