# Synthesis (stage 2)

This is the **gated** half of the loop: it runs after functional verification
passes and produces the area/timing feedback the optimization agents consume.

There are two flows here:

- **Open-source (works now)** — Yosys + the GHDL plugin from the OSS CAD Suite.
  Runs natively in WSL, no sudo, no Vivado. Gives real mapped **area** and a
  **relative timing** proxy. This is what the loop uses today.
- **Vivado (later)** — the `synthesize.tcl` reference flow for absolute area and
  Fmax on the real `xcvu5p` part. Staged for when Vivado is available.

## Open-source flow (Yosys) — `synth_oss.sh` + `synth_oss.ys`

```bash
bash tools/setup_oss_cad.sh     # one-time: installs GHDL+Yosys into ~/eda
bash syn/synth_oss.sh           # synth + report -> syn/oss_build/summary.txt
```

Outputs land in `syn/oss_build/`: `summary.txt` (parsed signal), `stat.txt` /
`stat.json` (mapped-cell area), `ltp.txt` (logic depth), `yosys.log`.

**Important caveats — read before trusting the numbers:**

1. **Area is a *relative* signal, not Vivado-accurate.** Yosys maps the
   addressable shift registers (`vhsnunzip_srl`) to flip-flops instead of Xilinx
   SRL primitives, so FF (and some LUT) counts are inflated versus Vivado. Use
   it for "did my change make it bigger or smaller?", not for absolute resource
   budgeting. The behavioral RAM does infer correctly to Block RAM.
2. **No absolute Fmax.** Open-source place-and-route (nextpnr) supports Lattice
   and Xilinx 7-series, **not** UltraScale+ (`xcvu5p`). So there is no timing
   closure here. `ltp` (longest register-to-register logic depth) is the
   relative timing proxy — lower is better, but it is not MHz.
3. For a **real Fmax** with open-source tools you would retarget to a 7-series
   part and add nextpnr-xilinx (+ prjxray). That changes the device; ask if you
   want this set up.

The behavioral RAM (`vhsnunzip_ram.sim.vhd`) is used for the open-source flow;
the primitive-instantiating `vhsnunzip_ram.syn.vhd` needs Xilinx unisim/unimacro
libraries that Yosys does not have.

## Vivado flow (later — absolute area/Fmax)

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
when we wire the Vivado stage in.)
