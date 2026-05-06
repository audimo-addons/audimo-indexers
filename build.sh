#!/usr/bin/env bash
# Build audimo-indexers as a single-file PyInstaller binary and drop it
# into the Tauri sidecar dir so the desktop app can ship it.
#
# Usage:
#   bash addons/audimo_indexers/build.sh
#
# This addon does not depend on libtorrent — the desktop streaming
# sidecar handles all peering. So no Homebrew libtorrent setup is
# required; just Python + the requirements.txt deps.

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "[build] creating .venv"
  python3 -m venv .venv
fi

.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/pip install --quiet pyinstaller

rm -rf build dist

.venv/bin/pyinstaller audimo_indexers.spec --clean --noconfirm

# Place the binary alongside audimo-aio for Tauri's externalBin pickup.
TRIPLE="$(uname -m)-apple-darwin"
case "$TRIPLE" in
  arm64-*) TRIPLE="aarch64-apple-darwin" ;;
esac
DEST="../../frontend/src-tauri/binaries/audimo-indexers-${TRIPLE}"
cp -f dist/audimo-indexers "$DEST"
chmod +x "$DEST"
echo "[build] wrote $(cd .. && cd .. && pwd)/frontend/src-tauri/binaries/audimo-indexers-${TRIPLE}"
