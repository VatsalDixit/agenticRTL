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

## What's where

### Where to start

To follow the work without reading everything, these five files are enough:

1. [`agentic/README.md`](agentic/README.md): how the loop works, what it
   costs, and what was learned the hard way.
2. [`agentic/loop.py`](agentic/loop.py): the loop itself. The docstring at
   the top lists the seven steps of one iteration.
3. [`agentic/measure.py`](agentic/measure.py): how a candidate is judged
   (correctness, bytes/cycle, f_max, area).
4. [`agentic/propose.py`](agentic/propose.py): what the Claude coding
   sessions are told, and what they may and may not do.
5. [`docs/loop-v3-plan.md`](docs/loop-v3-plan.md): the design of the current
   loop and the reasons behind each change.

### Top level

| Path | What it is |
|---|---|
| [`agentic/`](agentic/) | **The optimisation loop.** Everything below in "The loop" |
| [`rtl/`](rtl/) | The original `vhsnunzip` design, which is the loop's starting point |
| [`docs/`](docs/) | The loop's v3 design plan, attribution, upstream license |
| [`tb/`](tb/), [`vectors/`](vectors/), [`model/`](model/), [`flow/`](flow/), [`sim/`](sim/) | **Upstream's original tests, not used by the loop.** See the last section |

### The loop (`agentic/`)

The loop's main files:

| File | Role |
|---|---|
| [`loop.py`](agentic/loop.py) | The main loop: analyse, plan, write, measure, select, learn, record |
| [`propose.py`](agentic/propose.py) | Plans the directions and runs the Claude coding sessions, one git worktree each |
| [`measure.py`](agentic/measure.py) | Measures a design: simulation on every draw, then synthesis |
| [`analyse.py`](agentic/analyse.py) | Finds where throughput is lost: the rate of each stage against its ceiling |
| [`learn.py`](agentic/learn.py), [`skills.py`](agentic/skills.py), [`skills.json`](agentic/skills.json) | The skill library: what has worked, what has not, updated after every iteration |
| [`guide.py`](agentic/guide.py) | A description of the current design generated from its RTL, given to every session |
| [`packmodel.py`](agentic/packmodel.py) | Predicts the bytes/cycle a decoder shape allows on the real data, before anyone builds it |
| [`config.json`](agentic/config.json) | Settings: model, number of candidates, budgets, synthesis backend, FPGA part |

Correctness and the measuring instrument (frozen, so a candidate cannot change how it is judged):

| File | Role |
|---|---|
| [`oracle.py`](agentic/oracle.py) | Compares every output byte against the reference decompressor |
| [`ref/snappy.py`](agentic/ref/snappy.py) | The reference Snappy compressor and decompressor |
| [`ref/parquet_pages.py`](agentic/ref/parquet_pages.py) | Extracts the Snappy pages from a Parquet file |
| [`stim.py`](agentic/stim.py), [`make_data.py`](agentic/make_data.py) | The scoring data: real Parquet row groups (NYC taxi, TPC-H SF1) |
| [`tb/vhsnunzip_perf_tc.sim.08.vhd`](agentic/tb/vhsnunzip_perf_tc.sim.08.vhd) | The throughput testbench, which works with any port widths |
| [`probe.py`](agentic/probe.py) | Counts, in simulation, how often each internal stage moves, waits or idles |
| [`freeze.py`](agentic/freeze.py), [`frozen.json`](agentic/frozen.json) | Hashes of the files above, checked before every scoring pass |
| [`check.py`](agentic/check.py) | The one command a coding session may run: "does my design still work?" |

Synthesis ([`agentic/syn/`](agentic/syn/)):

| File | Role |
|---|---|
| [`hacc.py`](agentic/hacc.py), [`syn/vivado.tcl`](agentic/syn/vivado.tcl), [`syn/ram_xilinx.vhd`](agentic/syn/ram_xilinx.vhd) | Vivado place and route on the HACC host (Alveo U55C) over ssh |
| [`syn/synth.sh`](agentic/syn/synth.sh), [`syn/ram_stub.vhd`](agentic/syn/ram_stub.vhd), [`syn/lib/`](agentic/syn/lib/), [`syn/get_lib.sh`](agentic/syn/get_lib.sh) | Open-source alternative: GHDL + Yosys on the Nangate 45nm cell library |
| [`syn/sim_draws.sh`](agentic/syn/sim_draws.sh), [`syn/unit.sh`](agentic/syn/unit.sh) | GHDL simulation scripts |

