"""Shared constants + tiny pure helpers used across debrid clients.

Lives in `clients/` so each client module can import it without
pulling in server.py (which would create an import cycle — server.py
imports the clients package).
"""
from __future__ import annotations

import re


RD_BASE = "https://api.real-debrid.com/rest/1.0"


AUDIO_EXTS = {
    ".mp3", ".flac", ".aac", ".ogg", ".opus", ".m4a",
    ".wav", ".wma", ".alac", ".ape",
}


# Default TTL for the SQLite-backed debrid library cache. Used by
# RDClient / AllDebridClient / TorBoxClient when caching the user's
# downloaded-torrents snapshot.
_RD_DOWNLOADED_TTL = 60.0


def _rd_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _normalize_torrent_name(s: str) -> str:
    """Lowercase + strip non-alphanum so two spellings of the same torrent
    name (different separators, brackets, etc.) compare equal."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _extract_btih(s: str) -> str:
    """Lowercase 40-char hex info-hash from a magnet URI (or '')."""
    if not s:
        return ""
    m = re.search(r"btih:([0-9a-fA-F]{40})", s, re.I)
    return m.group(1).lower() if m else ""
