#!/usr/bin/env bash
#
# Dump a waveform for one testbench using NATIVE GHDL from the OSS CAD Suite,
# then open it in GTKWave (also bundled in the suite). No Docker. Requires
# Windows 11 WSLg (built in) for the GTKWave GUI to display.
#
# Usage:
#   bash tools/wave_wsl.sh [TESTBENCH_ENTITY] [FORMAT] [--no-gui]
#
#   TESTBENCH_ENTITY  defaults to vhsnunzip_pipeline_tc (has the most
#                     interesting internal dbg_* streams to watch).
#   FORMAT            fst (default), vcd, or ghw.
#   --no-gui          Dump the wave but do not launch GTKWave.
#
# Env: OSS_CAD_ENV overrides the environment file path.
#
# Examples:
#   bash tools/wave_wsl.sh                            # pipeline TB -> GTKWave
#   bash tools/wave_wsl.sh vhsnunzip_unbuffered_tc    # top-level TB
#   bash tools/wave_wsl.sh vhsnunzip_decoder_tc ghw   # GHW format
#   bash tools/wave_wsl.sh vhsnunzip_pipeline_tc fst --no-gui   # headless
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

# Parse args: optional TB name, optional format, optional --no-gui.
TB="vhsnunzip_pipeline_tc"
FMT="fst"
GUI=1
for arg in "$@"; do
  case "$arg" in
    --no-gui)        GUI=0 ;;
    fst|vcd|ghw)     FMT="$arg" ;;
    vhsnunzip_*_tc)  TB="$arg" ;;
    *) echo "ERROR: unrecognized arg '$arg'"; exit 2 ;;
  esac
done

echo "== dumping waveform: $TB ($FMT) =="
bash "$ROOT/sim/run.sh" "$TB" "--wave=$FMT"

WAVE="$ROOT/sim/work/$TB/wave.$FMT"
if [ ! -f "$WAVE" ]; then
  echo "ERROR: expected waveform not found at $WAVE"
  exit 1
fi

if [ "$GUI" -eq 0 ]; then
  echo "Wave written: $WAVE  (--no-gui, not opening viewer)"
  exit 0
fi

echo "== opening GTKWave =="
echo "  (needs WSLg; if nothing appears, run with --no-gui and open $WAVE"
echo "   from Windows in GTKWave or the Surfer VSCode extension instead.)"
exec gtkwave "$WAVE"