Running and watching a run:

| File | Role |
|---|---|
| [`setup.py`](agentic/setup.py) | Checks the machine and prepares everything, once |
| [`gui.py`](agentic/gui.py), [`report.py`](agentic/report.py) | Live dashboard window, and status.json / report.html |
| [`hacc_tmux.sh`](agentic/hacc_tmux.sh), [`mirror.py`](agentic/mirror.py) | Running the loop on the HACC host, and watching it from a laptop |
| [`compare.py`](agentic/compare.py) | Compares two runs, including cost per percent gained |
| [`tools.py`](agentic/tools.py) | Shared helpers: config, timeouts, git, logging |

Tests (no model or simulator needed):

| File | Role |
|---|---|
| [`test_kit.py`](agentic/test_kit.py) | The loop's checks: run with `python agentic/test_kit.py` |
| [`kitcheck_packmodel.py`](agentic/kitcheck_packmodel.py), [`kitcheck_probe.py`](agentic/kitcheck_probe.py) | Checks for the packing model and the probe, run by `test_kit.py` |

### The best design (on the [`dsw4-design`](https://github.com/VatsalDixit/agenticRTL/tree/dsw4-design) branch)

| Path | What it is |
|---|---|
| [`sim/dsw4/results-b3/RESULTS.md`](https://github.com/VatsalDixit/agenticRTL/blob/dsw4-design/sim/dsw4/results-b3/RESULTS.md) | **The measured result**: per-table bytes/cycle, f_max, LUTs, worst path |
| [`rtl/vhsnunzip_core.vhd`](https://github.com/VatsalDixit/agenticRTL/blob/dsw4-design/rtl/vhsnunzip_core.vhd) | The DSW-4 core, which replaces the original pipeline |
| [`rtl/vhsnunzip_parser.vhd`](https://github.com/VatsalDixit/agenticRTL/blob/dsw4-design/rtl/vhsnunzip_parser.vhd) with `blkrd`, `pt`, `walker` | The table parser: finds four Snappy elements per cycle |
| [`rtl/vhsnunzip_writer.vhd`](https://github.com/VatsalDixit/agenticRTL/blob/dsw4-design/rtl/vhsnunzip_writer.vhd), `elq`, `agen`, `dpath` | The back end: element queue, command issue, address generation, datapath |
| [`rtl/vhsnunzip_cbuf.vhd`](https://github.com/VatsalDixit/agenticRTL/blob/dsw4-design/rtl/vhsnunzip_cbuf.vhd), `cofifo`, `defifo` | Input buffer and the 32-byte input/output FIFOs |
| [`rtl/vhsnunzip_dsw4_pkg.vhd`](https://github.com/VatsalDixit/agenticRTL/blob/dsw4-design/rtl/vhsnunzip_dsw4_pkg.vhd) | Shared records and constants |
| [`sim/dsw4/`](https://github.com/VatsalDixit/agenticRTL/tree/dsw4-design/sim/dsw4) | Unit testbenches, mutation checks and regression runs, one folder per block (`front`, `parser`, `writer`, `dpath`, `core`, `regress`) |

On that branch, `vhsnunzip_pipeline`, `pre_decoder`, `decoder`,
`decoder_long`, `cmd_gen_1` and `cmd_gen_2` are the loop's iteration-35
design. DSW-4 no longer instantiates them.

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

## Upstream's original tests, not used by the loop

`tb/`, `vectors/`, `model/`, `flow/` and `sim/` came with the upstream
`vhsnunzip` code. They check each pipeline stage cycle by cycle against a
model of the original design. They show the starting design was verified
before the loop began.

The loop does not use them. Its changes alter the internal timing, so these
tests fail even when the output is correct. The loop has its own testbench
and oracle in `agentic/` (see above), which check only the bytes that come
out of the design.

| Path | What it is |
|---|---|
| [`tb/`](tb/) | Seven self-checking testbenches, one per pipeline stage plus the top |
| [`model/`](model/) | Python model of the original design; `gen_vectors.py` writes the test data |
| [`vectors/`](vectors/) | The test data it generated |
| [`flow/run_verify.sh`](flow/run_verify.sh), [`sim/run.sh`](sim/run.sh) | Run the testbenches, from the bottom stage up |

To run them, you need Linux or WSL with GHDL installed:

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
