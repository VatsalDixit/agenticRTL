#!/usr/bin/env bash
#
# Compile and run one vhsnunzip testbench under GHDL (VHDL-2008) and report
# PASS/FAIL.  Designed for Linux / WSL where GHDL lives; it is the fast inner
# step of the functional-verification loop.
#
# Usage:
#   sim/run.sh [TESTBENCH_ENTITY]
#
#   TESTBENCH_ENTITY defaults to vhsnunzip_unbuffered_tc.  Valid entities
#   (each has a matching tb/<entity>.sim.08.vhd):
#
#     vhsnunzip_pre_decoder_tc      unit: byte pre-decoder
#     vhsnunzip_decoder_tc          unit: element decoder
#     vhsnunzip_decoder_long_tc     unit: long-element decoder
#     vhsnunzip_cmd_gen_1_tc        unit: command generator stage 1
#     vhsnunzip_cmd_gen_2_tc        unit: command generator stage 2
#     vhsnunzip_pipeline_tc         integration: full datapath pipeline
#     vhsnunzip_unbuffered_tc       top: streaming single-core (default target)
#
# Requires: ghdl on PATH, and generated vectors in ../vectors (run
#           `python model/gen_vectors.py` first).
#
set -euo pipefail

TB="${1:-vhsnunzip_unbuffered_tc}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TB_FILE="$ROOT/tb/${TB}.sim.08.vhd"
BUILD="$ROOT/sim/work/${TB}"

if ! command -v ghdl >/dev/null 2>&1; then
  echo "ERROR: ghdl not found on PATH."
  echo "  Install (Debian/Ubuntu/WSL): sudo apt-get install -y ghdl"
  exit 127
fi
if [ ! -f "$TB_FILE" ]; then
  echo "ERROR: no testbench file for '$TB' (expected $TB_FILE)"
  exit 2
fi
if ! ls "$ROOT"/vectors/*.tv >/dev/null 2>&1; then
  echo "ERROR: no test vectors in $ROOT/vectors."
  echo "  Generate them first: python model/gen_vectors.py"
  exit 3
fi

# RTL analysed bottom-up (packages first, then leaf modules, then tops).
# NOTE: the *simulation* RAM model is used here (rtl/vhsnunzip_ram.sim.vhd);
# vhsnunzip_ram.syn.vhd shares the same entity and is for synthesis only.
RTL=(
  rtl/vhsnunzip_utils_pkg.vhd
  rtl/vhsnunzip_pkg.vhd
  rtl/vhsnunzip_int_pkg.vhd
  rtl/vhsnunzip_ram.sim.vhd
  rtl/vhsnunzip_srl.vhd
  rtl/vhsnunzip_fifo.vhd
  rtl/vhsnunzip_pre_decoder.vhd
  rtl/vhsnunzip_decoder.vhd
  rtl/vhsnunzip_decoder_long.vhd
  rtl/vhsnunzip_cmd_gen_1.vhd
  rtl/vhsnunzip_cmd_gen_2.vhd
  rtl/vhsnunzip_pipeline.vhd
  rtl/vhsnunzip_unbuffered.vhd
)

rm -rf "$BUILD"
mkdir -p "$BUILD"
# The testbenches open the *.tv files from the current working directory.
cp "$ROOT"/vectors/*.tv "$BUILD"/
cd "$BUILD"

STD=--std=08

echo "== analyse =="
for f in "${RTL[@]}"; do
  ghdl -a $STD "$ROOT/$f"
done
ghdl -a $STD "$TB_FILE"

echo "== elaborate =="
ghdl -e $STD "$TB"

echo "== run: $TB =="
# A failing assertion in the testbench has severity 'failure' and stops the
# simulation with a non-zero exit code.  --ieee-asserts=disable silences the
# harmless numeric_std metavalue warnings from don't-care ('-') comparisons.
#
# --stop-time bounds the run so a broken edit that deadlocks the handshake
# (valid/ready never fires) can't hang forever. A correct testbench ends well
# before this via its `done` signal. Reaching the stop-time means the design
# HUNG -- GHDL exits 0 in that case, so we detect the stop-time message and
# treat it as a failure (otherwise a hang would look like a pass).
STOP="--stop-time=20ms"
rc=0
ghdl -r $STD "$TB" --ieee-asserts=disable $STOP >run.log 2>&1 || rc=$?
sed -n '1,50p' run.log
if grep -q "stop-time" run.log; then
  echo "FAIL: $TB (HANG — hit $STOP without finishing; likely a handshake deadlock)"
  exit 1
fi
if [ $rc -ne 0 ]; then
  echo "FAIL: $TB (assertion/error, exit $rc)"
  exit 1
fi
echo "PASS: $TB"
