#!/usr/bin/env bash
#
# Open-source synthesis + area / relative-timing report for vhsnunzip_unbuffered,
# using Yosys + the GHDL plugin from the OSS CAD Suite. No Vivado, no sudo.
#
# Usage:  bash syn/synth_oss.sh
# Env:    OSS_CAD_ENV overrides the path to the OSS CAD Suite 'environment' file
#         (default ~/eda/oss-cad-suite/environment). Install with
#         tools/setup_oss_cad.sh.
#
# Outputs (syn/oss_build/):
#   yosys.log   full synthesis transcript
#   stat.txt    mapped-cell area, human-readable
#   stat.json   mapped-cell area, machine-readable (parsed below)
#   ltp.txt     longest register-to-register logic depth (relative timing proxy)
#   summary.txt parsed one-screen summary (the loop's feedback signal)
#
# CAVEATS (open-source flow, read syn/README.md):
#   * Area is a RELATIVE signal. Yosys maps the addressable shift registers
#     (vhsnunzip_srl) to flip-flops, not Xilinx SRL primitives, so FF/LUT counts
#     are inflated vs Vivado. Good for "did my change get bigger/smaller?",
#     not for absolute resource numbers.
#   * No absolute Fmax: open-source P&R does not support UltraScale+. `ltp`
#     (logic depth) is the relative timing proxy. Use Vivado for real MHz.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVF="${OSS_CAD_ENV:-$HOME/eda/oss-cad-suite/environment}"
if [ ! -f "$ENVF" ]; then
  echo "ERROR: OSS CAD Suite not found at $ENVF"
  echo "  Install it first: bash tools/setup_oss_cad.sh"
  exit 1
fi
# shellcheck disable=SC1090
source "$ENVF"

cd "$ROOT"
mkdir -p syn/oss_build

echo "== yosys synthesis (xcup) =="
yosys -m ghdl -s syn/synth_oss.ys 2>&1 | tee syn/oss_build/yosys.log >/dev/null
echo "   done ($(grep -c '' syn/oss_build/yosys.log) log lines)"

# ---- Parse a compact summary (the loop's feedback signal). ----
python3 - syn/oss_build/stat.json syn/oss_build/ltp.txt syn/oss_build/summary.txt <<'PY'
import json, re, sys

stat_path, ltp_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

stat = json.load(open(stat_path))
mods = stat.get("modules", {})
top = mods.get("vhsnunzip_unbuffered") or next(iter(mods.values()))
cells = top.get("num_cells_by_type", top.get("num_cells_by_type", {}))

def total(pred):
    return sum(v for k, v in cells.items() if pred(k))

lut   = total(lambda k: re.fullmatch(r"LUT[1-6]", k))
ff    = total(lambda k: k.startswith("FD"))
carry = total(lambda k: k.startswith("CARRY"))
mux   = total(lambda k: k.startswith("MUXF"))
bram  = total(lambda k: k.startswith("RAMB"))
uram  = total(lambda k: k.startswith("URAM"))
dsp   = total(lambda k: k.startswith("DSP"))
srl   = total(lambda k: k.startswith(("SRL", "RAMD", "RAMS")))

ltp_txt = open(ltp_path).read()
depths = [int(x) for x in re.findall(r"length=(\d+)", ltp_txt)]
depth = max(depths) if depths else None

lines = []
lines.append("vhsnunzip_unbuffered - open-source synthesis (Yosys, xcup cell lib)")
lines.append("=" * 62)
lines.append("AREA (relative signal; SRLs map to FFs -> inflated vs Vivado):")
lines.append(f"  LUTs (logic)      : {lut}")
lines.append(f"  LUT-RAM / SRL     : {srl}")
lines.append(f"  Flip-flops (FD*)  : {ff}")
lines.append(f"  CARRY             : {carry}")
lines.append(f"  Wide MUX (F7/8/9) : {mux}")
lines.append(f"  Block RAM (RAMB36): {bram}")
lines.append(f"  UltraRAM          : {uram}")
lines.append(f"  DSP               : {dsp}")
lines.append("")
lines.append("TIMING (relative proxy; no absolute Fmax on open-source xcup):")
lines.append(f"  Longest reg->reg logic depth (ltp): {depth if depth is not None else 'n/a'}")
lines.append("")
lines.append("For absolute area/Fmax on xcvu5p, use Vivado (syn/synthesize.tcl).")

text = "\n".join(lines) + "\n"
open(out_path, "w", newline="\n").write(text)
print(text)
PY

echo "Summary written to syn/oss_build/summary.txt"
