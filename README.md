# agenticRTL — vhsnunzip

An **agentic RTL-optimisation loop** applied to
[`vhsnunzip`](https://github.com/abs-tudelft/vhsnunzip), a VHDL-2008 Snappy
decompressor. You give the loop a goal ("increase throughput by 200%"). It
then improves the RTL by itself, iteration after iteration:

1. It analyses where the current best design loses throughput.
2. It plans a few different directions.
3. It runs parallel Claude coding sessions, each writing one candidate RTL
   change in its own git worktree.
4. It measures every candidate: it simulates on real Parquet data, checks
   every output byte against a frozen reference decompressor, and
   synthesises the design.
5. It keeps the best candidate and moves on.

The loop lives in [`agentic/`](agentic/README.md). The design it optimises
is in `rtl/`.

## Results

All numbers come from Vivado 2024.2 place and route on the Alveo U55C
(`xcu55c-fsvh2892-2L-e`, 4.0 ns constraint). Throughput is bytes/cycle ×
f_max. Bytes/cycle is the geometric mean over real Parquet row groups: NYC
taxi trips for training, and the TPC-H SF1 tables held out. Every output
byte is checked.

| Design | Where | B/cycle | f_max | Throughput | vs original | LUTs |
|---|---|---|---|---|---|---|
| Original `vhsnunzip_unbuffered` | starting point | 5.64 | 279.6 MHz | 1.578 GB/s | — | 1,808 |
| Loop run `hacc-real200`, iteration 84 | loop branch, Oct 7 | 16.38 | 256.1 MHz | 4.195 GB/s | +165.9% | 10,486 |
| **DSW-4**, built on the run's iteration-35 design | [`dsw4-design`](https://github.com/VatsalDixit/agenticRTL/tree/dsw4-design) | **20.60** | **251.8 MHz** | **5.187 GB/s** | **+228.7%** | 29,050 (+32 URAM) |

`hacc-real200` ran on an earlier version of this branch (commit `b3d0302`):
88 iterations, about $1,040 of model usage. DSW-4 widens the ports to
32 bytes and replaces the back end and the element parser of the loop's
iteration-35 design (2.758 GB/s), in three steps. B1, B2 and B3 are each one
commit on that branch. Its full measurement is in
`sim/dsw4/results-b3/RESULTS.md` on that branch.

Two later runs used the **blind kit** ([`fast-guard-blind`](https://github.com/VatsalDixit/agenticRTL/tree/fast-guard-blind)).
It is this loop with its documentation, tests, packing model and notes from
earlier runs removed, so that the run has to find its designs on its own:

| Run | Kit | Status | Best fully measured design |
|---|---|---|---|
| `fastguard200` | blind kit | stopped at iteration 20 | 2.287 GB/s (+45.0%), 10.47 B/cycle at 218.4 MHz, 6,504 LUTs |
| `nolearn25` | blind kit, learning step off | running (iteration 12 of 25 on Oct 8) | 1.919 GB/s (+21.6%) at iteration 9 |

## Branches

| Branch | What it is |
|---|---|
| `agentic-loop` (default) | The loop, with its documentation, tests and design notes |
| `dsw4-design` | The best design so far (+228.7%) and its measurements |
| `fast-guard-blind` | The blind kit used for the two runs above |
| `agentic-snappy` | Earlier hand-written optimisation work (July) |

## What's here

```
agentic/    The optimisation loop (see agentic/README.md)
rtl/        Synthesizable VHDL-2008: the design being optimised
tb/         Self-checking per-stage testbenches from upstream
model/      Python golden model + self-contained test-vector generator
vectors/    Pre-generated *.tv test vectors
sim/        GHDL runner for a single testbench
flow/       The original verification ladder
docs/       Attribution, upstream license, the loop's v3 design plan
```

## Running the loop

See [`agentic/README.md`](agentic/README.md) for setup. In short, it needs:

- GHDL and Yosys (the OSS CAD Suite) under WSL or Linux.
- Python 3.10+ with `claude-agent-sdk`, plus a logged-in Claude Code CLI.
- Optionally, ssh to the HACC build host for Vivado.

```bash
python agentic/setup.py                 # check tools, build stimulus, freeze the instrument
python agentic/test_kit.py              # the loop's own checks, no model needed
python agentic/loop.py --goal "increase throughput by 200%" --iters 100
python agentic/gui.py                   # live dashboard
```

[`docs/loop-v3-plan.md`](docs/loop-v3-plan.md) records the design of the
current loop and the reasons behind it.

## The design: `vhsnunzip_unbuffered`

The original is a streaming single-core Snappy decompressor. Its module
hierarchy (all in `rtl/`):

```
vhsnunzip_unbuffered
├── vhsnunzip_pipeline
│   ├── vhsnunzip_fifo, vhsnunzip_srl
│   ├── vhsnunzip_pre_decoder
│   ├── vhsnunzip_decoder, vhsnunzip_decoder_long
│   └── vhsnunzip_cmd_gen_1, vhsnunzip_cmd_gen_2
└── vhsnunzip_ram (×2)          + packages: _pkg, _int_pkg, _utils_pkg
```

`vhsnunzip_ram.sim.vhd` is used for simulation and `vhsnunzip_ram.syn.vhd`
(same entity) for synthesis.

## Manual verification (upstream testbenches)

These steps run on Linux or WSL with GHDL installed. The loop does not use
them: it has its own testbench and oracle. They are still a quick way to
check the original design stage by stage.

```bash
python3 model/gen_vectors.py            # regenerate vectors/*.tv from the golden model
bash flow/run_verify.sh                 # unit → integration → top, stops at the first failure
bash sim/run.sh vhsnunzip_unbuffered_tc # or a single testbench
```

See [`flow/README.md`](flow/README.md) for details.

## Provenance & license

The design is derived from `vhsnunzip` (ABS group, TU Delft), via the
`celeris-labs/parcore` fork. The original license (MIT/Apache, see
`docs/LICENSE.upstream`) applies to `rtl/`, `tb/` and `model/emu/`. Everything
else is original to this repo. See [`docs/ATTRIBUTION.md`](docs/ATTRIBUTION.md).
