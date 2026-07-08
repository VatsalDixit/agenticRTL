#!/usr/bin/env bash
#
# Accurate synthesis metrics via Vivado (Windows). Runs syn/synth_vivado.tcl and
# parses the METRIC lines into syn/vivado_build/<top>/metrics.json + a summary.
#
# Usage:  bash syn/synth_vivado.sh [TOP] [MODE]
#           TOP  default vhsnunzip_unbuffered
#           MODE full (route, accurate) [default] | fast (synth only)
# Env:    VIVADO_BAT overrides the vivado.bat path.
#
# NOTE: run from Windows (Git Bash). Vivado is a Windows install; the GHDL/Yosys
# tools live in WSL. This is the one step that runs on the Windows side.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIVADO="${VIVADO_BAT:-/c/Xilinx/Vivado/2024.1/bin/vivado.bat}"
TOP="${1:-vhsnunzip_unbuffered}"
MODE="${2:-full}"

[ -f "$VIVADO" ] || { echo "ERROR: vivado.bat not found at $VIVADO (set VIVADO_BAT)"; exit 1; }

cd "$ROOT"
OUT="syn/vivado_build/$TOP"
mkdir -p "$OUT"
LOG="$OUT/vivado.log"

echo "== Vivado synthesis ($TOP, mode=$MODE) — this takes minutes =="
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
  "$VIVADO" -mode batch -nolog -nojournal -notrace \
    -source syn/synth_vivado.tcl -tclargs "$TOP" "$MODE" 2>&1 | tee "$LOG" | grep -E "METRIC|ERROR|CRITICAL" || true

if ! grep -q "METRIC_DONE" "$LOG"; then
  echo "ERROR: Vivado did not finish (no METRIC_DONE). See $LOG"
  exit 1
fi

# Parse METRIC lines -> metrics.json + summary.txt
python - "$LOG" "$OUT/metrics.json" "$OUT/summary.txt" <<'PY'
import json, re, sys
log, jpath, spath = sys.argv[1], sys.argv[2], sys.argv[3]
m = {}
for line in open(log, errors="ignore"):
    g = re.match(r"\s*METRIC\s+(\w+)\s+(.+?)\s*$", line)
    if g:
        k, v = g.group(1), g.group(2)
        try:    m[k] = float(v) if ("." in v or k in ("wns_ns",)) else int(v)
        except ValueError: m[k] = v
data = {
    "tool": "vivado",
    "top": m.get("top"), "mode": m.get("mode"), "part": m.get("part"),
    "timing": {"period_ns": m.get("period_ns"), "wns_ns": m.get("wns_ns"),
               "fmax_mhz": m.get("fmax_mhz")},
    "area": {"luts": m.get("luts"), "lutmem": m.get("lutmem"), "ffs": m.get("ffs"),
             "carry": m.get("carry"), "bram36": m.get("bram36"),
             "bram18": m.get("bram18"), "uram": m.get("uram"), "dsp": m.get("dsp")},
}
json.dump(data, open(jpath, "w"), indent=2)
t, a = data["timing"], data["area"]
s = []
s.append(f"{data['top']} — Vivado {data['mode']} synthesis ({data['part']})")
s.append("=" * 60)
s.append(f"  Fmax        : {t['fmax_mhz']} MHz   (WNS {t['wns_ns']} ns @ {t['period_ns']} ns clk)")
s.append(f"  LUTs        : {a['luts']}")
s.append(f"  LUT as mem  : {a['lutmem']}   (SRL/distributed RAM)")
s.append(f"  Flip-flops  : {a['ffs']}")
s.append(f"  CARRY8      : {a['carry']}")
s.append(f"  Block RAM   : {a['bram36']}x RAMB36 + {a['bram18']}x RAMB18")
s.append(f"  UltraRAM    : {a['uram']}")
s.append(f"  DSP         : {a['dsp']}")
open(spath, "w", newline="\n").write("\n".join(s) + "\n")
print("\n".join(s))
PY
echo
echo "Wrote $OUT/metrics.json and $OUT/summary.txt"
