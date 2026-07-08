#!/usr/bin/env bash
# Probe the environment for available tooling (read-only; installs nothing).
set -u
echo "arch=$(uname -m)"
. /etc/os-release 2>/dev/null && echo "distro=$ID $VERSION_ID"
echo "home=$HOME"
for c in conda mamba micromamba curl wget tar xz zstd bzip2 gcc gnat make ghdl nvc python3; do
  if command -v "$c" >/dev/null 2>&1; then
    printf '%-11s: %s\n' "$c" "$(command -v "$c")"
  else
    printf '%-11s: MISSING\n' "$c"
  fi
done
printf '%-11s: ' internet
if curl -fsSILm8 https://conda.anaconda.org >/dev/null 2>&1 || \
   wget -q --spider --timeout=8 https://conda.anaconda.org 2>/dev/null; then
  echo OK
else
  echo NO
fi
printf '%-11s: ' passwordless-sudo
sudo -n true 2>/dev/null && echo YES || echo NO
