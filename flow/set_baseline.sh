#!/usr/bin/env bash
#
# Record the current (committed) design's metrics as the optimization baseline
# that flow/eval.sh compares candidates against. Run this once at the start and
# again each time you accept (commit) an improvement.
#
# Usage:  bash flow/set_baseline.sh [oss|vivado] [vivado-mode]
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TOOL="${1:-oss}"
bash flow/measure.sh "$@" >/dev/null
cp flow/metrics/latest.json "flow/metrics/baseline.$TOOL.json"
echo "baseline set from $TOOL -> flow/metrics/baseline.$TOOL.json:"
cat "flow/metrics/baseline.$TOOL.json"
