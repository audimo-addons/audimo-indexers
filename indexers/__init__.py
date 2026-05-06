"""Indexer registry + per-indexer search functions.

Each indexer module exposes a ``search_*`` coroutine. The
``_SOURCES`` registry maps source ids to spec dicts the route
handlers in server.py iterate over.
"""
from __future__ import annotations

from .apibay import search_apibay, _apibay_query, _apibay_files, APIBAY_BASE
from .bitsearch import search_bitsearch, _bitsearch_query, BITSEARCH_BASE
from .torrentdownload import (
    search_torrentdownload, _td_query, TORRENTDOWNLOAD_BASE,
)
from .prowlarr import search_prowlarr, _prowlarr_query, extract_rd_link
from .rutracker import (
    search_rutracker, _rutracker_query, _rt_cookie_jar,
    _rt_parse_size, _rt_strip_html, _rt_topic_torrent_bytes,
    RUTRACKER_BASE, RUTRACKER_UA, RT_FETCH_CAP,
)
from .audiobookbay import (
    search_audiobookbay, search_audiobookbay_books,
    _abb_fetch_magnet, _abb_strip_tags,
    _abb_base, _abb_query_words, _abb_build_query,
    ABB_DEFAULT_BASE, ABB_UA,
)


# ──────────────────────────────────────────────────────────────────
# Source registry — one per indexer
# ──────────────────────────────────────────────────────────────────
#
# Mirrors audimo_aio._SOURCES so adding more indexers (bitsearch,
# torrentdownload, prowlarr, rutracker, audiobookbay) in later
# Phase 2 commits is just appending entries here.

_SOURCES: dict[str, dict] = {
    "apibay": {
        "id": "apibay",
        "label": "PirateBay (apibay)",
        "icon": "",
        "enabled_key": "src_apibay_enabled",
        "default_enabled": True,
        "requires": lambda cfg: None,  # no required keys
        "search": lambda cfg, ctx: search_apibay(
            ctx["artist"], ctx["title"], ctx.get("album", "")
        ),
    },
    "bitsearch": {
        "id": "bitsearch",
        "label": "BitSearch",
        "icon": "",
        "enabled_key": "src_bitsearch_enabled",
        "default_enabled": True,
        "requires": lambda cfg: None,
        "search": lambda cfg, ctx: search_bitsearch(
            ctx["artist"], ctx["title"], ctx.get("album", "")
        ),
    },
    "torrentdownload": {
        "id": "torrentdownload",
        "label": "torrentdownload.info",
        "icon": "",
        "enabled_key": "src_torrentdownload_enabled",
        "default_enabled": True,
        "requires": lambda cfg: None,
        "search": lambda cfg, ctx: search_torrentdownload(
            ctx["artist"], ctx["title"], ctx.get("album", "")
        ),
    },
    "prowlarr": {
        "id": "prowlarr",
        "label": "Prowlarr",
        "icon": "",
        "enabled_key": "src_prowlarr_enabled",
        "default_enabled": False,
        "requires": lambda cfg: (
            "prowlarr_url not set" if not (cfg.get("prowlarr_url") or "").strip() else
            "prowlarr_api_key not set" if not (cfg.get("prowlarr_api_key") or "").strip() else
            None
        ),
        "search": lambda cfg, ctx: search_prowlarr(
            (cfg.get("prowlarr_url") or "").rstrip("/"),
            cfg.get("prowlarr_api_key", ""),
            ctx.get("artist", ""),
            ctx.get("title", ""),
            ctx.get("album", ""),
        ),
    },
    "rutracker": {
        "id": "rutracker",
        "label": "RuTracker",
        "icon": "",
        "enabled_key": "src_rutracker_enabled",
        "default_enabled": False,
        "requires": lambda cfg: (
            "rutracker_bb_session not set"
            if not (cfg.get("rutracker_bb_session") or "").strip()
            else None
        ),
        "search": lambda cfg, ctx: search_rutracker(
            cfg.get("rutracker_bb_session", ""),
            ctx.get("artist", ""),
            ctx.get("title", ""),
            ctx.get("album", ""),
        ),
    },
    "audiobookbay": {
        "id": "audiobookbay",
        "label": "AudiobookBay",
        "icon": "",
        "enabled_key": "src_audiobookbay_enabled",
        "default_enabled": True,
        "requires": lambda cfg: None,
        "search": lambda cfg, ctx: search_audiobookbay(cfg, ctx),
        # Audiobook-only indexer. Music searches should never see
        # AudiobookBay results in their picker — see _kind_matches.
        "kinds": ["audiobook"],
    },
}


__all__ = [
    "search_apibay",
    "search_bitsearch",
    "search_torrentdownload",
    "search_prowlarr",
    "search_rutracker",
    "search_audiobookbay",
    "search_audiobookbay_books",
    "_apibay_files",
    "_rt_topic_torrent_bytes",
    "_abb_fetch_magnet",
    "extract_rd_link",
    "_SOURCES",
]
