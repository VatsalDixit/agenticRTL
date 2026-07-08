#!/usr/bin/env bash
#
# Install the OSS CAD Suite (YosysHQ) into the user's home under ~/eda.
# This is a self-contained prebuilt bundle -- no sudo, no system changes.
# Provides the open-source EDA stack used by this repo:
#   ghdl                  VHDL simulation (native, replaces the Docker path)
#   yosys + ghdl plugin   VHDL synthesis + area (LUT/FF/BRAM) reporting
#   nextpnr-*             place & route for supported families (timing)
#
# Usage:  bash tools/setup_oss_cad.sh
# After:  source ~/eda/oss-cad-suite/environment   (puts the tools on PATH)
#
set -euo pipefail

REL="2026-07-08"
TARBALL="oss-cad-suite-linux-x64-20260708.tgz"
URL="https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${REL}/${TARBALL}"
DEST="$HOME/eda"
PREFIX="$DEST/oss-cad-suite"

if [ -x "$PREFIX/bin/ghdl" ] && [ -x "$PREFIX/bin/yosys" ]; then
  echo "Already installed at $PREFIX"
else
  mkdir -p "$DEST"
  cd "$DEST"
  echo "Downloading OSS CAD Suite ${REL} (~689 MB)..."
  curl -fL --retry 3 -o "$TARBALL" "$URL"
  echo "Extracting..."
  tar xzf "$TARBALL"
  rm -f "$TARBALL"
fi

echo "== tool versions =="
"$PREFIX/bin/ghdl" --version 2>&1 | head -1
"$PREFIX/bin/yosys" -V 2>&1 | head -1
echo
echo "Installed to $PREFIX"
echo "Add to your shell:  source $PREFIX/environment"
