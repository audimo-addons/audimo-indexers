#!/usr/bin/env bash
# Run the audimo_indexers addon natively on port 9005.
#
# This addon is public-host-safe: it queries indexers and resolves
# torrents via debrid backends (RD/AllDebrid/TorBox/...) only. There
# is no libtorrent peering in this process — the user's local Audimo
# desktop streaming sidecar (port 11471) handles the actual torrent
# data path. So this script does not need libtorrent on PATH and
# will not load it even if installed.

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Creating venv (.venv/)..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

echo "[run] starting audimo-indexers on http://0.0.0.0:9005"

exec .venv/bin/uvicorn server:app \
  --host 0.0.0.0 \
  --port 9005 \
  --proxy-headers \
  --no-access-log \
  --reload \
  --reload-dir "$(pwd)" \
  --reload-exclude ".venv/*"
