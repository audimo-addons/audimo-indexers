"""
PyInstaller / standalone launcher for the audimo-indexers addon.

`run_native.sh` covers dev mode. For frozen builds the binary needs
an actual `__main__` that boots uvicorn directly. Mirrors audimo-aio's
run.py.

This addon does not depend on libtorrent — torrent peering happens in
the user's local desktop streaming sidecar, not here.
"""

import os

import uvicorn

from server import app


def main() -> None:
    # Default to 127.0.0.1; `0.0.0.0` requires an explicit opt-in
    # because it exposes the addon (and any baked-in debrid creds) to
    # the LAN.
    host = (
        os.getenv("AUDIMO_INDEXERS_HOST")
        or os.getenv("AUDIMO_ADDON_HOST")
        or os.getenv("TUNNEL_ADDON_HOST")
        or "127.0.0.1"
    )
    port = int(os.getenv("AUDIMO_INDEXERS_PORT", "9005"))
    # access_log=False: addon URLs carry user secrets (RD api key,
    # rutracker bb_session, prowlarr key) in path segments. uvicorn's
    # default access log would write them to stdout. App errors still
    # surface on stderr.
    uvicorn.run(
        app, host=host, port=port,
        proxy_headers=True, log_level="info", access_log=False,
    )


if __name__ == "__main__":
    main()
