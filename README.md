# agenticRTL — vhsnunzip

A **lite, self-contained** hardware-design repository built around the
[`vhsnunzip`](https://github.com/abs-tudelft/vhsnunzip) VHDL Snappy
decompressor, intended as the substrate for an **agentic RTL-design loop**:
edit RTL → verify functionally → (once correct) synthesize → feed timing/area
back to optimization agents → repeat.

This is a stripped-down extraction of the decompressor and its verification
model. Everything not needed to build, verify, and (later) synthesize the
`vhsnunzip_unbuffered` core has been removed — no `libstf`, no Coyote, no Arrow,
no `snzip`/`snappy` C builds, no `vhlib` submodule.

## What's here

```
rtl/        Synthesizable VHDL-2008 — the vhsnunzip_unbuffered dependency subtree
tb/         Self-checking testbenches (vhlib-free), unit → integration → top
model/      Python golden model + self-contained test-vector generator
vectors/    Pre-generated *.tv test vectors (repo simulates out of the box)
sim/        GHDL runner for a single testbench
syn/        Synthesis scaffold (Vivado tcl + constraints) for the later stage
flow/       The verification-loop orchestrator + loop design notes
tools/      Docker-based GHDL runner + environment probe (zero-install path)
docs/        Attribution and upstream license
```

## The design under test: `vhsnunzip_unbuffered`

A streaming single-core Snappy decompressor. Approx. figures from upstream
(Vivado, `xcvu5p`): **~1844 LUTs, ~1066 regs, 0 BRAM, 2 URAM, ~256 MHz,
~1.5 GB/s**. Its module hierarchy (all in `rtl/`):

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
`vhsnunzip_ram.syn.vhd` (same entity) is for synthesis.

## Quick start

> **Status: verified green** — the full testbench ladder passes under GHDL
> (unit → integration → top), across multiple random seeds and multi-chunk
> inputs; and the design **synthesizes** under Yosys (open-source). Everything
> runs **natively in WSL/Linux** — no Docker, no sudo.

### One-time setup (WSL, no sudo)

Installs the open-source EDA stack (GHDL + Yosys + ghdl-yosys-plugin, i.e. the
OSS CAD Suite) into `~/eda`:

```bash
bash tools/setup_oss_cad.sh
```

### Functional verification (native GHDL)

```bash
bash tools/verify_wsl.sh              # whole ladder (stops at first failure)
bash tools/verify_wsl.sh --seed 7     # regenerate vectors with a new seed (fuzz)
bash tools/verify_wsl.sh --top-only   # just the top-level testbench
```

`verify_wsl.sh` sources the OSS CAD environment, then runs `flow/run_verify.sh`.
If you have the environment sourced already (`source ~/eda/oss-cad-suite/environment`),
call `bash flow/run_verify.sh` or `bash sim/run.sh <tb>` directly.

### Synthesis — area + relative timing (open-source)

```bash
bash syn/synth_oss.sh                 # -> syn/oss_build/summary.txt
```

Reports mapped area (LUT/FF/CARRY/BRAM) and a relative timing proxy (logic
depth). See caveats in [syn/README.md](syn/README.md): area is a *relative*
signal (Yosys maps SRLs to FFs, inflating counts vs Vivado), and open-source
tools cannot produce an absolute Fmax for UltraScale+ — Vivado remains the
source of truth for absolute numbers on `xcvu5p`.

> _Optional Docker alternative:_ `bash tools/run_sim_docker.sh` runs the sim
> ladder inside the `ghdl/ghdl` image if you ever want a zero-install check on
> a machine without the suite. Not needed for the normal WSL flow.

`gen_vectors.py` runs the Python golden model, self-verifies it, and serializes
the expected per-stage stream transfers to `vectors/*.tv`. The VHDL testbenches
read those back and assert against them. Regenerate with a new seed or your own
input to fuzz:

```bash
python3 model/gen_vectors.py --seed 7
python3 model/gen_vectors.py path/to/your/file --min 1024 --max 65536 --max-prob 0.3

`gen_vectors.py` runs the Python golden model, self-verifies it, and serializes
the expected per-stage stream transfers to `vectors/*.tv`. The VHDL testbenches
read those back and assert against them. Regenerate with a new seed or your own
input to fuzz:

```bash
python3 model/gen_vectors.py --seed 7
python3 model/gen_vectors.py path/to/your/file --min 1024 --max 65536 --max-prob 0.3
```

## Verification-first loop

Per the design goal, the loop runs **functional verification first** (fast,
seconds) and only kicks off **synthesis** (slow, minutes–hours) once the design
is functionally correct. See [`flow/README.md`](flow/README.md) for the full
loop design and where the timing/area optimization subagents plug in.

## Synthesis (later stage)

No Vivado is wired up yet. `syn/` holds the Vivado tcl + constraints from
upstream (`xcvu5p-flva2104-2-i`) ready for when a toolchain is available; see
[`syn/README.md`](syn/README.md) for the plan, including an open-source
(GHDL + yosys) interim area-estimate path.

## Provenance & license

Derived from `vhsnunzip` (ABS group, TU Delft) via the `celeris-labs/parcore`
fork. Original license (MIT/Apache — see `docs/LICENSE.upstream`) applies to all
`rtl/`, `tb/`, and `model/emu/` files. New glue (`model/gen_vectors.py`,
`model/emu/snappy.py`, `sim/`, `flow/`, docs) is original to this repo. See
[`docs/ATTRIBUTION.md`](docs/ATTRIBUTION.md).
