#!/usr/bin/env bash
#
# Correctness gate (run from Windows / Git Bash). Delegates to the GHDL ladder
# in WSL and exits 0 iff every testbench passes. This is the hard gate every
# optimization candidate must clear before its area/timing numbers count.
#
# Usage:  bash flow/verify.sh [--seed N] [--top-only] [--regen]
# Env:    WSL_DISTRO overrides the distro (default Ubuntu-22.04).
#
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WSLROOT="/mnt${ROOT}"                 # /c/... -> /mnt/c/...
DISTRO="${WSL_DISTRO:-Ubuntu-22.04}"

out=$(MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
        wsl.exe -d "$DISTRO" -- bash "$WSLROOT/tools/verify_wsl.sh" "$@" 2>&1 | tr -d '\r')
echo "$out" | grep -E 'PASS:|FAIL:|ALL PASS|FIRST FAILURE' || true
if echo "$out" | grep -q "ALL PASS"; then
  echo "VERIFY: PASS"
  exit 0
else
  echo "VERIFY: FAIL"
  exit 1
fi
