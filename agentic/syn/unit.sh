#!/usr/bin/env bash
#
# Compile the design plus one unit testbench and run the testbench, for a
# build-track step (python agentic/check.py --unit NAME).
#
# Not frozen: it never touches a scored draw. The scored simulations run
# through sim_draws.sh, which compiles only RTL/*.vhd, so a unit test in
# rtl/unit/ is invisible to scoring (and to synthesis, which also lists only
# the top-level rtl/*.vhd).
#
# usage: unit.sh RTL_DIR UNIT_FILE BUILD_DIR RUN_DIR NAME STOP_TIME
#
#   RTL_DIR    the design's rtl/ folder (every *.vhd except *.syn.vhd)
#   UNIT_FILE  rtl/unit/NAME.vhd, holding entity NAME (no ports)
#   BUILD_DIR  scratch folder for the analysed library
#   RUN_DIR    the folder the testbench runs in: cs.tv, expected.hex,
#              elements.tv are there
#   NAME       the testbench entity
#   STOP_TIME  simulated-time guard, e.g. 100ms
#
# Prints UNIT_COMPILE_FAIL or UNIT_ELAB_FAIL (with the log), or
# "UNIT_RC=<n> STOPPED=<0|1>" after the run. An assertion of severity error
# or failure stops the run with a non-zero rc.
#
set -u

RTL="$1"; UNIT="$2"; BUILD="$3"; RUN="$4"; NAME="$5"; STOP="$6"

mkdir -p "$BUILD" || exit 2
cd "$BUILD" || exit 2
rm -f work-obj08.cf "$NAME" "$NAME.exe"

FILES=()
for f in "$RTL"/*.vhd; do
  case "$f" in *.syn.vhd) continue ;; esac
  FILES+=("$f")
done

if ! ghdl -i --std=08 "${FILES[@]}" "$UNIT" > analyse.log 2>&1; then
  echo "UNIT_COMPILE_FAIL"
  cat analyse.log
  exit 3
fi
if ! ghdl -m --std=08 "$NAME" > elab.log 2>&1; then
  echo "UNIT_ELAB_FAIL"
  cat elab.log
  exit 4
fi

cd "$RUN" || exit 2
if [ -x "$BUILD/$NAME" ]; then
  "$BUILD/$NAME" --assert-level=error --ieee-asserts=disable-at-0 --stop-time="$STOP" > sim.log 2>&1
else
  ghdl -r --std=08 --workdir="$BUILD" "$NAME" --assert-level=error \
    --ieee-asserts=disable-at-0 --stop-time="$STOP" > sim.log 2>&1
fi
rc=$?
stopped=0
if grep -q "stopped by --stop-time" sim.log; then stopped=1; fi
tail -n 40 sim.log
echo "UNIT_RC=$rc STOPPED=$stopped"
