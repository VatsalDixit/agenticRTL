#!/usr/bin/env bash
#
# Run the vhsnunzip testbench ladder inside the official GHDL Docker image.
# Requires only a running Docker daemon -- no local GHDL, no sudo, no changes
# to the host. The pre-generated vectors in vectors/ are used as-is (no Python
# needed inside the container).
#
# Usage:
#   tools/run_sim_docker.sh [TESTBENCH_ENTITY]
#
#   With no argument, runs the whole ladder (unit -> integration -> top),
#   stopping at the first failure. With an entity name, runs just that one.
#
# Override the image with GHDL_IMAGE, e.g.:
#   GHDL_IMAGE=ghdl/ghdl:5.1.1-mcode-ubuntu-22.04 tools/run_sim_docker.sh
#
set -euo pipefail

IMAGE="${GHDL_IMAGE:-ghdl/ghdl:6.0.0-mcode-ubuntu-22.04}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TB="${1:-}"

LADDER=(
  vhsnunzip_pre_decoder_tc
  vhsnunzip_decoder_tc
  vhsnunzip_decoder_long_tc
  vhsnunzip_cmd_gen_1_tc
  vhsnunzip_cmd_gen_2_tc
  vhsnunzip_pipeline_tc
  vhsnunzip_unbuffered_tc
)
if [ -n "$TB" ]; then
  LADDER=( "$TB" )
fi

# Build the in-container command: run sim/run.sh per testbench, stop on first fail.
CMD='set -e; fail=0;'
for tb in "${LADDER[@]}"; do
  CMD+=" echo; echo '### $tb ###'; bash sim/run.sh $tb || { echo \">>> FIRST FAILURE: $tb\"; exit 1; };"
done
CMD+=' echo; echo "ALL PASS";'

echo "image : $IMAGE"
echo "mount : $ROOT -> /work"
# MSYS_NO_PATHCONV stops Git Bash from mangling the container-side paths on Windows.
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
  docker run --rm -v "${ROOT}:/work" -w /work --entrypoint bash "$IMAGE" -lc "$CMD"
