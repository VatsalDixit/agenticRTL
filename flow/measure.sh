#!/usr/bin/env bash
#
# Fitness measurement (run from Windows / Git Bash). Runs synthesis with the
# chosen tool and copies the normalized metrics to flow/metrics/latest.json.
#
#   oss     -> Yosys in WSL: fast (~30s), relative area + logic-depth proxy.
#   vivado  -> Vivado on Windows: slow (minutes), accurate area + Fmax (7-series).
#
# Usage:  bash flow/measure.sh [oss|vivado] [vivado-mode]
#           vivado-mode: full (route, accurate) [default] | fast (synth only)
# Env:    WSL_DISTRO (default Ubuntu-22.04)
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WSLROOT="/mnt${ROOT}"
DISTRO="${WSL_DISTRO:-Ubuntu-22.04}"
TOOL="${1:-oss}"
mkdir -p "$ROOT/flow/metrics"
LATEST="$ROOT/flow/metrics/latest.json"

case "$TOOL" in
  oss)
    log="$ROOT/flow/metrics/oss.log"
    if ! MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
         wsl.exe -d "$DISTRO" -- bash "$WSLROOT/syn/synth_oss.sh" >"$log" 2>&1; then
      echo "ERROR: oss synthesis failed (see $log)"; tail -15 "$log"; exit 1
    fi
    cp "$ROOT/syn/oss_build/metrics.json" "$LATEST"
    ;;
  vivado)
    MODE="${2:-full}"
    log="$ROOT/flow/metrics/vivado.log"
    if ! bash "$ROOT/syn/synth_vivado.sh" vhsnunzip_unbuffered "$MODE" >"$log" 2>&1; then
      echo "ERROR: vivado synthesis failed (see $log)"; tail -15 "$log"; exit 1
    fi
    cp "$ROOT/syn/vivado_build/vhsnunzip_unbuffered/metrics.json" "$LATEST"
    ;;
  *) echo "unknown tool '$TOOL' (use oss|vivado)"; exit 2 ;;
esac

echo "metrics -> $LATEST"
cat "$LATEST"
