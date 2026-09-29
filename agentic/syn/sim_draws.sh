#!/usr/bin/env bash
#
# Compile one design with the loop's throughput testbench, then simulate it
# against several stimulus draws in parallel, at most SIM_JOBS at a time
# (default 3).
#
# The cap exists because every simulator holds its own copy of the design
# model, about a gigabyte once the design has two cores, and two of these
# scripts run side by side (two candidates measured, or two sessions checking).
# Uncapped, twenty simulators ran at once in WSL's 15 GB, the out-of-memory
# killer took some of them, and the loop recorded ten sound candidates as
# failing correctness. Results do not depend on the cap; only the wall time.
#
# usage: sim_draws.sh RTL_DIR TB_FILE BUILD_DIR "GENERICS" STOP_TIME DRAW_DIR:CHUNKS...
#
#   RTL_DIR     the candidate's rtl/ folder (every *.vhd except *.syn.vhd)
#   TB_FILE     the loop's own copy of vhsnunzip_perf_tc.sim.08.vhd
#   BUILD_DIR   scratch folder for the analysed library
#   GENERICS    e.g. "-gCO_BYTES=8 -gCO_CNT_BITS=3 -gDE_BYTES=8 -gDE_CNT_BITS=4"
#   STOP_TIME   simulated-time deadlock guard, e.g. 50ms
#   DRAW_DIR    a folder holding cs.tv; CHUNKS is the number of chunks in it
#
# Each draw folder gets sim.log, perf.txt and out.hex. Prints one status line
# per draw at the end: "DRAW <dir> rc=<n> deadlock=<0|1>".
#
set -u

RTL="$1"; TB="$2"; BUILD="$3"; GENERICS="$4"; STOP="$5"; shift 5

mkdir -p "$BUILD" || exit 2
cd "$BUILD" || exit 2
rm -f work-obj08.cf vhsnunzip_perf_tc vhsnunzip_perf_tc.exe e~vhsnunzip_perf_tc.o

FILES=()
for f in "$RTL"/*.vhd; do
  case "$f" in *.syn.vhd) continue ;; esac
  FILES+=("$f")
done

if ! ghdl -i --std=08 "${FILES[@]}" "$TB" > analyse.log 2>&1; then
  echo "COMPILE_FAIL"
  cat analyse.log
  exit 3
fi
if ! ghdl -m --std=08 vhsnunzip_perf_tc > elab.log 2>&1; then
  echo "ELAB_FAIL"
  cat elab.log
  exit 4
fi
echo "ELAB_OK"

# With the LLVM/GCC backend `ghdl -m` leaves an executable behind and that is
# what runs; with mcode there is none and `ghdl -r` re-elaborates from the
# library in this folder.
EXE=""
if [ -x "$BUILD/vhsnunzip_perf_tc" ]; then
  EXE="$BUILD/vhsnunzip_perf_tc"
fi

run_one() {
  local spec="$1"
  local dir="${spec%:*}"
  local chunks="${spec##*:}"
  (
    cd "$dir" || { echo "DRAW $dir rc=98 deadlock=0"; exit 0; }
    rm -f perf.txt out.hex sim.log
    # shellcheck disable=SC2086
    if [ -n "$EXE" ]; then
      "$EXE" $GENERICS -gEXPECT_CHUNKS="$chunks" --ieee-asserts=disable --stop-time="$STOP" > sim.log 2>&1
    else
      ghdl -r --std=08 --workdir="$BUILD" vhsnunzip_perf_tc $GENERICS -gEXPECT_CHUNKS="$chunks" --ieee-asserts=disable --stop-time="$STOP" > sim.log 2>&1
    fi
    rc=$?
    dl=0
    if grep -q -e "stopped by --stop-time" -e "DEADLOCK" sim.log; then dl=1; fi
    echo "DRAW $dir rc=$rc deadlock=$dl"
  )
}

SIM_JOBS="${SIM_JOBS:-3}"
for spec in "$@"; do
  while [ "$(jobs -rp | wc -l)" -ge "$SIM_JOBS" ]; do
    wait -n
  done
  run_one "$spec" &
done
wait
echo "SIM_DONE"
