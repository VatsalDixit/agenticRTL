#!/usr/bin/env bash
#
# Run the functional-verification ladder on NATIVE GHDL from the OSS CAD Suite
# (no Docker). Sources the suite environment, then delegates to flow/run_verify.sh.
#
# Usage:  bash tools/verify_wsl.sh [--seed N] [--input FILE] [--regen] [--top-only]
# Env:    OSS_CAD_ENV overrides the environment file path.
#
set -euo pipefail
ENVF="${OSS_CAD_ENV:-$HOME/eda/oss-cad-suite/environment}"
if [ ! -f "$ENVF" ]; then
  echo "ERROR: OSS CAD Suite not found at $ENVF — run tools/setup_oss_cad.sh"
  exit 1
fi
# shellcheck disable=SC1090
source "$ENVF"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec bash flow/run_verify.sh "$@"
