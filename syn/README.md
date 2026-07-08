# Synthesis (stage 2 — scaffolded, not yet wired to a toolchain)

This is the **gated, slow** half of the loop. It runs only after functional
verification passes, and produces the timing/area feedback the optimization
agents consume. There is **no Vivado on this machine yet**, so nothing here runs
automatically — the files are staged and ready.

## Files

- `synthesize.tcl` — upstream Vivado batch script. Creates a project, adds all
  non-`*.sim.*` VHDL, applies `constraints.xdc`, runs
  `synth_design`/`opt`/`place`/`route`, and reports timing + utilization to
  `synth_<top>/timing.log` and `synth_<top>/utilization.log`.
- `constraints.xdc` — clock + I/O constraints used for the reference numbers.

Target part: **`xcvu5p-flva2104-2-i`** (Virtex UltraScale+). Reference clock is
4 ns (250 MHz); `f_max = 1000 / (4 − WNS)`.

## Running it (when Vivado is available)

The synthesis file list must use `vhsnunzip_ram.syn.vhd` (NOT the `.sim`
variant). Adapt the `glob` in `synthesize.tcl` to this repo's `rtl/` layout, or
pass the file list explicitly. Then:

```bash
vivado -nolog -nojournal -mode batch -source syn/synthesize.tcl -tclargs vhsnunzip_unbuffered
```

Outputs to parse for the feedback signal:

- `timing.log`  → worst negative slack → `f_max`
- `utilization.log` → `CLB LUTs`, `CLB Registers`, `Block RAM Tile`, `URAM`

(The upstream `tests/synthesize.py` has the exact parsing regexes; port it here
when we wire stage 2 in.)

## Interim open-source path (no Vivado)

For rough **area** estimates without Vivado, GHDL + yosys can synthesize the
VHDL to a generic/Xilinx cell netlist:

```bash
# ghdl-yosys-plugin required
yosys -m ghdl -p 'ghdl --std=08 <rtl files> -e vhsnunzip_unbuffered; synth_xilinx; stat'
```

This gives LUT/FF/RAM cell counts — useful as a coarse area proxy for the area
agent — but **not** trustworthy timing closure. Vivado remains the source of
truth for `f_max`. Treat yosys numbers as a fast pre-filter only.
