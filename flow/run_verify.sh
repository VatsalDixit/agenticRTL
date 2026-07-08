#!/usr/bin/env bash
#
# Fast functional-verification loop for vhsnunzip_unbuffered.
#
# Regenerates test vectors (optionally with a fresh random seed / data) and runs
# the whole testbench ladder bottom-up, stopping at the first failure so the
# failing stage is localised.  This is the inner loop an optimisation agent runs
# after every RTL edit -- it is fast (seconds under GHDL) and gates correctness
# *before* any slow synthesis pass.
#
# Usage:
#   flow/run_verify.sh [--seed N] [--input FILE] [--regen] [--top-only]
#
#   --seed N     RNG seed for chunk splitting when regenerating (default: 0)
#   --input F    compress/decompress FILE instead of the built-in sample
#   --regen      force regeneration of vectors before simulating
#   --top-only   run only the top-level testbench (skip the unit ladder)
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SEED=0
INPUT=""
REGEN=0
TOP_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --seed)     SEED="$2"; REGEN=1; shift 2 ;;
    --input)    INPUT="$2"; REGEN=1; shift 2 ;;
    --regen)    REGEN=1; shift ;;
    --top-only) TOP_ONLY=1; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done

# Regenerate vectors if asked, or if none exist yet.
if [ "$REGEN" = 1 ] || ! ls "$ROOT"/vectors/*.tv >/dev/null 2>&1; then
  echo "== generating vectors (seed=$SEED${INPUT:+, input=$INPUT}) =="
  python "$ROOT/model/gen_vectors.py" ${INPUT:+"$INPUT"} --seed "$SEED"
fi

# Bottom-up ladder: a failure at a unit stage pinpoints the broken block before
# the integration/top tests muddy the picture.
if [ "$TOP_ONLY" = 1 ]; then
  LADDER=( vhsnunzip_unbuffered_tc )
else
  LADDER=(
    vhsnunzip_pre_decoder_tc
    vhsnunzip_decoder_tc
    vhsnunzip_decoder_long_tc
    vhsnunzip_cmd_gen_1_tc
    vhsnunzip_cmd_gen_2_tc
    vhsnunzip_pipeline_tc
    vhsnunzip_unbuffered_tc
  )
fi

fail=0
for tb in "${LADDER[@]}"; do
  echo
  echo "############### $tb ###############"
  if ! "$ROOT/sim/run.sh" "$tb"; then
    echo ">>> FIRST FAILURE at: $tb"
    fail=1
    break
  fi
done

echo
if [ "$fail" = 0 ]; then
  echo "ALL PASS (${#LADDER[@]} testbenches)"
else
  exit 1
fi
