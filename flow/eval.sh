#!/usr/bin/env bash
#
# Evaluate the current (edited) RTL working tree as an optimization candidate.
# This is the safe try/measure/report harness the optimizer agents call after
# making an RTL edit.
#
#   1. CORRECTNESS GATE  — flow/verify.sh (GHDL ladder). If it fails, the edit
#      is rejected and rtl/ is auto-reverted to HEAD. Nothing broken survives.
#   2. MEASURE           — flow/measure.sh <tool> -> flow/metrics/latest.json
#   3. COMPARE           — latest vs flow/metrics/baseline.json, print deltas +
#      a recommendation for the stated goal.
#
# On correctness PASS the edit is LEFT IN PLACE (working tree dirty) for the
# orchestrator to accept (commit + `flow/set_baseline.sh`) or discard
# (`git restore rtl/`). On correctness FAIL the edit is reverted automatically.
#
# Usage:  bash flow/eval.sh [oss|vivado] [--goal timing|area]
#
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TOOL="oss"; GOAL="timing"
while [ $# -gt 0 ]; do
  case "$1" in
    oss|vivado) TOOL="$1"; shift ;;
    --goal) GOAL="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

echo "### 1. correctness gate ###"
if ! bash flow/verify.sh; then
  echo ">>> REJECT: candidate is INCORRECT. Reverting rtl/ to HEAD."
  git restore rtl/ 2>/dev/null || git checkout -- rtl/
  exit 1
fi

echo
echo "### 2. measure ($TOOL) ###"
bash flow/measure.sh "$TOOL" >/dev/null
echo "   metrics written to flow/metrics/latest.json"

echo
echo "### 3. compare vs baseline (goal: $GOAL) ###"
BASELINE="flow/metrics/baseline.$TOOL.json"
if [ ! -f "$BASELINE" ]; then
  echo "   no $TOOL baseline yet — run 'bash flow/set_baseline.sh $TOOL' on the"
  echo "   committed design first. Showing raw metrics:"
  cat flow/metrics/latest.json
  exit 0
fi

python - "$BASELINE" flow/metrics/latest.json "$GOAL" <<'PY'
import json, sys
base = json.load(open(sys.argv[1])); cur = json.load(open(sys.argv[2])); goal = sys.argv[3]
ba, ca = base.get("area", {}), cur.get("area", {})
bt, ct = base.get("timing", {}), cur.get("timing", {})

def row(name, b, c, lower_better=True):
    if b is None or c is None:
        print(f"  {name:14s}  {str(b):>10}  {str(c):>10}     n/a"); return 0
    d = c - b
    better = (d < 0) if lower_better else (d > 0)
    tag = "better" if (d != 0 and better) else ("worse" if d != 0 else "same")
    sign = "+" if d > 0 else ""
    print(f"  {name:14s}  {b:>10}  {c:>10}   {sign}{d:>6}  {tag}")
    return d

print(f"  {'metric':14s}  {'baseline':>10}  {'candidate':>10}   {'delta':>7}")
print("  " + "-"*52)
d_lut = row("LUTs", ba.get("luts"), ca.get("luts"))
d_ff  = row("FFs", ba.get("ffs"), ca.get("ffs"))
row("CARRY", ba.get("carry"), ca.get("carry"))
row("BRAM36", ba.get("bram36"), ca.get("bram36"))
row("UltraRAM", ba.get("uram"), ca.get("uram"))

# Timing metric depends on tool.
if "fmax_mhz" in bt and bt.get("fmax_mhz") is not None:
    d_t = row("Fmax(MHz)", bt.get("fmax_mhz"), ct.get("fmax_mhz"), lower_better=False)
    timing_better = d_t > 0
elif "ltp_depth" in bt:
    d_t = row("logic-depth", bt.get("ltp_depth"), ct.get("ltp_depth"))
    timing_better = d_t < 0
else:
    d_t = 0; timing_better = False

area_better = (d_lut + d_ff) < 0
print()
print("  VERDICT (correct=yes):")
if goal == "timing":
    if timing_better and (d_lut + d_ff) <= 0.05*((ba.get('luts',0)+ba.get('ffs',0)) or 1):
        print("    ACCEPT — timing improved without meaningful area cost.")
    elif timing_better:
        print("    CONSIDER — timing improved but area grew; weigh the trade-off.")
    else:
        print("    REJECT (goal not met) — timing did not improve.")
else:  # area
    if area_better and not (d_t and not timing_better):
        print("    ACCEPT — area reduced without hurting timing.")
    elif area_better:
        print("    CONSIDER — area reduced but timing regressed; weigh the trade-off.")
    else:
        print("    REJECT (goal not met) — area did not reduce.")
print()
print("  If ACCEPT: git add -A && git commit, then 'bash flow/set_baseline.sh %s'." % ("oss"))
print("  If REJECT: git restore rtl/")
PY
