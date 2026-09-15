#!/usr/bin/env bash
# Fetch the Nangate 45nm open cell library used for area and timing, if the
# copy that ships with the kit is missing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HERE/lib"
URL=https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib
curl -fsSL --retry 3 -o "$HERE/lib/NangateOpenCellLibrary_typical.lib" "$URL"
ls -la "$HERE/lib/"
