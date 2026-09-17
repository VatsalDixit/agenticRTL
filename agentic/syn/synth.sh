#!/usr/bin/env bash
#
# Open-source synthesis and static timing for one candidate design.
#
# GHDL reads the VHDL-2008, the GHDL-Yosys plugin hands it to Yosys, ABC maps
# it onto the Nangate 45nm standard-cell library with real timing arcs. Area is
# the sum of the cell areas; the clock comes from ABC's critical-path delay.
# Memories stay as memory cells (the RAM entity is replaced by a stub), so the
# area number is about the logic the optimiser edits, not about storage.
#
# usage: synth.sh RTL_DIR STUB_FILE LIBERTY OUT_DIR TOP PERIOD_PS
#
# Prints SYNTH_OK and the two lines the harness parses (Delay = ..., Chip area),
# or SYNTH_*_FAIL with the tool log tail.
#
set -u

RTL="$1"; STUB="$2"; LIB="$3"; OUT="$4"; TOP="$5"; PERIOD="$6"

mkdir -p "$OUT" || exit 2
cd "$OUT" || exit 2
rm -f work-obj08.cf "$TOP" "$TOP.exe" "e~$TOP.o" netlist.v

# The file list is discovered, not hand-written, so a candidate that adds an
# entity is picked up. `ghdl -i` records the units and `ghdl -m` sorts out the
# analysis order from the dependencies.
FILES=()
for f in "$RTL"/*.vhd; do
  case "$f" in *.sim.vhd|*.syn.vhd) continue ;; esac
  FILES+=("$f")
done
FILES+=("$STUB")

if ! ghdl -i --std=08 "${FILES[@]}" > analyse.log 2>&1; then
  echo "SYNTH_ANALYSE_FAIL"; cat analyse.log; exit 3
fi
if ! ghdl -m --std=08 "$TOP" > elab.log 2>&1; then
  echo "SYNTH_ELAB_FAIL"; cat elab.log; exit 4
fi

yosys -m ghdl -q -l yosys.log -p "
  ghdl --std=08 $TOP
  hierarchy -check -top $TOP
  proc
  flatten
  opt_expr
  opt_clean
  memory -nomap
  opt -fast
  techmap
  opt
  dfflibmap -liberty $LIB
  dump -o dffs.txt t:DFF_*
  abc -liberty $LIB -D $PERIOD -script +strash;&get,-n;&fraig,-x;&put;scorr;dc2;dretime;retime,-o,{D};strash;&get,-n;&dch,-f;&nf,{D};&put;buffer,-c;topo;stime,-p
  setundef -zero
  opt_clean
  tee -o stat.log stat -liberty $LIB
" > yosys.out 2>&1
rc=$?
if [ $rc -ne 0 ]; then
  echo "SYNTH_YOSYS_FAIL"
  tail -40 yosys.log
  exit 5
fi

echo "SYNTH_OK"
grep -E "Delay\s*=" yosys.log | tail -1
grep -E "Chip area for module" yosys.log | tail -1
grep -E "used for sequential elements" yosys.log | tail -1
