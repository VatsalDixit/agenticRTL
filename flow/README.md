# The verification flow

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

## Stages 2 and 3 — synthesis and optimisation (in `agentic/`)

This file describes the first, manual verification ladder. Synthesis and the
optimising agents were built afterwards as the agentic loop in
[`agentic/`](../agentic/README.md): it measures every candidate with its own
frozen testbench and reference decompressor, synthesises it (Yosys on Nangate
45nm, or Vivado place and route on the HACC host), and scores it on
throughput.
