# Attribution

This repository is a lite extraction derived from **`vhsnunzip`**, a hardware
Snappy decompressor.

- Upstream: https://github.com/abs-tudelft/vhsnunzip (ABS group, TU Delft)
- Extracted via the fork used by `celeris-labs/parcore`:
  https://github.com/lucat1/vhsnunzip

## What was copied verbatim (upstream license applies — see `LICENSE.upstream`)

- `rtl/*.vhd` — the `vhsnunzip_unbuffered` synthesizable dependency subtree
  (packages, srl, fifo, ram sim/syn, pre_decoder, decoder(+long),
  cmd_gen_1/2, pipeline, unbuffered).
- `tb/*.sim.08.vhd` — the self-checking, vhlib-free testbenches for that
  subtree.
- `model/emu/{__init__,operators,streams,utils}.py` — the Python golden model.
- `syn/synthesize.tcl`, `syn/constraints.xdc` — the Vivado synthesis flow.

## What is new / original to this repo

- `model/emu/snappy.py` — a **pure-Python raw-Snappy compressor** written to
  replace the upstream `snzip` C-binary dependency, so no C toolchain or
  submodules are needed. Emits only the 1-/2-byte copy-offset forms the model
  supports; self-checks via an independent reference decompressor.
- `model/gen_vectors.py` — self-contained test-vector generator (adapted from
  upstream `tests/test.py`, minus the snzip dependency, with a built-in sample
  and parametrized output).
- `sim/run.sh`, `flow/run_verify.sh` — GHDL runner and the verification-loop
  orchestrator.
- Documentation (`README.md`, `flow/README.md`, `syn/README.md`, this file).

## What was intentionally dropped

`libstf`, Coyote, Apache Arrow, `tpch-dbgen`, the `parcore` software/examples/
benchmarks/tools, the `vhlib` test-framework submodule (only `fifo_tc` needed
it — dropped), and the `snappy`/`snzip` C submodules.
