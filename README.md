# agenticRTL — vhsnunzip

A **lite, self-contained** hardware-design repository built around the
[`vhsnunzip`](https://github.com/abs-tudelft/vhsnunzip) VHDL Snappy
decompressor, and an **agentic RTL-design loop** on top of it:
edit RTL → verify functionally → synthesize → feed timing/area back to
optimization agents → repeat.

Everything not needed to build, verify, synthesize, and optimize the
`vhsnunzip_unbuffered` core was removed — no `libstf`, Coyote, Arrow, `vhlib`,
or `snzip`/`snappy` C builds.

## What's here

```
rtl/             Synthesizable VHDL-2008 — the vhsnunzip_unbuffered subtree
tb/              Self-checking testbenches (vhlib-free), unit → integration → top
model/           Python golden model + self-contained test-vector generator
vectors/         Pre-generated *.tv test vectors (repo simulates out of the box)
sim/             GHDL runner for a single testbench
syn/             Synthesis: open-source (Yosys) + Vivado flows -> area/timing
flow/            Verification + optimization loop (verify/measure/eval + docs)
tools/           Toolchain setup (OSS CAD Suite) + native-WSL / Docker runners
.claude/agents/  Optimizer agent specs (rtl-timing / rtl-area)
docs/            Attribution and upstream license
```

## The three-stage loop

1. **Verify** (fast, every edit) — GHDL testbench ladder, native WSL.
2. **Synthesize** (area/timing) — Yosys (fast, relative) or Vivado (accurate).
3. **Optimize** (agent-driven) — timing/area agents edit `rtl/`, gated by verify.

```
edit rtl/ ─▶ flow/eval.sh ─┬─ 1. verify  (GHDL ladder; auto-revert if incorrect)
                           ├─ 2. measure (Yosys or Vivado → metrics.json)
                           └─ 3. compare (vs baseline → accept / reject verdict)
```

## Toolchain (where things run)

- **Source + git + Vivado → Windows** (`C:\Xilinx\Vivado\2024.1`).
- **GHDL + Yosys → WSL** (OSS CAD Suite in `~/eda`, installed no-sudo).
- Nothing EDA-related installed on Windows besides Vivado; no Docker required.

One-time setup of the open-source tools:

```bash
bash tools/setup_oss_cad.sh          # installs GHDL + Yosys into ~/eda (WSL)
```

## Quick start

```bash
# 1. Verify correctness (GHDL, native WSL) — the full ladder
bash tools/verify_wsl.sh
bash tools/verify_wsl.sh --seed 7          # regenerate vectors + fuzz

# 2a. Synthesis — fast open-source signal (area + relative logic depth)
bash syn/synth_oss.sh                      # -> syn/oss_build/summary.txt

# 2b. Synthesis — accurate Vivado signal (Fmax + area)
bash syn/synth_vivado.sh                   # -> syn/vivado_build/.../summary.txt

# 3. Optimization loop: evaluate an RTL edit end-to-end
bash flow/set_baseline.sh vivado           # record the reference metrics once
bash flow/eval.sh vivado --goal timing     # verify + measure + verdict vs baseline
```

The Python golden model (`model/`) generates and self-verifies the `*.tv`
vectors that the testbenches assert against; regenerate with a new seed or your
own input to fuzz (`python3 model/gen_vectors.py --seed N` /
`python3 model/gen_vectors.py FILE --min 1024 --max 65536 --max-prob 0.3`).

## The design under test: `vhsnunzip_unbuffered`

A streaming single-core Snappy decompressor.

```
vhsnunzip_unbuffered
├── vhsnunzip_pipeline
│   ├── vhsnunzip_fifo, vhsnunzip_srl
│   ├── vhsnunzip_pre_decoder
│   ├── vhsnunzip_decoder, vhsnunzip_decoder_long
│   └── vhsnunzip_cmd_gen_1, vhsnunzip_cmd_gen_2
└── vhsnunzip_ram (×2)          + packages: _pkg, _int_pkg, _utils_pkg
```

Each stage has a matching self-checking testbench, so a failure localizes to a
specific block. `vhsnunzip_ram.sim.vhd` is used for simulation;
`vhsnunzip_ram.syn.vhd` (same entity, Xilinx primitives) is for synthesis.

**Reference (upstream, Vivado `xcvu5p`):** ~1844 LUTs, ~1066 regs, 2 URAM,
~256 MHz.
**This repo's Vivado baseline (`xc7k160t`, `RAM_STYLE=block`):** 150.29 MHz,
1678 LUTs, 1168 FFs, 16 BRAM36. (Only 7-series device support is installed;
set `VIVADO_PART`/`VIVADO_RAMSTYLE` to retarget to `xcvu5p` when UltraScale+
support is added.)

## Loop details

- **Verification** — [flow/README.md](flow/README.md)
- **Synthesis** (open-source + Vivado, caveats) — [syn/README.md](syn/README.md)
- **Optimization agents** (design + how to run) — [flow/OPTIMIZATION.md](flow/OPTIMIZATION.md)

## Provenance & license

Derived from `vhsnunzip` (ABS group, TU Delft) via the `celeris-labs/parcore`
fork. Upstream license (see `docs/LICENSE.upstream`) applies to all `rtl/`,
`tb/`, and `model/emu/` files. New glue (`model/gen_vectors.py`,
`model/emu/snappy.py`, `sim/`, `syn/`, `flow/`, `tools/`, `.claude/agents/`,
docs) is original to this repo. See [docs/ATTRIBUTION.md](docs/ATTRIBUTION.md).
