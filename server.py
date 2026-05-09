"""audimo_indexers — Comet-style sources + debrid addon.

This addon is **public-host-safe**: it queries indexers (apibay,
bitsearch, prowlarr, rutracker, audiobookbay, …) and resolves
torrent sources via debrid backends (RD/AllDebrid/TorBox/etc) only.
There is no libtorrent peering, no DHT, no direct torrent download
in this process. That keeps the addon legally clean enough to host
on a public URL (DigitalOcean, Fly, …) where many users can point
their own debrid keys at it.

When a source has no debrid coverage, the addon emits an
``unsupported`` SSE event with code ``torrent_no_debrid`` carrying
the magnet / info_hash. The user's local Audimo desktop app routes
that to its bundled streaming sidecar (port 11471, libtorrent lives
there) which does the actual peering. The addon never sees torrent
bytes.

Each source returned is stamped ``addon_id = "audimo-indexers"`` so
the Audimo AIO aggregator (which proxies resolve.stream / cache.resolve
back to whoever owns the source) routes follow-up calls here.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from typing import AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse


# Hosted-mode flag is informational only — there is no longer a
# "non-hosted" mode that bundles libtorrent. The flag is preserved
# so existing settings UIs and log lines that key on it still parse;
# new code should not add behaviour conditional on this value.
HOSTED = (os.environ.get("AUDIMO_HOSTED") or "").strip().lower() in ("1", "true", "yes")


MANIFEST = {
    "id": "audimo-indexers",
    "name": "Audimo Indexers" + (" (hosted)" if HOSTED else ""),
    "version": "0.17.0",
    "description": (
        "Aggregates multiple indexers and (eventually) debrid backends. "
        "Designed to be installed as an extension inside the Audimo AIO "
        "addon, but works standalone."
    ),
    "capabilities": [
        "resolve.sources",
        "resolve.sources.stream",
        "resolve.stream",
        "cache.resolve",
        "search.books",
    ],
    "display": {
        "label": "Indexers",
        "icon": ""
    },
    "settings_schema": [
        {
            "type": "section",
            "label": "Hosted access",
            "description": "If you're running this addon on a public URL, set AUDIMO_ADDON_KEY on the server and paste the same value here. Leave blank for local installs.",
            "fields": [
                {
                    "key": "addon_key",
                    "type": "password",
                    "label": "Addon access key",
                    "description": "Shared secret with the server's AUDIMO_ADDON_KEY env var.",
                    "placeholder": "long-random-string",
                },
            ],
        },
        {
            "type": "section",
            "label": "Indexers",
            "description": "Where to look for sources. Toggle individually.",
            "fields": [
                {
                    "key": "src_apibay_enabled",
                    "type": "boolean",
                    "label": "PirateBay (apibay)",
                    "description": "Public JSON API. No setup required.",
                    "default": True,
                },
                {
                    "key": "src_bitsearch_enabled",
                    "type": "boolean",
                    "label": "BitSearch",
                    "description": "Public meta-aggregator. Covers music and audiobooks.",
                    "default": True,
                },
                {
                    "key": "src_torrentdownload_enabled",
                    "type": "boolean",
                    "label": "torrentdownload.info",
                    "description": "Public RSS feed. Zero config.",
                    "default": True,
                },
                {
                    "key": "src_prowlarr_enabled",
                    "type": "boolean",
                    "label": "Prowlarr",
                    "description": "Use your own Prowlarr instance for indexer aggregation.",
                    "default": False,
                },
                {
                    "key": "prowlarr_url",
                    "label": "Prowlarr URL",
                    "type": "text",
                    "placeholder": "http://localhost:9696",
                    "show_if": "src_prowlarr_enabled",
                },
                {
                    "key": "prowlarr_api_key",
                    "label": "Prowlarr API key",
                    "type": "password",
                    "secret": True,
                    "show_if": "src_prowlarr_enabled",
                },
                {
                    "key": "src_rutracker_enabled",
                    "type": "boolean",
                    "label": "RuTracker",
                    "description": "Russian tracker. Requires session cookie (the login form has a captcha that can't be solved server-side).",
                    "default": False,
                },
                {
                    "key": "rutracker_bb_session",
                    "label": "RuTracker bb_session cookie",
                    "type": "password",
                    "secret": True,
                    "description": "Log in to rutracker.org in a browser, open DevTools → Application → Cookies, copy the value of the 'bb_session' cookie.",
                    "show_if": "src_rutracker_enabled",
                },
                {
                    "key": "src_audiobookbay_enabled",
                    "type": "boolean",
                    "label": "AudiobookBay",
                    "description": "Audiobook-specific public tracker. Only consulted when the search is for an audiobook.",
                    "default": True,
                },
                {
                    "key": "audiobookbay_base",
                    "label": "AudiobookBay base URL",
                    "type": "text",
                    "placeholder": "https://audiobookbay.fi",
                    "description": "Base URL for the tracker. The site rotates TLDs (.fi / .lu / .li …); update if results stop loading.",
                    "show_if": "src_audiobookbay_enabled",
                },
                {
                    "key": "verify_torrents",
                    "type": "boolean",
                    "label": "Verify torrents are alive (BEP-15 announce)",
                    "description": "Before returning sources, run a UDP-tracker announce to filter dead torrents and pass live peer endpoints to the streaming sidecar (skips DHT on the hot path). Adds 1-3s of search latency; results are cached. Turn off if your network blocks outbound UDP or you're seeing slow searches.",
                    "default": True,
                },
            ],
        },
        {
            "type": "section",
            "label": "Debrid",
            "description": "Optional — cached-torrent shortcut and instant streaming for torrents Real-Debrid already has.",
            "fields": [
                {
                    "key": "rd_api_key",
                    "label": "Real-Debrid API key",
                    "type": "password",
                    "secret": True,
                    "description": "Optional. With a key, RD-cached / RD-cacheable torrents play instantly. Without one, the desktop streaming sidecar peers torrents directly.",
                },
                {
                    "key": "alldebrid_api_key",
                    "label": "AllDebrid API key",
                    "type": "password",
                    "secret": True,
                    "description": "Alternative to Real-Debrid. Used only when no RD key is set (RD wins on tie). Get yours at alldebrid.com → Account → API.",
                },
                {
                    "key": "torbox_api_key",
                    "label": "TorBox API key",
                    "type": "password",
                    "secret": True,
                    "description": "Alternative to RD/AllDebrid. Used only when neither of those is set (priority: RD → AllDebrid → TorBox). Get yours at torbox.app → Settings → API Key.",
                },
                {
                    "key": "premiumize_api_key",
                    "label": "Premiumize API key",
                    "type": "password",
                    "secret": True,
                    "description": "Priority slot 4 (RD → AllDebrid → TorBox → Premiumize). Get yours at premiumize.me → My Account → API key.",
                },
                {
                    "key": "debridlink_api_key",
                    "label": "Debrid-Link API key",
                    "type": "password",
                    "secret": True,
                    "description": "Priority slot 5. Get yours at debrid-link.fr → Account → API.",
                },
                {
                    "key": "easydebrid_api_key",
                    "label": "EasyDebrid API key",
                    "type": "password",
                    "secret": True,
                    "description": "Priority slot 6. Cached-only debrid (no slow staging) — uncached torrents are handed off to the desktop streaming sidecar. Get yours at easydebrid.com.",
                },
            ],
        },
        {
            "type": "section",
            "label": "Storage",
            "description": "Where downloaded files live on disk. Leave blank for sensible defaults.",
            "fields": [
                {
                    "key": "audiobook_save_dir",
                    "label": "Audiobook downloads",
                    "type": "text",
                    "placeholder": "~/Audiobooks",
                    "description": "Audiobook downloads land here permanently for offline replay.",
                },
                {
                    "key": "permanent_music_dir",
                    "label": "Music downloads",
                    "type": "text",
                    "placeholder": "~/Music/Audimo",
                    "description": "Music downloads land here permanently. Replay serves from disk without re-fetching.",
                },
                {
                    "key": "temp_dir",
                    "label": "Temporary cache",
                    "type": "text",
                    "placeholder": "$TMPDIR/audimo_indexers",
                    "description": "Work area for any throwaway intermediate files (rarely used today; reserved for future flows).",
                },
                {
                    "key": "delete_local_after_debrid_cache",
                    "label": "Delete local copy after debrid caches it",
                    "type": "boolean",
                    "default": False,
                    "description": "When you have a debrid backend (Real-Debrid, AllDebrid, etc.) and a torrent's local download finishes uploading to that debrid, remove the local copy from disk and play from the debrid CDN going forward. Saves disk space at the cost of redownloading if the debrid drops the cache later. Off by default — local copies are forever.",
                },
            ],
        },
    ],
}


app = FastAPI(
    title=MANIFEST["name"],
    version=MANIFEST["version"],
    # Auto-generated docs UI fingerprints every route on a public host
    # — set AUDIMO_DEBUG=1 to opt back in.
    docs_url="/docs" if str(os.environ.get("AUDIMO_DEBUG", "")).lower() in {"1", "true", "yes"} else None,
    redoc_url=None,
    openapi_url="/openapi.json" if str(os.environ.get("AUDIMO_DEBUG", "")).lower() in {"1", "true", "yes"} else None,
)

# Audimo addons are called from the user's browser (device-as-client)
# and from other addons (the aggregator), so they must allow any
# cross-origin caller. `allow_credentials=False` is a hard pin — auth
# (when configured) is a header bearer token, never a cookie, so we
# never want to turn the wildcard origin into a credentialed one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Optional shared-secret gate ──────────────────────────────────
#
# When AUDIMO_ADDON_KEY is set the addon refuses every request that
# doesn't present `X-Audimo-Addon-Key` matching it. This is the only
# auth mechanism for hosted deploys — anyone with the URL would
# otherwise be able to drive the user's debrid bill. Manifest +
# CORS preflight stay public so install URLs and browser fetches
# still work.
#
# Unset = no check (the local-dev / Tauri-sidecar path).
_ADDON_KEY = (os.environ.get("AUDIMO_ADDON_KEY") or "").strip()


@app.middleware("http")
async def _require_addon_key(request: Request, call_next):
    if not _ADDON_KEY:
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)
    # Manifest is always public — clients call it before they know
    # the key (during install URL probing).
    if request.url.path.endswith("/manifest.json"):
        return await call_next(request)
    # /configure is the page where the user types the key in the
    # first place, so it can't itself require the key — that's a
    # chicken-and-egg lockout. The form is purely client-side
    # (HTML + JS that builds an install URL); no live data leaks
    # by serving it. Same for /health — the container healthcheck
    # in compose hits http://localhost:9005/health without auth.
    p = request.url.path
    if p.endswith("/configure") or p == "/health":
        return await call_next(request)
    presented = request.headers.get("x-audimo-addon-key", "").strip()
    if not presented or presented != _ADDON_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"detail": "addon key required"},
            status_code=401,
        )
    return await call_next(request)


# Lightweight request logger — uvicorn runs with --no-access-log to
# avoid leaking secrets in path-segmented installs, but we still need
# visibility into what's hitting the addon during dev/testing. This
# logs ONLY the route template (FastAPI's resolved path), not the raw
# URL, so a path like /<base64-config>/resolve/sources is logged as
# /{config}/resolve/sources — never the config itself.
@app.middleware("http")
async def _log_route(request: Request, call_next):
    response = await call_next(request)
    route = request.scope.get("route")
    template = getattr(route, "path", None) or "(unmatched)"
    has_config = bool(request.path_params.get("config"))
    # Surface only the `dbg` query param verbatim — diagnostic pings
    # from the frontend use it; everything else is suppressed so we
    # don't accidentally log secrets that ride in query strings.
    qs = request.url.query
    dbg = ""
    if qs:
        for kv in qs.split("&"):
            if kv.startswith("dbg="):
                dbg = " " + kv  # e.g. " dbg=force-remove-audimo-aio"
                break
    print(
        f"[req] {request.method} {template} "
        f"status={response.status_code} has_config={has_config}{dbg}",
        flush=True,
    )
    return response


# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────

AUDIO_EXTS = {
    ".mp3", ".flac", ".aac", ".ogg", ".opus", ".m4a",
    ".wav", ".wma", ".alac", ".ape",
}

# VIDEO_EXTS / VIDEO_KEYWORDS / TRACKERS / is_video / make_magnet now
# live in indexers/_shared.py. TRACKERS is re-imported below for the
# verify-sources callsite that still lives in server.py.
from indexers._shared import TRACKERS  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _config_from(request: Request, payload: dict | None = None) -> dict:
    """Effective config: body settings overlaid by path-segmented config.

    Mirrors audimo_aio._config_from. Path config wins (it carries
    secrets baked into the install URL); body settings carry non-secret
    toggles the aggregator forwards.
    """
    body_cfg = dict((payload or {}).get("settings") or {}) if payload is not None else {}
    raw = request.path_params.get("config", "") or ""
    path_cfg = _parse_config_str(raw) if raw else {}
    return {**body_cfg, **path_cfg}


def _parse_config_str(s: str) -> dict:
    """Decode the path-segmented config blob (Stremio-style).

    Format: a base64url-encoded JSON dict, optionally URL-encoded once.
    Returns {} on any error so a malformed segment never 500s.
    """
    if not s:
        return {}
    try:
        # Try plain base64url first.
        import base64
        try:
            raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
        except Exception:
            raw = urllib.parse.unquote(s)
        return json.loads(raw)
    except Exception:
        return {}


def _bool(cfg: dict, key: str, default: bool = False) -> bool:
    """Coerce settings values (which may arrive as JSON true/false,
    "true"/"false", "1"/"0") into Python bools. Mirrors audimo_aio."""
    v = cfg.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


# ──────────────────────────────────────────────────────────────────
# Storage paths — mirror audimo_aio's helpers but use a separate
# default temp dir name (``audimo_indexers``) so the two addons can
# coexist without their cleanup tasks stepping on each other.
# ──────────────────────────────────────────────────────────────────

import tempfile


def _expand_dir(raw: str | None, default: str) -> str:
    p = (raw or "").strip()
    if not p:
        p = default
    p = os.path.expanduser(p)
    os.makedirs(p, exist_ok=True)
    return p


# Container-friendly defaults: env vars override the home-dir paths
# so a Docker run with `-v /downloads/music:/music` can wire those up
# without the user baking custom paths into their install URL. The
# per-user config (cfg.get(...)) still wins when set, so a single
# multi-tenant deploy with a baked install URL keeps working.
_DEFAULT_AUDIOBOOK_DIR = os.environ.get("AUDIMO_INDEXERS_AUDIOBOOK_DIR") or \
    os.path.join(os.path.expanduser("~"), "Audiobooks")
_DEFAULT_MUSIC_DIR = os.environ.get("AUDIMO_INDEXERS_MUSIC_DIR") or \
    os.path.join(os.path.expanduser("~"), "Music", "Audimo")


def _audiobook_save_dir(cfg: dict) -> str:
    return _expand_dir(cfg.get("audiobook_save_dir"), _DEFAULT_AUDIOBOOK_DIR)


def _permanent_music_dir(cfg: dict) -> str:
    return _expand_dir(cfg.get("permanent_music_dir"), _DEFAULT_MUSIC_DIR)


def _temp_dir(cfg: dict) -> str:
    return _expand_dir(
        cfg.get("temp_dir"),
        os.path.join(tempfile.gettempdir(), "audimo_indexers"),
    )


def _safe_slug(name: str) -> str:
    """Turn a name into a directory-safe slug. Keeps alphanumerics,
    dashes, underscores, dots, spaces; collapses everything else."""
    s = re.sub(r"[^A-Za-z0-9._\- ]+", "_", (name or "").strip()) or "download"
    return s[:120]


def _organized_relpath(kind: str, title: str, artist: str, album: str, ext: str) -> str:
    """Compute a clean library-style relative path:
      music     → {Artist}/{Album}/{Title}.{ext}
      audiobook → {Author}/{Title}/{Title}.{ext}
    Falls back to "Unknown" parts when fields are missing."""
    title_s = _safe_slug(title or "Unknown Title")
    if kind == "audiobook":
        author_s = _safe_slug(artist or "Unknown Author")
        return os.path.join(author_s, title_s, f"{title_s}{ext}")
    artist_s = _safe_slug(artist or "Unknown Artist")
    album_s = _safe_slug(album or "Unknown Album")
    return os.path.join(artist_s, album_s, f"{title_s}{ext}")


def _rmdir_walk_up(start_dir: str, stop_at: str, max_levels: int = 4):
    """Remove ``start_dir`` if empty, walk up to its parent, etc.
    Stops at (but doesn't remove) ``stop_at``. Caps at ``max_levels``
    to guard against unrelated-parent deletion if the path isn't
    under stop_at."""
    try:
        stop_at_real = os.path.realpath(stop_at)
        cur = os.path.realpath(start_dir)
        for _ in range(max_levels):
            if cur == stop_at_real or not cur.startswith(stop_at_real + os.sep):
                break
            if not os.path.isdir(cur):
                break
            try:
                if os.listdir(cur):
                    break
                os.rmdir(cur)
            except OSError:
                break
            cur = os.path.dirname(cur)
    except Exception:
        pass




# ──────────────────────────────────────────────────────────────────
# SQLite cache + debrid clients
# ──────────────────────────────────────────────────────────────────
#
# These two cohesive sections were extracted to sibling modules so
# server.py can focus on routes + indexer code. The names below are
# imported back here so existing callsites in the route handlers
# (_resolve_sources, /admin, /test_*, etc.) keep working unchanged.
#
#   cache_db    — SQLite cache (debrid library, indexer query,
#                 torrent_files, torrent_health) + BEP-15 verify.
#   clients/    — RD/AllDebrid/TorBox/Premiumize/EasyDebrid/Debrid-
#                 Link client classes + the shared helpers they share.

import cache_db
from cache_db import (
    _cache_get_debrid_library,
    _cache_put_debrid_library,
    _cache_get_indexer_query,
    _cache_put_indexer_query,
    _cache_get_torrent_files,
    _cache_put_torrent_files,
    _cache_get_health,
    _cache_put_health,
    _HEALTH_CACHE_TTL_S,
)
from clients import (
    AllDebridClient,
    DebridLinkClient,
    EasyDebridClient,
    PremiumizeClient,
    RDClient,
    TorBoxClient,
    RD_BASE,
    _RD_DOWNLOADED_TTL,
    _active_debrid,
    _add_and_wait,
    _check_rd_cache,
    _extract_btih,
    _normalize_torrent_name,
    _rd_headers,
    _unrestrict_audio,
    fetch_rd_downloaded,
)

# ──────────────────────────────────────────────────────────────────
# Indexer modules
# ──────────────────────────────────────────────────────────────────
#
# The 6 per-indexer search functions and their direct helpers were
# extracted into the ``indexers/`` package. The names re-imported
# below keep route handlers, the verification path, and the
# ``_SOURCES`` registry usage in this module unchanged.

from indexers import (
    _SOURCES,
    search_apibay,
    search_bitsearch,
    search_torrentdownload,
    search_prowlarr,
    search_rutracker,
    search_audiobookbay,
    search_audiobookbay_books,
    _apibay_files,
    _rt_topic_torrent_bytes,
    _abb_fetch_magnet,
    extract_rd_link,
)
from indexers._shared import (
    is_video,
    make_magnet,
    _seed_bucket,
    _album_collapses_to_artist,
    _files_contain_track,
    _normalize_title_phrase,
    _title_phrase_variants,
)


@app.on_event("startup")
async def _start_cache():
    cache_db.start_cache()




def _source_kinds(spec: dict) -> list[str]:
    """Which track kinds (music / audiobook) does this source serve?
    Default: music-only — every source not explicitly tagged is
    treated as a music indexer."""
    return list(spec.get("kinds") or ["music"])


def _kind_matches(spec: dict, kind: str) -> bool:
    """Is this source applicable to a search of the given kind?
    Empty kind ('') is treated as music — that's the default for
    bare track lookups from the music search/library flows."""
    k = (kind or "music").lower()
    return k in _source_kinds(spec)


def _source_enabled(source_id: str, cfg: dict) -> bool:
    spec = _SOURCES.get(source_id)
    if not spec:
        return False
    return _bool(cfg, spec["enabled_key"], spec.get("default_enabled", False))


# ──────────────────────────────────────────────────────────────────
# HTTP surface
# ──────────────────────────────────────────────────────────────────


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


@app.get("/", response_class=HTMLResponse)
async def landing() -> str:
    caps = "".join(
        f'<span style="background:#222;padding:2px 8px;margin:2px;border-radius:6px">{c}</span>'
        for c in MANIFEST["capabilities"]
    )
    indexers = "".join(
        f'<li>{spec["label"]}</li>' for spec in _SOURCES.values()
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{MANIFEST['name']}</title>
<style>body{{font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 20px;color:#eee;background:#111}}
h1{{margin:0 0 8px}}p{{color:#aaa}}</style></head><body>
<h1>{MANIFEST['name']}</h1>
<p>{MANIFEST['description']}</p>
<p>v{MANIFEST['version']} · capabilities: {caps}</p>
<p>Indexers wired:</p><ul>{indexers}</ul>
<p>Manifest: <a href="/manifest.json" style="color:#7af">/manifest.json</a> · Health: <a href="/health" style="color:#7af">/health</a></p>
</body></html>"""


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict:
    return {
        "id": MANIFEST["id"],
        "version": MANIFEST["version"],
        "hosted": HOSTED,
    }


# ──────────────────────────────────────────────────────────────────
# Admin / observability — /admin (JSON + HTML)
# ──────────────────────────────────────────────────────────────────
#
# One-screen dashboard so we can eyeball cache hit rates and indexer
# health without spelunking the log. Reads are cheap: SQLite COUNT()
# on small tables. No external API calls.

import time as _admin_time
_BOOT_TIME = _admin_time.time()


def _admin_snapshot(cfg: dict | None = None) -> dict:
    """Compose a JSON-friendly snapshot of addon state. Pure data —
    rendered as JSON for /admin and as HTML for /admin (browser)."""
    cfg = cfg or {}
    snap: dict = {
        "addon": {
            "id": MANIFEST["id"],
            "version": MANIFEST["version"],
            "hosted": HOSTED,
            "uptime_seconds": int(_admin_time.time() - _BOOT_TIME),
        },
    }

    # Indexers — which are enabled/runnable?
    indexers: list[dict] = []
    for spec in _SOURCES.values():
        enabled = _source_enabled(spec["id"], cfg)
        req = spec["requires"](cfg) if cfg else None
        indexers.append({
            "id": spec["id"],
            "label": spec["label"],
            "enabled": enabled,
            "blocked_by": req,
        })
    snap["indexers"] = indexers

    # Configured debrids (which keys are present in the config we got).
    debrid_keys = [
        ("rd", "Real-Debrid", "rd_api_key"),
        ("alldebrid", "AllDebrid", "alldebrid_api_key"),
        ("torbox", "TorBox", "torbox_api_key"),
        ("premiumize", "Premiumize", "premiumize_api_key"),
        ("debridlink", "Debrid-Link", "debridlink_api_key"),
        ("easydebrid", "EasyDebrid", "easydebrid_api_key"),
    ]
    snap["debrids"] = [
        {"name": n, "label": l, "configured": bool((cfg.get(k) or "").strip())}
        for n, l, k in debrid_keys
    ]
    active = _active_debrid(cfg)
    snap["active_debrid"] = active.name if active else None

    # SQLite cache stats.
    cache_stats = {"db_path": cache_db._CACHE_DB_PATH}
    try:
        with sqlite3.connect(cache_db._CACHE_DB_PATH) as conn:
            now = _admin_time.time()
            cache_stats["debrid_library_rows"] = conn.execute(
                "SELECT COUNT(*) FROM debrid_library WHERE expires_at > ?", (now,)
            ).fetchone()[0]
            cache_stats["debrid_library_expired"] = conn.execute(
                "SELECT COUNT(*) FROM debrid_library WHERE expires_at <= ?", (now,)
            ).fetchone()[0]
            cache_stats["indexer_query_rows"] = conn.execute(
                "SELECT COUNT(*) FROM indexer_query WHERE expires_at > ?", (now,)
            ).fetchone()[0]
            cache_stats["indexer_query_expired"] = conn.execute(
                "SELECT COUNT(*) FROM indexer_query WHERE expires_at <= ?", (now,)
            ).fetchone()[0]
        try:
            cache_stats["db_size_bytes"] = os.path.getsize(cache_db._CACHE_DB_PATH)
        except Exception:
            cache_stats["db_size_bytes"] = None
    except Exception as e:
        cache_stats["error"] = f"{type(e).__name__}: {e}"
    snap["cache"] = cache_stats

    return snap


@app.get("/admin")
@app.get("/{config}/admin")
async def admin(request: Request, config: str = ""):
    """Combined JSON / HTML endpoint. Browsers (Accept: text/html) get
    a styled dashboard; API clients (Accept: application/json) get
    raw JSON."""
    cfg = _config_from(request, None)
    snap = _admin_snapshot(cfg)

    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return snap

    return HTMLResponse(_render_admin_html(snap))


def _render_admin_html(snap: dict) -> str:
    """Tiny single-screen dashboard. No JS, no external deps —
    refresh the page to update."""
    addon = snap["addon"]
    cache = snap["cache"]

    def _fmt_bytes(n):
        if not n: return "0 B"
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _fmt_uptime(s):
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {sec}s"
        return f"{sec}s"

    indexer_rows = "".join(
        f'<tr><td>{i["id"]}</td><td>{i["label"]}</td>'
        f'<td>{"✓" if i["enabled"] else "—"}</td>'
        f'<td class="muted">{i["blocked_by"] or ""}</td></tr>'
        for i in snap["indexers"]
    )
    debrid_rows = "".join(
        f'<tr><td>{d["name"]}</td><td>{d["label"]}</td>'
        f'<td>{"✓" if d["configured"] else "—"}</td>'
        f'<td>{"<b>active</b>" if d["name"] == snap["active_debrid"] else ""}</td></tr>'
        for d in snap["debrids"]
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Audimo Indexers — admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; padding:24px 20px; font:14px/1.4 -apple-system,system-ui,sans-serif;
  background:#0e0f12; color:#e6e6e6; max-width:900px; margin-left:auto; margin-right:auto; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.sub {{ color:#888; font-size:12px; margin-bottom:24px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:18px; }}
.card {{ background:#14161a; border:1px solid #2a2d33; border-radius:8px; padding:14px 16px; }}
.card h2 {{ font-size:13px; margin:0 0 10px; color:#ccc; font-weight:600; text-transform:uppercase; letter-spacing:.05em; }}
.kv {{ display:flex; justify-content:space-between; padding:3px 0; font-size:13px; }}
.kv .k {{ color:#aaa; }}
.kv .v {{ font-family:ui-monospace,monospace; color:#fff; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:5px 8px; border-bottom:1px solid #232529; }}
th {{ color:#888; font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:.05em; }}
.muted {{ color:#666; font-size:12px; }}
.refresh {{ color:#9cf; font-size:12px; margin-top:18px; }}
</style></head><body>
<h1>Audimo Indexers</h1>
<div class="sub">v{addon['version']} · {("hosted" if addon['hosted'] else "self-hosted")} · uptime {_fmt_uptime(addon['uptime_seconds'])}</div>

<div class="grid">
  <div class="card">
    <h2>Cache</h2>
    <div class="kv"><span class="k">debrid_library rows</span><span class="v">{cache.get('debrid_library_rows','?')}</span></div>
    <div class="kv"><span class="k">indexer_query rows</span><span class="v">{cache.get('indexer_query_rows','?')}</span></div>
    <div class="kv"><span class="k">expired (awaiting purge)</span><span class="v">{(cache.get('debrid_library_expired',0) or 0) + (cache.get('indexer_query_expired',0) or 0)}</span></div>
    <div class="kv"><span class="k">db file size</span><span class="v">{_fmt_bytes(cache.get('db_size_bytes') or 0)}</span></div>
    <div class="kv"><span class="k">db path</span><span class="v" style="font-size:11px">{cache.get('db_path','')}</span></div>
  </div>
  <div class="card">
    <h2>Pipeline</h2>
    <div class="kv"><span class="k">indexers</span><span class="v">{len(snap['indexers'])}</span></div>
    <div class="kv"><span class="k">debrids</span><span class="v">{len(snap['debrids'])}</span></div>
    <div class="kv"><span class="k">active debrid</span><span class="v">{snap.get('active_debrid') or '—'}</span></div>
    <div class="kv"><span class="k">peering</span><span class="v" style="font-size:11px">desktop sidecar</span></div>
  </div>
</div>

<div class="card" style="margin-bottom:18px">
  <h2>Indexers</h2>
  <table><thead><tr><th>id</th><th>label</th><th>enabled</th><th>blocked by</th></tr></thead>
  <tbody>{indexer_rows}</tbody></table>
</div>

<div class="card" style="margin-bottom:18px">
  <h2>Debrids</h2>
  <table><thead><tr><th>name</th><th>label</th><th>configured</th><th>active</th></tr></thead>
  <tbody>{debrid_rows}</tbody></table>
</div>

<div class="refresh">Refresh the page to update — no auto-poll.</div>
</body></html>"""


def _public_manifest() -> dict:
    """MANIFEST view tailored to the current server's auth posture.
    When AUDIMO_ADDON_KEY is unset, drop the "Hosted access" section
    from settings_schema so the configure form doesn't ask the user
    to type a key the server isn't enforcing. Pure presentation —
    capabilities, ids, etc. are unchanged.
    """
    if _ADDON_KEY:
        return MANIFEST
    schema = [s for s in MANIFEST.get("settings_schema") or []
              if not (isinstance(s, dict) and s.get("label") == "Hosted access")]
    return {**MANIFEST, "settings_schema": schema}


@app.get("/manifest.json")
@app.get("/{config}/manifest.json")
async def manifest(config: str = "") -> dict:
    return _public_manifest()


# ──────────────────────────────────────────────────────────────────
# /configure — HTML form rendered from settings_schema.
#
# Mirrors audimo_aio's flow: user fills out fields, hits "Generate
# install URL", form builds a base64url-encoded config segment and
# emits a URL like ``http://host/<cfg>/manifest.json``. Audimo app
# stores that URL; subsequent calls land on
# ``/<cfg>/resolve/sources`` etc. and ``_config_from`` reads the
# config from path_params.
# ──────────────────────────────────────────────────────────────────


_CONFIGURE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Configure — __NAME__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; padding: 40px 20px; font-family: -apple-system, BlinkMacSystemFont,
      "Segoe UI", Roboto, sans-serif;
    background: #0e0f12; color: #e6e6e6; min-height: 100vh;
    display: flex; justify-content: center;
  }
  .wrap { max-width: 600px; width: 100%; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 28px; }
  fieldset {
    border: 1px solid #2a2d33; border-radius: 10px;
    margin: 0 0 20px; padding: 18px 20px 22px;
  }
  legend { padding: 0 8px; color: #ccc; font-weight: 600; }
  .section-desc { color: #777; font-size: 12px; margin: -4px 0 14px; }
  .field { margin-top: 14px; }
  .field label { display: block; font-size: 13px; color: #aaa; margin: 0 0 6px; }
  .field .req { color: #e57; }
  .desc { font-size: 12px; color: #777; margin: -3px 0 6px; }
  input[type=text], input[type=password] {
    width: 100%; box-sizing: border-box; padding: 10px 12px;
    background: #1a1c20; color: #e6e6e6;
    border: 1px solid #2a2d33; border-radius: 8px; font-size: 14px;
    font-family: inherit;
  }
  input[type=text]:focus, input[type=password]:focus { outline: none; border-color: #4a90e2; }
  .toggle-row { display: flex; align-items: center; gap: 10px; padding: 10px 0; }
  .toggle-row input[type=checkbox] { width: 18px; height: 18px; accent-color: #4a90e2; margin: 0; }
  .toggle-row .toggle-text { display: flex; flex-direction: column; }
  .toggle-row .toggle-text strong { font-size: 14px; color: #e6e6e6; font-weight: 500; }
  .toggle-row .toggle-text .desc { margin: 2px 0 0; }
  .submit {
    margin-top: 8px; padding: 12px 22px; background: #4a90e2; color: #fff;
    border: none; border-radius: 8px; font-size: 14px; font-weight: 600;
    cursor: pointer;
  }
  .submit:hover { background: #5aa0f2; }
  .out {
    margin-top: 24px; padding: 16px; background: #14161a;
    border: 1px solid #2a2d33; border-radius: 8px;
    word-break: break-all; font-family: ui-monospace, Menlo, monospace;
    font-size: 13px; color: #9cf;
  }
  .out.empty { color: #555; }
  .copy {
    margin-top: 10px; padding: 7px 14px; font-size: 12px;
    background: #2a2d33; color: #ccc;
    border: none; border-radius: 6px; cursor: pointer;
  }
  .copy:hover { background: #3a3d43; }
  .hint { margin-top: 10px; font-size: 12px; color: #777; }
  .hidden { display: none !important; }
  .test-row { margin-top: 6px; display: flex; align-items: center; gap: 10px; }
  .test-btn {
    padding: 5px 12px; background: #2a2d33; color: #ccc;
    border: 1px solid #3a3d43; border-radius: 6px; font-size: 12px;
    cursor: pointer; font-family: inherit;
  }
  .test-btn:hover:not(:disabled) { background: #3a3d43; color: #fff; }
  .test-btn:disabled { opacity: 0.6; cursor: progress; }
  .test-result { font-size: 13px; flex: 1; min-width: 0; word-break: break-word; }
  .test-result.ok  { color: #6dd58c; }
  .test-result.err { color: #e57373; }
  .test-result.pending { color: #888; }
</style>
</head>
<body>
<div class="wrap">
  <h1>__NAME__</h1>
  <div class="sub">Build your install URL. Settings are baked into the path — no per-user state on the addon.</div>
  <form id="f">
    __FIELDS__
    <button type="submit" class="submit">Save</button>
  </form>
  <div id="out" class="out empty">Fill the form to generate your install URL.</div>
  <button class="copy" id="copyBtn" style="display:none">Copy</button>
  <div class="hint" id="hint">Tip: when opened from inside Audimo, Save sends settings back automatically. Otherwise paste the URL into Audimo → Addons → Install (replace any existing install).</div>
</div>
<script>
const SCHEMA = __SCHEMA__;
const PREFILL = __PREFILL__;
const ADDON_ID = "__ADDON_ID__";
function* leafFields(schema) {
  for (const f of schema) {
    if (f.type === 'section' && Array.isArray(f.fields)) yield* leafFields(f.fields);
    else yield f;
  }
}
const ALL = Array.from(leafFields(SCHEMA));
function getEl(key) { return document.getElementById('inp_' + key); }
function isFilled(key) {
  const el = getEl(key);
  if (!el) return false;
  if (el.type === 'checkbox') return el.checked;
  return (el.value || '').trim() !== '';
}
function applyVisibility() {
  for (const f of ALL) {
    if (!f.show_if) continue;
    const wrap = document.getElementById('wrap_' + f.key);
    if (!wrap) continue;
    wrap.classList.toggle('hidden', !isFilled(f.show_if));
  }
}
for (const f of ALL) {
  const el = getEl(f.key);
  if (!el) continue;
  const pre = PREFILL[f.key];
  if (pre == null) continue;
  if (el.type === 'checkbox') {
    el.checked = pre === true || pre === 'true' || pre === '1' || pre === 1;
  } else {
    el.value = pre;
  }
}
document.addEventListener('input', applyVisibility);
document.addEventListener('change', applyVisibility);
applyVisibility();
function b64url(s) {
  return btoa(s).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
}
function buildInstallUrl() {
  const obj = {};
  for (const f of ALL) {
    const el = getEl(f.key);
    if (!el) continue;
    if (el.type === 'checkbox') {
      obj[f.key] = el.checked;
    } else {
      const v = (el.value || '').trim();
      if (v) obj[f.key] = v;
    }
  }
  const cfg = b64url(JSON.stringify(obj));
  return location.origin + '/' + cfg + '/manifest.json';
}
document.getElementById('f').addEventListener('submit', (e) => {
  e.preventDefault();
  const url = buildInstallUrl();
  const out = document.getElementById('out');
  out.textContent = url;
  out.classList.remove('empty');
  document.getElementById('copyBtn').style.display = 'inline-block';
  // Audimo's AddonsView listens for an install-url postMessage so the
  // addon can be installed directly from this page (window opens via
  // the in-app Configure button). Listener is strict about shape:
  // type='tunnel-addon:install' (colon, not hyphen) and addonId must
  // be present so it can validate origin against the installed addon
  // by id. Was previously missing both — Save silently no-op'd.
  try {
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage({
        type: 'tunnel-addon:install',
        url: url,
        addonId: ADDON_ID,
      }, '*');
      const hint = document.getElementById('hint');
      if (hint) hint.textContent = 'Sent to Audimo ✓ — you can close this tab.';
    }
  } catch {}
});
async function runTest(kind, btn, resultEl) {
  let body, endpoint = '/test/' + kind;
  if (kind === 'rd') {
    body = { rd_api_key: (getEl('rd_api_key')?.value || '').trim() };
    if (!body.rd_api_key) {
      resultEl.className = 'test-result err';
      resultEl.textContent = 'Enter your Real-Debrid API key first.';
      return;
    }
  } else if (kind === 'alldebrid') {
    body = { alldebrid_api_key: (getEl('alldebrid_api_key')?.value || '').trim() };
    if (!body.alldebrid_api_key) {
      resultEl.className = 'test-result err';
      resultEl.textContent = 'Enter your AllDebrid API key first.';
      return;
    }
  } else if (kind === 'torbox') {
    body = { torbox_api_key: (getEl('torbox_api_key')?.value || '').trim() };
    if (!body.torbox_api_key) {
      resultEl.className = 'test-result err';
      resultEl.textContent = 'Enter your TorBox API key first.';
      return;
    }
  } else if (kind === 'premiumize') {
    body = { premiumize_api_key: (getEl('premiumize_api_key')?.value || '').trim() };
    if (!body.premiumize_api_key) {
      resultEl.className = 'test-result err';
      resultEl.textContent = 'Enter your Premiumize API key first.';
      return;
    }
  } else if (kind === 'debridlink') {
    body = { debridlink_api_key: (getEl('debridlink_api_key')?.value || '').trim() };
    if (!body.debridlink_api_key) {
      resultEl.className = 'test-result err';
      resultEl.textContent = 'Enter your Debrid-Link API key first.';
      return;
    }
  } else if (kind === 'easydebrid') {
    body = { easydebrid_api_key: (getEl('easydebrid_api_key')?.value || '').trim() };
    if (!body.easydebrid_api_key) {
      resultEl.className = 'test-result err';
      resultEl.textContent = 'Enter your EasyDebrid API key first.';
      return;
    }
  } else { return; }
  btn.disabled = true;
  resultEl.className = 'test-result pending';
  resultEl.textContent = 'Testing…';
  try {
    const r = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    resultEl.className = 'test-result ' + (d.ok ? 'ok' : 'err');
    resultEl.textContent = (d.ok ? '✓ ' : '✗ ') + (d.message || (d.ok ? 'OK' : 'Failed'));
  } catch (e) {
    resultEl.className = 'test-result err';
    resultEl.textContent = '✗ Network error: ' + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}
document.querySelectorAll('button.test-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const kind = btn.getAttribute('data-test');
    const resultEl = document.getElementById('test_result_' + kind);
    runTest(kind, btn, resultEl);
  });
});
document.getElementById('copyBtn').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(document.getElementById('out').textContent);
    document.getElementById('copyBtn').textContent = 'Copied ✓';
    setTimeout(() => { document.getElementById('copyBtn').textContent = 'Copy'; }, 1500);
  } catch {}
});
</script>
</body>
</html>
"""


# Field key → /test/<kind> endpoint suffix. When a field is rendered
# whose key matches, an inline "Test" button is appended below the
# input so the user can verify the credential in place rather than
# scrolling down to a separate test panel.
_FIELD_TEST_KINDS = {
    "rd_api_key": "rd",
    "alldebrid_api_key": "alldebrid",
    "torbox_api_key": "torbox",
    "premiumize_api_key": "premiumize",
    "debridlink_api_key": "debridlink",
    "easydebrid_api_key": "easydebrid",
}


def _render_field_html(f: dict) -> str:
    key = f.get("key", "")
    ftype = f.get("type", "text")
    label = f.get("label") or key
    desc = f.get("description") or ""
    desc_html = f'<div class="desc">{desc}</div>' if desc else ""
    if ftype == "boolean":
        checked = " checked" if f.get("default") else ""
        return (
            f'<div class="field toggle-row" id="wrap_{key}">'
            f'<input id="inp_{key}" type="checkbox"{checked}>'
            f'<div class="toggle-text"><strong>{label}</strong>{desc_html}</div>'
            f'</div>'
        )
    input_type = "password" if ftype == "password" else "text"
    placeholder = f.get("placeholder") or ""
    required = ' <span class="req">*</span>' if f.get("required") else ""
    test_kind = _FIELD_TEST_KINDS.get(key)
    test_html = (
        f'<div class="test-row">'
        f'<button type="button" class="test-btn" data-test="{test_kind}">Test</button>'
        f'<span class="test-result" id="test_result_{test_kind}"></span>'
        f'</div>'
    ) if test_kind else ""
    return (
        f'<div class="field" id="wrap_{key}">'
        f'<label for="inp_{key}">{label}{required}</label>'
        f'{desc_html}'
        f'<input id="inp_{key}" type="{input_type}" placeholder="{placeholder}" autocomplete="off">'
        f'{test_html}'
        f'</div>'
    )


def _render_section_html(section: dict) -> str:
    label = section.get("label") or ""
    desc = section.get("description") or ""
    desc_html = f'<div class="section-desc">{desc}</div>' if desc else ""
    inner = "\n      ".join(_render_field_html(f) for f in (section.get("fields") or []))
    return (
        f'<fieldset><legend>{label}</legend>{desc_html}\n      {inner}\n    </fieldset>'
    )


def _render_configure(prefill: dict) -> str:
    # Match what /manifest.json hands clients: when no addon-key is
    # enforced server-side, drop the "Hosted access" form section so
    # the user doesn't type a value the server ignores.
    schema = _public_manifest().get("settings_schema") or []
    parts: list[str] = []
    for entry in schema:
        if entry.get("type") == "section":
            parts.append(_render_section_html(entry))
        else:
            parts.append(_render_field_html(entry))
    fields_html = "\n    ".join(parts)
    html = _CONFIGURE_HTML
    html = html.replace("__NAME__", MANIFEST.get("name", "Audimo addon"))
    html = html.replace("__FIELDS__", fields_html)
    html = html.replace("__SCHEMA__", json.dumps(schema))
    html = html.replace("__ADDON_ID__", MANIFEST.get("id", ""))
    html = html.replace("__PREFILL__", json.dumps(prefill))
    return html


@app.get("/configure", response_class=HTMLResponse)
@app.get("/{config}/configure", response_class=HTMLResponse)
def configure(request: Request):
    raw = request.path_params.get("config", "") or ""
    prefill: dict = {}
    if raw:
        try:
            import base64
            pad = "=" * (-len(raw) % 4)
            decoded = base64.urlsafe_b64decode(raw + pad).decode("utf-8")
            obj = json.loads(decoded)
            if isinstance(obj, dict):
                prefill = obj
        except Exception:
            pass
    return HTMLResponse(_render_configure(prefill))


@app.post("/test/rd")
async def test_rd(payload: dict) -> dict:
    """Verify a Real-Debrid API key authenticates. Used by the
    "Test API key" button on /configure. Returns
    {ok: bool, message: str} — never echoes the key."""
    key = (payload.get("rd_api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{RD_BASE}/user",
                headers=_rd_headers(key),
            )
            if r.status_code == 200:
                d = r.json()
                username = d.get("username") or "?"
                acct_type = d.get("type") or "?"
                return {"ok": True, "message": f"Authenticated as {username} ({acct_type})"}
            if r.status_code == 401:
                return {"ok": False, "message": "Invalid key (HTTP 401)"}
            return {"ok": False, "message": f"RD returned HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.post("/test/easydebrid")
async def test_easydebrid(payload: dict) -> dict:
    """Verify an EasyDebrid API key via /v1/user/details."""
    key = (payload.get("easydebrid_api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{EasyDebridClient.BASE}/v1/user/details",
                headers={"Authorization": f"Bearer {key}"},
            )
            if r.status_code != 200:
                return {"ok": False, "message": f"EasyDebrid returned HTTP {r.status_code}"}
            data = r.json() or {}
            user_id = data.get("id") or "?"
            paid = data.get("paid_until") or data.get("paidUntil") or 0
            note = ""
            if paid:
                import datetime as _dt
                note = f" (paid until {_dt.date.fromtimestamp(int(paid)).isoformat()})"
            return {"ok": True, "message": f"Authenticated as {user_id}{note}"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.post("/test/debridlink")
async def test_debridlink(payload: dict) -> dict:
    """Verify a Debrid-Link API key via /account/infos."""
    key = (payload.get("debridlink_api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{DebridLinkClient.BASE}/account/infos",
                headers={"Authorization": f"Bearer {key}"},
            )
            if r.status_code != 200:
                return {"ok": False, "message": f"Debrid-Link returned HTTP {r.status_code}"}
            wrapper = r.json() or {}
            if not wrapper.get("success"):
                err = wrapper.get("error") or "auth failed"
                return {"ok": False, "message": str(err)}
            value = wrapper.get("value") or {}
            username = value.get("username") or value.get("email") or "?"
            premium = value.get("premiumLeft")
            note = f" ({premium}s left)" if premium else ""
            return {"ok": True, "message": f"Authenticated as {username}{note}"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.post("/test/premiumize")
async def test_premiumize(payload: dict) -> dict:
    """Verify a Premiumize API key via /api/account/info.
    Returns {ok, message}; never echoes the key."""
    key = (payload.get("premiumize_api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{PremiumizeClient.BASE}/account/info",
                params={"apikey": key},
            )
            if r.status_code != 200:
                return {"ok": False, "message": f"Premiumize returned HTTP {r.status_code}"}
            resp = r.json()
            if resp.get("status") != "success":
                return {"ok": False, "message": resp.get("message") or "auth failed"}
            email = resp.get("customer_id") or resp.get("email") or "?"
            premium = resp.get("premium_until")
            note = f" (premium until {premium})" if premium else ""
            return {"ok": True, "message": f"Authenticated as {email}{note}"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.post("/test/torbox")
async def test_torbox(payload: dict) -> dict:
    """Verify a TorBox API key authenticates via /v1/api/user/me."""
    key = (payload.get("torbox_api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{TorBoxClient.BASE}/user/me",
                headers={"Authorization": f"Bearer {key}"},
            )
            if r.status_code != 200:
                return {"ok": False, "message": f"TorBox returned HTTP {r.status_code}"}
            wrapper = r.json()
            if not wrapper.get("success"):
                err = wrapper.get("error") or wrapper.get("detail") or "unknown"
                return {"ok": False, "message": f"{err}"}
            user = wrapper.get("data") or {}
            email = user.get("email") or "?"
            plan = user.get("plan") or "?"
            return {"ok": True, "message": f"Authenticated as {email} (plan {plan})"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.post("/test/alldebrid")
async def test_alldebrid(payload: dict) -> dict:
    """Verify an AllDebrid API key authenticates. Returns
    {ok: bool, message: str}. Never echoes the key."""
    key = (payload.get("alldebrid_api_key") or "").strip()
    if not key:
        return {"ok": False, "message": "No key provided"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{AllDebridClient.BASE_V4}/user",
                headers={"Authorization": f"Bearer {key}"},
            )
            if r.status_code == 200:
                wrapper = r.json()
                if wrapper.get("status") != "success":
                    err = (wrapper.get("error") or {})
                    return {"ok": False, "message": f"{err.get('code','?')}: {err.get('message','?')}"}
                user = ((wrapper.get("data") or {}).get("user") or {})
                username = user.get("username") or "?"
                premium = "premium" if user.get("isPremium") else "free"
                return {"ok": True, "message": f"Authenticated as {username} ({premium})"}
            if r.status_code == 401:
                return {"ok": False, "message": "Invalid key (HTTP 401)"}
            return {"ok": False, "message": f"AllDebrid returned HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.post("/search/books")
@app.post("/{config}/search/books")
async def search_books(payload: dict, request: Request, config: str = "") -> dict:
    """Audiobook discovery via AudiobookBay listings.

    Companion to audimo-aio's Open Library search — same response
    shape, different catalog. Open Library has metadata for every
    book ever printed but not all of them have audiobooks; ABB has
    actual audiobook torrents but only what's been seeded.
    Surfacing both lets the user pick the right path: hit Open
    Library when they know the book and want to find it everywhere,
    hit ABB when they want only books that actually have audio
    available.

    Honours the user's `src_audiobookbay_enabled` toggle — if ABB is
    disabled in the addon config we return an empty list rather than
    silently re-enabling it via this endpoint.
    """
    cfg = _config_from(request, payload)
    # Same enabled-check the resolve.sources path uses — applies the
    # default_enabled fallback so a fresh install with no baked config
    # still queries ABB.
    if not _source_enabled("audiobookbay", cfg):
        return {"books": []}
    q = (payload.get("q") or payload.get("query") or "").strip()
    if not q:
        return {"books": []}
    limit = int(payload.get("limit") or 30)
    limit = max(1, min(limit, 60))
    try:
        books = await search_audiobookbay_books(cfg, q, limit=limit)
    except Exception as e:
        print(f"[search.books] audiobookbay error: {type(e).__name__}: {e}")
        return {"books": []}
    return {"books": books}


@app.post("/resolve/sources")
@app.post("/{config}/resolve/sources")
async def resolve_sources(payload: dict, request: Request, config: str = "") -> dict:
    """Run all enabled indexers in parallel, dedupe by info_hash,
    return a flat ``sources`` list. Each source carries
    ``addon_id = "audimo-indexers"`` so the aggregator routes
    follow-up resolve.stream / cache.resolve calls back here."""
    title = (payload.get("title") or "").strip()
    artist = (payload.get("artist") or "").strip()
    album = (payload.get("album") or "").strip()
    kind = (payload.get("kind") or "").strip().lower()
    if not title:
        raise HTTPException(400, "title required")
    # Was 10. The picker now wants the long tail so a curated "Top
    # picks" cluster can sit above an "All results" list.
    limit = int(payload.get("limit") or 50)

    cfg = _config_from(request, payload)
    debrid = _active_debrid(cfg)
    ctx = {"artist": artist, "title": title, "album": album, "kind": kind}

    runnable: list[dict] = []
    for spec in _SOURCES.values():
        if not _source_enabled(spec["id"], cfg):
            continue
        if spec["requires"](cfg):
            continue
        # Skip indexers that don't serve this track kind. Without
        # this AudiobookBay shows up in music pickers (and vice
        # versa) — confusing for users and rarely helpful.
        if not _kind_matches(spec, kind):
            continue
        runnable.append(spec)

    if not runnable:
        return {"sources": []}

    # Kick the debrid-downloaded fetch off in parallel with indexers
    # so we don't double round-trip latency. Cached calls (within
    # _RD_DOWNLOADED_TTL) are essentially free. Works for whichever
    # debrid the user has configured.
    rd_task = asyncio.create_task(debrid.fetch_downloaded()) if debrid else None

    batch = await asyncio.gather(
        *(spec["search"](cfg, ctx) for spec in runnable),
        return_exceptions=True,
    )

    raw: list[dict] = []
    seen: set[str] = set()
    for spec, results in zip(runnable, batch):
        if isinstance(results, BaseException):
            print(f"[sources] {spec['id']} raised: "
                  f"{type(results).__name__}: {str(results)[:200]}")
            continue
        for r in results or []:
            if is_video(r.get("name", "")):
                continue
            k = (r.get("info_hash") or "").upper() or r.get("name", "")
            if not k or k in seen:
                continue
            seen.add(k)
            raw.append(r)

    # RD cache flagging — best-effort. Match by info_hash first, fall
    # back to normalized-name comparison since some indexers don't
    # surface info_hash for every result.
    rd_hashes: set[str] = set()
    rd_names: set[str] = set()
    if rd_task is not None:
        try:
            rd_hashes, rd_names = await rd_task
        except Exception:
            pass
    for t in raw:
        h = (t.get("info_hash") or "").upper()
        if h and h in rd_hashes:
            t["rd_cached"] = True
        elif rd_names and _normalize_torrent_name(t.get("name", "")) in rd_names:
            t["rd_cached"] = True
        else:
            t["rd_cached"] = False

    # RTN-equivalent ranking: parse each torrent name for format,
    # bitrate, year, and version tags. Stamps version_tags onto the
    # source for the picker, and folds quality + version-penalty into
    # the sort key.
    for t in raw:
        nm = t.get("name", "")
        q = _parse_torrent_quality(nm)
        # Display-only badges: quality (FLAC/320k) + version variants
        # (Live/Instrumental/…). Quality no longer factors into ranking
        # — search is sorted purely by seeders + rd_cached.
        version_tags, _ = _detect_version_tags(nm, title)
        t["version_tags"] = list(q["tags"]) + [v.replace("_", " ").title() for v in version_tags]
        if q["year"]:
            t.setdefault("year", q["year"])

    # Drop video torrents — music videos / TV rips share the artist+
    # title phrase but are useless as audio sources. Cheap filename
    # check, no relevance scoring needed.
    raw = [t for t in raw if not is_video(t.get("name", ""))]

    # ── File-list verification ─────────────────────────────────────
    # For indexers that expose a cheap file-list endpoint, fetch the
    # filenames and confirm the requested track is actually inside.
    # Stamps `_verified`:
    #   True  — file list contains a leaf matching the title phrase
    #   False — file list fetched but track not present (drop)
    #   None  — file list unavailable (apibay only for v1; other
    #           indexers fall through here pending implementation)
    #
    # Bounded to the top ~30 by current ranking so we don't hammer
    # apibay on every search. Rows past the cutoff stay unverified —
    # they're below the fold anyway and the user is unlikely to scroll
    # to them.
    pre_sorted = sorted(
        raw,
        key=lambda t: (
            t.get("rd_cached", False),
            t.get("seeders", 0),
        ),
        reverse=True,
    )
    verify_targets = [t for t in pre_sorted[:30]
                      if t.get("source") == "apibay" and t.get("_apibay_id")]
    if verify_targets:
        async with httpx.AsyncClient(timeout=6.0) as vclient:
            file_lists = await asyncio.gather(
                *(_apibay_files(vclient, t["_apibay_id"]) for t in verify_targets),
                return_exceptions=True,
            )
        for t, files in zip(verify_targets, file_lists):
            if isinstance(files, BaseException) or not files:
                t["_verified"] = None
                continue
            t["_verified"] = _files_contain_track(files, title)
            if t["_verified"]:
                t["_file_idx"] = _pick_file_idx(files, title, artist)
    for t in raw:
        t.setdefault("_verified", None)

    # Drop torrents we explicitly confirmed don't contain the track.
    # Unverified (None) and verified-True both stay.
    confirmed_or_unknown = [t for t in raw if t.get("_verified") is not False]
    if confirmed_or_unknown:
        raw = confirmed_or_unknown

    # Pure top-seeded ranking, with rd_cached as the only tier above.
    # The 3-query strategy (album / discography / track) keeps results
    # focused without needing relevance or verified-file tiers.
    raw.sort(
        key=lambda t: (
            t.get("rd_cached", False),
            t.get("seeders", 0),
        ),
        reverse=True,
    )

    sources: list[dict] = []
    for t in raw[:limit]:
        sid = t.get("info_hash") or t["name"]
        sources.append({
            "id": sid,
            "kind": "torrent",
            "name": t["name"],
            "link": t.get("rd_link", ""),
            "link_type": t.get("link_type", "magnet"),
            "info_hash": t.get("info_hash", ""),
            "seeders": t.get("seeders", 0),
            "size": t.get("size", 0),
            "source": t.get("source", ""),
            "rd_cached": t.get("rd_cached", False),
            "query_type": t.get("query_type", ""),
            # topic_id is opaque per-source state used by lazy-magnet
            # indexers (audiobookbay, rutracker). The picker round-trips
            # it to resolve.stream so the addon can fetch the .torrent
            # / detail page at play time.
            "topic_id": t.get("topic_id", ""),
            "version_tags": t.get("version_tags", []),
            "year": t.get("year"),
            "addon_id": MANIFEST["id"],
            # File-list verification: True (✅ confirmed file inside),
            # False (filtered out earlier), None (unverified — indexer
            # didn't expose a cheap file list). Picker renders ✅ only
            # for True, nothing for None.
            "verified": t.get("_verified"),
            # Index of the matching audio file in the torrent (set
            # alongside _verified=True). Frontend hands this to the
            # bundled core libtorrent server so it streams the right
            # file directly.
            "file_idx": t.get("_file_idx"),
            # BEP-15-verified peer endpoints (when verify_torrents is
            # on and a tracker responded). The frontend forwards these
            # to the streaming sidecar's /<ih>/create so libtorrent
            # connects them directly, bypassing DHT on the hot path.
            "peers": t.get("_peers", []),
        })

    # Live-tracker verification: drops dead torrents and attaches a
    # peer list to each surviving source. Default on; user can
    # disable in settings if their network blocks UDP or they want
    # absolute lowest search latency.
    if _bool(cfg, "verify_torrents", default=True):
        sources = await cache_db._verify_sources(sources, list(TRACKERS))
        # Live tracker verification rewrites `seeders` with the count
        # the tracker actually reported, which can differ from what the
        # indexer scraped. Re-sort so the picker shows highest-live-
        # seeders first instead of the indexer-time order.
        sources.sort(
            key=lambda s: (
                s.get("rd_cached", False),
                s.get("seeders", 0),
            ),
            reverse=True,
        )

    return {"sources": sources}


@app.post("/resolve/sources/stream")
@app.post("/{config}/resolve/sources/stream")
async def resolve_sources_stream(payload: dict, request: Request, config: str = "") -> StreamingResponse:
    """SSE variant — emits one ``section`` per indexer as it finishes,
    then ``done``. Aggregator uses the simpler /resolve/sources path
    today, but we provide this for parity with audimo_aio so other
    callers (a future direct-extension UI, e.g.) can stream too."""
    title = (payload.get("title") or "").strip()
    artist = (payload.get("artist") or "").strip()
    album = (payload.get("album") or "").strip()
    kind = (payload.get("kind") or "").strip().lower()
    if not title:
        raise HTTPException(400, "title required")
    limit = int(payload.get("limit") or 50)

    cfg = _config_from(request, payload)
    debrid = _active_debrid(cfg)
    ctx = {"artist": artist, "title": title, "album": album, "kind": kind}

    runnable: list[dict] = [
        spec for spec in _SOURCES.values()
        if _source_enabled(spec["id"], cfg)
        and not spec["requires"](cfg)
        and _kind_matches(spec, kind)
    ]

    async def gen() -> AsyncGenerator[bytes, None]:
        queue: asyncio.Queue = asyncio.Queue()
        rd_task = (
            asyncio.create_task(debrid.fetch_downloaded())
            if debrid else None
        )

        def _shape_sources(spec: dict, results, rd_h: set[str], rd_n: set[str]) -> list[dict]:
            out: list[dict] = []
            for r in results or []:
                nm = r.get("name", "")
                if is_video(nm):
                    continue
                h = (r.get("info_hash") or "").upper()
                if h and h in rd_h:
                    rd_cached = True
                elif rd_n and _normalize_torrent_name(nm) in rd_n:
                    rd_cached = True
                else:
                    rd_cached = False
                q = _parse_torrent_quality(nm)
                version_tags, _ = _detect_version_tags(nm, title)
                sid = r.get("info_hash") or nm
                out.append({
                    "id": sid,
                    "kind": "torrent",
                    "name": nm,
                    "link": r.get("rd_link", ""),
                    "link_type": r.get("link_type", "magnet"),
                    "info_hash": r.get("info_hash", ""),
                    "seeders": r.get("seeders", 0),
                    "size": r.get("size", 0),
                    "source": r.get("source", ""),
                    "rd_cached": rd_cached,
                    "query_type": r.get("query_type", ""),
                    "topic_id": r.get("topic_id", ""),
                    "version_tags": list(q["tags"]) + [v.replace("_"," ").title() for v in version_tags],
                    "year": q["year"],
                    "addon_id": MANIFEST["id"],
                    # Pull verification from the raw row — set by the
                    # post-search verification pass below for indexers
                    # that expose a file-list endpoint (apibay today).
                    # None means "unknown / not verified yet".
                    "verified": r.get("_verified"),
                    # Index of the matching audio file within the
                    # torrent — set alongside _verified=True in the
                    # verification pass. The frontend hands this to
                    # the bundled core libtorrent server so it streams
                    # the right file directly. None when verification
                    # didn't run / couldn't pick (caller falls back
                    # to the server's "largest playable file" pick).
                    "file_idx": r.get("_file_idx"),
                })
            # Sort: rd_cached → seeders. Pure top-seeded ranking,
            # no relevance / quality / verified tiers — the 3-query
            # search strategy (album / discography / track) keeps
            # results focused without scoring.
            out.sort(
                key=lambda s: (
                    s.get("rd_cached", False),
                    s.get("seeders", 0),
                ),
                reverse=True,
            )
            return out[:limit]

        async def run(spec: dict):
            try:
                results = await spec["search"](cfg, ctx)
            except BaseException as e:
                results = e
            if isinstance(results, BaseException):
                await queue.put({
                    "type": "section",
                    "indexer": spec["id"],
                    "label": spec["label"],
                    "icon": spec.get("icon", ""),
                    "sources": [],
                    "error": f"{type(results).__name__}: {str(results)[:200]}",
                })
                return
            # Three-phase emit:
            #   1. shape + emit with no RD marks (instant first paint)
            #   2. RD cache check completes → re-emit with rd_cached marks
            #   3. file-list verification completes → drop wrong-torrent
            #      matches and re-emit. This is what stops a
            #      Blink-182 "Enema of the State" torrent (high
            #      seeders, name matches the artist) ranking #1 for "I
            #      Miss You" — verification fetches the file list and
            #      drops it because no file matches the title.
            await queue.put({
                "type": "section",
                "indexer": spec["id"],
                "label": spec["label"],
                "icon": spec.get("icon", ""),
                "sources": _shape_sources(spec, results, set(), set()),
            })
            rd_h: set[str] = set()
            rd_n: set[str] = set()
            if rd_task is not None:
                try:
                    rd_h, rd_n = await rd_task
                except Exception:
                    pass
                if rd_h or rd_n:
                    await queue.put({
                        "type": "section",
                        "indexer": spec["id"],
                        "label": spec["label"],
                        "icon": spec.get("icon", ""),
                        "sources": _shape_sources(spec, results, rd_h, rd_n),
                    })

            # Verification pass. Two paths:
            #  • apibay  — cheap /f.php file-list endpoint, fast.
            #  • everyone else — fetch metadata via libtorrent (DHT)
            #    capped at the top N candidates per indexer so we
            #    don't blow up the swarm. Cached per-infohash for
            #    30 days, so the second search of the same query
            #    is instant. Both paths stamp _verified
            #    True/False/None and then drop only the False rows.
            try:
                async with httpx.AsyncClient(timeout=6.0) as vclient:
                    # apibay is cheap — its detail endpoint returns the
                    # file list directly. We don't run a libtorrent
                    # metadata fetch here anymore: this addon is
                    # public-host-safe and never opens a peer
                    # connection. Sources we can't verify cheaply
                    # stay marked _verified=None so the picker shows
                    # them without a checkmark, and the user's local
                    # streaming sidecar picks the right file at play
                    # time using its own largest-audio-file heuristic.
                    apibay_targets = [
                        r for r in results
                        if r.get("source") == "apibay" and r.get("_apibay_id")
                    ][:30]
                    apibay_tasks = [
                        asyncio.create_task(_apibay_files(vclient, r["_apibay_id"]))
                        for r in apibay_targets
                    ]
                    if not apibay_targets:
                        return
                    apibay_files = await asyncio.gather(*apibay_tasks, return_exceptions=True)
                    for r, files in zip(apibay_targets, apibay_files):
                        if isinstance(files, BaseException) or not files:
                            r["_verified"] = None
                        else:
                            r["_verified"] = _files_contain_track(files, title)
                            if r["_verified"]:
                                r["_file_idx"] = _pick_file_idx(files, title, artist)
                # Drop torrents we explicitly confirmed don't contain
                # the track. Keep verified=True and verified=None
                # (unknown). Fall back to the unfiltered list if
                # everything would be dropped, so the picker is never
                # empty just because metadata happened to be slow.
                survived = [r for r in results if r.get("_verified") is not False]
                if survived:
                    results = survived
                await queue.put({
                    "type": "section",
                    "indexer": spec["id"],
                    "label": spec["label"],
                    "icon": spec.get("icon", ""),
                    "sources": _shape_sources(spec, results, rd_h, rd_n),
                })
            except Exception as e:
                print(f"[verify] {spec['id']}: {type(e).__name__}: {str(e)[:200]}")

        workers = [asyncio.create_task(run(s)) for s in runnable]

        async def signal_done():
            await asyncio.gather(*workers, return_exceptions=True)
            await queue.put(None)
        asyncio.create_task(signal_done())

        while True:
            try:
                ev = await queue.get()
            except asyncio.CancelledError:
                break
            if ev is None:
                break
            yield _sse(ev)
            if await request.is_disconnected():
                break
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/resolve/stream")
@app.post("/{config}/resolve/stream")
async def resolve_stream(payload: dict, request: Request, config: str = "") -> StreamingResponse:
    """Debrid-only playback path.

    This addon never peers torrents itself; libtorrent lives in the
    user's local Audimo desktop streaming sidecar (port 11471). The
    flow is:

      1. emit progress "Checking <debrid>…"
      2. _check_rd_cache(info_hash) — instant if user already has it
      3. If miss and rd_cached flag was set OR we have a hash, addMagnet,
         poll for download, unrestrict, emit ``ready``.
      4. If no debrid is configured OR the debrid has no copy, emit an
         ``unsupported`` SSE event with code ``torrent_no_debrid`` and
         echo back the magnet/info_hash. The desktop client routes
         that to its bundled streaming sidecar to peer the torrent
         locally — the addon never sees torrent bytes.
    """
    source = payload.get("source") or {}
    track = payload.get("track") or {}
    cfg = _config_from(request, payload)

    if source.get("kind") != "torrent":
        async def err():
            yield _sse({
                "type": "error",
                "code": "unsupported_source_kind",
                "message": "this addon only handles kind='torrent' (Phase 3a)",
            })
        return StreamingResponse(err(), media_type="text/event-stream")

    rd_link = source.get("link") or ""
    src_id = (source.get("source") or "").lower()
    topic_id = (source.get("topic_id") or "").strip()

    # AudiobookBay sources are listing-only; the magnet lives on the
    # detail page. Fetch it lazily at play time so we don't hammer the
    # site during search. If the fetch fails, the user gets a clear
    # 400 rather than a hung libtorrent.
    if not rd_link and src_id == "audiobookbay" and topic_id:
        magnet = await _abb_fetch_magnet(cfg, topic_id)
        if magnet:
            rd_link = magnet

    # Pre-fetch authenticated .torrent bytes for sources whose magnet
    # in the search result is a placeholder (private trackers we have
    # creds for). For rutracker, the .torrent's announce URL embeds
    # the user's passkey — bare magnets would never find peers.
    pre_torrent_bytes: bytes | None = None
    if src_id == "rutracker" and topic_id:
        bb = (cfg.get("rutracker_bb_session") or "").strip()
        if bb:
            pre_torrent_bytes = await _rt_topic_torrent_bytes(bb, topic_id)
            if not pre_torrent_bytes:
                print(f"[resolve.stream] rutracker .torrent fetch failed t={topic_id}", flush=True)

    if not rd_link and not pre_torrent_bytes:
        async def err():
            yield _sse({
                "type": "error",
                "code": "missing_link",
                "message": "source.link required (or fetched .torrent for private tracker)",
            })
        return StreamingResponse(err(), media_type="text/event-stream")

    debrid = _active_debrid(cfg)
    title = (track.get("title") or "").strip()
    artist = (track.get("artist") or "").strip()
    album = (track.get("album") or "").strip()
    track_kind = (track.get("kind") or "").strip().lower()
    info_hash = (source.get("info_hash") or _extract_btih(rd_link) or "").lower()
    name = source.get("name") or title
    link_type = source.get("link_type") or "magnet"
    rd_cached = bool(source.get("rd_cached"))
    seeders = int(source.get("seeders") or 0)

    # `organize_root` is the user's library directory we look in for
    # an already-saved copy of this track (the existing-file fast
    # path below). Pre-libtorrent-removal versions of this addon used
    # to write here too; today, only the desktop streaming sidecar
    # writes — but legacy entries on disk still play through this
    # path, and a future client could write under the same scheme.
    if track_kind == "audiobook":
        organize_root = _audiobook_save_dir(cfg)
    else:
        organize_root = _permanent_music_dir(cfg)

    print(
        f"[resolve.stream] info_hash={info_hash[:12]} rd_cached={rd_cached} "
        f"link_type={link_type!r} debrid={debrid.name if debrid else 'none'} "
        f"track_kind={track_kind!r}",
        flush=True,
    )

    # Existing-file fast path: if the organized destination already
    # holds a complete, content-bearing file (written by the desktop
    # streaming sidecar on a prior play), serve it directly so any
    # cache.resolve redispatch lands on the local copy without
    # round-tripping debrid.
    #
    # libtorrent (in the sidecar) pre-allocates the full destination
    # size before any bytes have arrived, so file-exists + size>0
    # isn't enough — a cancelled mid-download leaves a sparse
    # all-zeros file. Verify actual content by reading the first 16
    # bytes and checking that they aren't all zero. Real audio formats
    # all start with magic bytes (FLAC=`fLaC`, MP3=`ID3` or `\xFF\xFB`,
    # M4A/M4B contains `ftyp` early, etc.) — none start with 16 zero
    # bytes.
    if organize_root:
        for guess in (".flac", ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".wav"):
            organized_rel = _organized_relpath(track_kind, title, artist, album, guess)
            candidate = os.path.join(organize_root, organized_rel)
            if not (os.path.exists(candidate) and os.path.getsize(candidate) > 1024):
                continue
            try:
                with open(candidate, "rb") as f:
                    head = f.read(16)
            except Exception:
                continue
            if head == b"\x00" * 16:
                # Sparse partial — sidecar pre-allocated but never wrote.
                continue
            mime = _AUDIO_MIME_MAP.get(guess, "audio/mpeg")
            async def existing_file_ready():
                yield _sse({
                    "type": "ready",
                    "stream_url": "/file?path=" + urllib.parse.quote(candidate, safe=""),
                    "stream_url_relative": True,
                    "filename": os.path.basename(candidate),
                    "mime_type": mime,
                    "info_hash": info_hash,
                    "torrent_id": "",
                    "rd_link": rd_link,
                    "source_label": "Local",
                    "seeders": 0,
                    "addon_local_file": candidate,
                })
            return StreamingResponse(
                existing_file_ready(),
                media_type="text/event-stream",
            )

    def _peering_handoff_event() -> dict:
        """Tell the desktop client we have nothing to stream and it
        should peer the torrent locally via its bundled streaming
        sidecar. The client matches on code='torrent_no_debrid'."""
        return {
            "type": "unsupported",
            "code": "torrent_no_debrid",
            "message": (
                "No debrid hit for this torrent. The desktop app's "
                "streaming sidecar will peer it locally."
            ),
            "info_hash": info_hash,
            "magnet": rd_link if link_type == "magnet" else "",
            "name": name,
            "seeders": seeders,
        }

    async def gen() -> AsyncGenerator[bytes, None]:
        if debrid is None:
            yield _sse(_peering_handoff_event())
            return

        async with httpx.AsyncClient(timeout=20) as client:
            try:
                yield _sse({"type": "progress", "pct": 5, "message": f"Checking {debrid.label}…"})

                rd_info = None
                if info_hash:
                    rd_info = await debrid.find_cached(client, info_hash)

                if rd_info:
                    yield _sse({"type": "progress", "pct": 80, "message": f"Found in {debrid.label} — unrestricting…"})
                elif rd_cached and (rd_link or info_hash):
                    # Indexer flagged it cached → fast addMagnet.
                    # For non-cached, hand off to the desktop streaming
                    # sidecar (see _peering_handoff_event) — peering
                    # locally beats waiting for debrid to stage it.
                    yield _sse({"type": "progress", "pct": 30, "message": f"Adding to {debrid.label}…"})

                    progress_q: asyncio.Queue = asyncio.Queue()

                    async def on_progress(pct, msg):
                        await progress_q.put({"type": "progress", "pct": pct, "message": msg})

                    add_task = asyncio.create_task(debrid.add_and_wait(
                        client, rd_link, name, link_type, info_hash,
                        on_progress=on_progress,
                        torrent_bytes=pre_torrent_bytes,
                    ))
                    try:
                        while not add_task.done():
                            try:
                                ev = await asyncio.wait_for(progress_q.get(), timeout=0.5)
                                yield _sse(ev)
                            except asyncio.TimeoutError:
                                continue
                        rd_info = await add_task
                    except (asyncio.CancelledError, GeneratorExit):
                        add_task.cancel()
                        raise
                    finally:
                        if not add_task.done():
                            add_task.cancel()

                if not rd_info or not rd_info.get("links"):
                    # Debrid had no cached or live copy. Hand off to
                    # the desktop's local streaming sidecar to peer
                    # the torrent — see _peering_handoff_event docs.
                    yield _sse(_peering_handoff_event())
                    return

                yield _sse({"type": "progress", "pct": 90, "message": "Unrestricting audio…"})
                audio = await debrid.unrestrict_audio(client, rd_info["links"], title, artist)
                if not audio:
                    yield _sse({
                        "type": "error",
                        "code": "no_audio",
                        "message": f"{debrid.label} returned no audio files matching the track",
                    })
                    return

                ready_hash = (rd_info.get("hash") or info_hash or "").lower()
                source_label = (
                    debrid.source_label_cached if rd_cached
                    else debrid.source_label_live
                )
                yield _sse({
                    "type": "ready",
                    "stream_url": audio["download"],
                    "filename": audio["filename"],
                    "mime_type": audio.get("mimeType") or "audio/mpeg",
                    "info_hash": ready_hash,
                    "torrent_id": rd_info.get("id"),
                    "rd_link": audio.get("rd_link", audio["download"]),
                    "source_label": source_label,
                    "debrid": debrid.name,
                    "seeders": seeders,
                })
            except Exception as e:
                yield _sse({
                    "type": "error",
                    "code": "internal",
                    "message": f"{type(e).__name__}: {e}",
                })

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/debrid_cache_check")
@app.post("/{config}/debrid_cache_check")
async def debrid_cache_check(payload: dict, request: Request, config: str = "") -> dict:
    """Synchronously check whether the active debrid backend has this
    torrent cached and ready to stream. The orchestrator races this
    against libtorrent peering on click — if RD has the file (most
    popular content does), this returns a playable CDN URL in 1-2s
    while libtorrent is still doing DHT bootstrap, and the user gets
    instant playback.

    Body: { info_hash, title?, artist? }
    Returns: { cached: bool, url?, label?, filename?, mime_type? }

    Snappy by design — 8s timeout. Caller falls back to libtorrent
    on a "not cached" result OR on timeout. No background work; the
    side-effect-free cousin of /push_to_debrid.
    """
    info_hash = (payload.get("info_hash") or "").strip()
    title = (payload.get("title") or "").strip()
    artist = (payload.get("artist") or "").strip()
    if not info_hash:
        raise HTTPException(400, "info_hash required")
    cfg = _config_from(request, payload)
    debrid = _active_debrid(cfg)
    if debrid is None:
        return {"cached": False, "reason": "no debrid configured"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            info = await debrid.find_cached(client, info_hash)
            if not info or not info.get("links"):
                return {"cached": False}
            audio = await debrid.unrestrict_audio(client, info["links"], title, artist)
            if not audio:
                return {"cached": False, "reason": "no audio file matched"}
            return {
                "cached": True,
                "url": audio["download"],
                "filename": audio.get("filename") or "",
                "mime_type": audio.get("mimeType") or "audio/mpeg",
                "label": debrid.source_label_cached,
            }
    except Exception as e:
        print(f"[debrid-check] {info_hash[:12]} {type(e).__name__}: {e}", flush=True)
        return {"cached": False, "reason": f"{type(e).__name__}: {e}"}


@app.post("/push_to_debrid")
@app.post("/{config}/push_to_debrid")
async def push_to_debrid(payload: dict, request: Request, config: str = "") -> dict:
    """Push a torrent to the user's configured debrid backend.

    Used by the bundled-streaming-server flow: when core has streamed
    bytes via libtorrent and saved the file locally, the frontend
    calls this so the addon's debrid backend (RD/AD/TB/Premiumize/...)
    also caches the torrent. Future plays can then skip libtorrent
    entirely and serve from the debrid CDN.

    Body shape:
      info_hash (required)
      magnet (required when no debrid-side cache hit)
      title, artist (optional, used by unrestrict_audio file matching)
      cache_key (opaque, echoed back to on_complete_url)
      on_complete_url (optional, POSTed to when push succeeds)

    Returns immediately with {ok: true, queued: true|false}. The
    actual debrid work runs in the background — caller doesn't have
    to wait.
    """
    info_hash = (payload.get("info_hash") or "").strip()
    magnet = (payload.get("magnet") or "").strip()
    title = (payload.get("title") or "").strip()
    artist = (payload.get("artist") or "").strip()
    cache_key = (payload.get("cache_key") or "").strip()
    on_complete_url = (payload.get("on_complete_url") or "").strip()

    if not info_hash or not magnet:
        raise HTTPException(400, "info_hash and magnet required")

    cfg = _config_from(request, payload)
    debrid = _active_debrid(cfg)
    if debrid is None:
        return {"ok": True, "queued": False, "reason": "no debrid configured"}

    name = title or magnet[:80]
    # Caller (frontend orchestrator) can pass delete_local explicitly,
    # which it does by reading addon.settings.delete_local_after_debrid_cache
    # from localStorage. We honour the explicit value when present;
    # otherwise fall back to cfg (URL-config-segment) so a flow that
    # bypasses the orchestrator (curl, direct addon call) still works.
    if isinstance(payload.get("delete_local"), bool):
        delete_local = payload["delete_local"]
    else:
        delete_local = bool(cfg.get("delete_local_after_debrid_cache"))
    print(f"[push-to-debrid] {info_hash[:12]} delete_local={delete_local}", flush=True)

    async def background():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                info = await debrid.add_and_wait(
                    client, magnet, name, "magnet", info_hash,
                )
                # add_and_wait gives up after ~2 min of polling. RD
                # often takes 5-10 min to actually finish caching a
                # newly-added torrent. The torrent shows up in the
                # user's RD library later, but our callback already
                # bailed and the local copy never got deleted.
                #
                # Keep polling find_cached every 30s for up to 30 min
                # so slow caches still resolve. Idempotent — once RD
                # has it, we get the info and continue.
                if not info or not info.get("links"):
                    print(f"[push-to-debrid] {info_hash[:12]} {debrid.label}: add_and_wait gave up, long-polling for cache completion (up to 30 min)", flush=True)
                    for attempt in range(60):  # 60 * 30s = 30 min
                        await asyncio.sleep(30)
                        try:
                            info = await debrid.find_cached(client, info_hash)
                        except Exception as e:
                            print(f"[push-to-debrid] {info_hash[:12]} long-poll error: {e}", flush=True)
                            continue
                        if info and info.get("links"):
                            print(f"[push-to-debrid] {info_hash[:12]} cached after {(attempt+1)*30}s of long-polling", flush=True)
                            break
                    if not info or not info.get("links"):
                        print(f"[push-to-debrid] {info_hash[:12]} {debrid.label}: not cached after 30 min", flush=True)
                        return
                audio = await debrid.unrestrict_audio(
                    client, info["links"], title, artist,
                )
                if not audio:
                    print(f"[push-to-debrid] {info_hash[:12]} {debrid.label}: no audio file matched", flush=True)
                    return
                print(f"[push-to-debrid] {info_hash[:12]} → {debrid.label} ✓", flush=True)
                if not on_complete_url or not cache_key:
                    return
                # POST back to the orchestrator/backend so it can
                # promote the cache row's streamUrl to the debrid CDN
                # and (per the user's toggle) delete the local copy.
                try:
                    await client.post(on_complete_url, json={
                        "cache_key": cache_key,
                        "info_hash": info_hash,
                        "debrid_url": audio["download"],
                        "debrid_label": (
                            debrid.source_label_cached if info.get("from_cache")
                            else debrid.source_label_live
                        ),
                        "filename": audio.get("filename") or "",
                        "mime_type": audio.get("mimeType") or "audio/mpeg",
                        "delete_local": delete_local,
                    }, timeout=10)
                except Exception as e:
                    print(f"[push-to-debrid] {info_hash[:12]} on_complete_url failed: {e}", flush=True)
        except Exception as e:
            print(f"[push-to-debrid] {info_hash[:12]} error: {type(e).__name__}: {e}", flush=True)

    asyncio.create_task(background())
    return {"ok": True, "queued": True, "debrid": debrid.label}


@app.post("/cache/resolve")
@app.post("/{config}/cache/resolve")
async def cache_resolve(payload: dict, request: Request, config: str = "") -> dict:
    """Re-resolve a previously-stored library entry to a fresh stream URL.

    Entries this addon writes during /resolve/stream's ``ready`` event
    carry ``rd_link`` + ``torrent_id`` + ``debrid`` so we can re-resolve
    via the originating debrid rather than re-running the full
    add-and-poll dance.

    Dispatch:
      * If the entry's ``debrid`` is RD (or unset, for legacy entries),
        use RD's fast torrents/info → unrestrict path.
      * Otherwise, try a single-link unrestrict via the user's
        currently-active debrid. Some debrids re-unrestrict cleanly;
        for those that don't, the user re-plays via the live flow.
    """
    entry = payload.get("entry") or {}
    cfg = _config_from(request, payload)

    # Local-file fast path: if the entry was originally completed via
    # libtorrent and saved into the user's library, serve it from disk.
    # No debrid needed — the bytes are already here.
    addon_local = (entry.get("addon_local_file") or "").strip()
    if addon_local:
        real = os.path.realpath(addon_local)
        roots: list[str] = []
        try: roots.append(os.path.realpath(_permanent_music_dir(cfg)))
        except Exception: pass
        try: roots.append(os.path.realpath(_audiobook_save_dir(cfg)))
        except Exception: pass
        inside = any(
            (lambda r: (lambda c: c == r)(os.path.commonpath([real, r])))(root)
            for root in roots if root
        ) if roots else False
        # libtorrent pre-allocates files at full size before any bytes
        # arrive — a cancelled mid-download leaves a sparse all-zeros
        # file. Verify actual content (first 16 bytes != all zero)
        # before claiming this file is playable.
        if inside and os.path.exists(real) and os.path.getsize(real) > 1024:
            try:
                with open(real, "rb") as f:
                    head_bytes = f.read(16)
            except Exception:
                head_bytes = b""
            if head_bytes and head_bytes != b"\x00" * 16:
                ext = os.path.splitext(real)[1].lower()
                mime = _AUDIO_MIME_MAP.get(ext, entry.get("mimeType") or "audio/mpeg")
                return {
                    "streamUrl": "/file?path=" + urllib.parse.quote(real, safe=""),
                    "streamUrlRelative": True,
                    "filename": entry.get("filename", os.path.basename(real)),
                    "mimeType": mime,
                    "source": entry.get("source", "Local"),
                    "albumCover": entry.get("albumCover"),
                    "seeders": 0,
                }
            # File is allocated but empty / sparse — fall through to
            # try debrid recovery before surfacing local_file_missing.
        # File is gone or unusable. If the entry has a debrid handle
        # (rd_link or torrent_id) AND a debrid is configured, fall
        # through to the debrid fast path below — the user shouldn't
        # be prompted for re-download when we can silently re-resolve
        # via RD/AD/etc. Only surface local_file_missing when there's
        # genuinely no recovery path.
        has_debrid_handle = bool(
            (entry.get("rd_link") or "").strip()
            or (entry.get("torrent_id") or "").strip()
        )
        if not (has_debrid_handle and _active_debrid(cfg) is not None):
            return {
                "local_file_missing": True,
                "expected_path": addon_local,
                "redispatch_payload": {
                    "source": entry.get("source_payload") or {},
                    "track": entry.get("track_payload") or {},
                },
            }
        # else: continue to the debrid block below.

    debrid = _active_debrid(cfg)
    if debrid is None:
        # No debrid configured — but if the entry carries a
        # source_payload we can still play it via libtorrent. Return a
        # redispatch envelope so the caller falls through to
        # /resolve/stream, where the existing libtorrent path picks it
        # up. This is the no-RD/Reddit-stranger flow: hosted aggregator
        # finds the torrent → local sidecar peers it → audio plays. A
        # 400 here used to break the library-replay path on devices
        # that don't have debrid configured on this addon.
        if entry.get("source_payload"):
            track_payload = entry.get("track_payload") or {
                "title": entry.get("track_title", ""),
                "artist": entry.get("track_artist", ""),
                "album": entry.get("track_album", ""),
                "kind": (entry.get("category") or "").strip().lower() or "music",
            }
            return {
                "redispatch": True,
                "endpoint": "resolve.stream",
                "payload": {
                    "source": entry.get("source_payload") or {},
                    "track": track_payload,
                },
            }
        raise HTTPException(400, "no debrid configured")

    rd_link = (entry.get("rd_link") or "").strip()
    torrent_id = (entry.get("torrent_id") or "").strip()
    title = (entry.get("track_title") or "").strip()
    artist = (entry.get("track_artist") or "").strip()
    entry_debrid = (entry.get("debrid") or "").strip().lower()

    async with httpx.AsyncClient(timeout=20) as client:
        # RD fast path: hit torrents/info for the freshest links list.
        # Surviving an RD unrestrict URL rotation is the whole point.
        # Only meaningful when the entry was originally produced by
        # RD AND the user still has RD configured (debrid.name == "rd").
        if (entry_debrid in ("rd", "")) and debrid.name == "rd" and torrent_id:
            try:
                r = await client.get(
                    f"{RD_BASE}/torrents/info/{torrent_id}",
                    headers=_rd_headers(debrid.api_key),
                )
                if r.status_code == 200:
                    info = r.json()
                    if info.get("links"):
                        audio = await debrid.unrestrict_audio(
                            client, info["links"], title, artist,
                        )
                        if audio:
                            return {
                                "streamUrl": audio["download"],
                                "filename": audio["filename"],
                                "mimeType": audio.get("mimeType") or "audio/mpeg",
                                "source": entry.get("source", debrid.source_label_cached),
                                "albumCover": entry.get("albumCover"),
                                "seeders": 0,
                            }
            except Exception as e:
                print(f"[cache.resolve] torrents/info path failed: {e}")

        # Fallback: single-link unrestrict via whichever debrid is
        # active. Works for RD (re-unrestricting a stale link) and
        # for AllDebrid; TorBox/Premiumize don't accept arbitrary
        # URL inputs to their unrestrict — they'll return None and
        # the user falls through to the live flow.
        if rd_link:
            audio = await debrid.unrestrict_audio(
                client, [rd_link], title, artist,
            )
            if audio:
                return {
                    "streamUrl": audio["download"],
                    "filename": audio["filename"],
                    "mimeType": audio.get("mimeType") or "audio/mpeg",
                    "source": entry.get("source", debrid.source_label_cached),
                    "albumCover": entry.get("albumCover"),
                    "seeders": 0,
                }

    # Debrid had nothing for us — but if the entry carries a
    # source_payload we can re-run resolve.stream end-to-end
    # (libtorrent + bg debrid cache). Returning redispatch is
    # friendlier than a 404: the orchestrator follows the envelope
    # back through resolve.stream automatically.
    #
    # Track metadata: prefer the explicit track_payload, but fall
    # back to entry-level fields for older rows that didn't snapshot
    # one. Without this fallback an entry written before track_payload
    # was added redispatches with `track: {}` and the addon's
    # resolve.stream rejects the empty body. Synthesizing the track
    # from track_title/track_artist/track_album/category is enough
    # for the kind-aware save path to work.
    if entry.get("source_payload"):
        track_payload = entry.get("track_payload") or {
            "title": entry.get("track_title", ""),
            "artist": entry.get("track_artist", ""),
            "album": entry.get("track_album", ""),
            "kind": (entry.get("category") or "").strip().lower() or "music",
        }
        return {
            "redispatch": True,
            "endpoint": "resolve.stream",
            "payload": {
                "source": entry.get("source_payload") or {},
                "track": track_payload,
            },
        }

    raise HTTPException(404, f"couldn't re-resolve via {debrid.label}")


def _pick_file_idx(files: list[str], title: str, artist: str = "") -> int | None:
    """Pick the index of the audio file in `files` that best matches
    the requested track. Used at search time — when an indexer (e.g.
    apibay) returns the torrent's file list cheaply, we score each
    file against the title/artist and stamp the best index onto the
    source as ``file_idx``. The desktop streaming sidecar reads that
    on play and streams the right file directly without re-running
    its own selection heuristic.

    Returns None when no file scores above zero (no playable audio,
    or no file actually matches the title — the caller should leave
    file_idx unset and let the streaming sidecar fall back to its
    largest-file heuristic).
    """
    if not files or not title:
        return None
    best_idx = None
    best_score = 0
    for i, fpath in enumerate(files):
        ext = os.path.splitext(fpath or "")[1].lower()
        if ext not in _AUDIO_EXTS:
            continue
        score = _score_audio_file(fpath, title, artist)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


_AUDIO_EXTS = {".mp3", ".flac", ".aac", ".m4a", ".m4b", ".ogg", ".opus", ".wav"}
_AUDIO_MIME_MAP = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".aac": "audio/aac",
    ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".mp4": "audio/mp4",
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".wav": "audio/wav",
}


# ── Version-tag detection ─────────────────────────────────────────
# Tag a torrent/file name as "this is the original studio recording"
# vs "this is some non-original variant the user almost certainly
# didn't mean." Each tag carries a score penalty; some tags are
# strong enough that the result sinks well below originals but stays
# in the list (so the user can still pick it if they really want it).
#
# Detection skips tags whose keyword already appears in the user's
# queried title — so a search for "Live at Wembley" isn't demoted.
_VERSION_TAG_RULES: list[tuple[str, str, float]] = [
    ("instrumental", r"\b(instrumental|inst\.?\b)", -8.0),
    ("acapella",     r"\b(acapella|a\s*cappella|vocals?\s*only)\b", -8.0),
    ("karaoke",      r"\bkaraoke\b", -50.0),
    ("remix",        r"\b(remix(es)?|rmx|bootleg|mashup)\b", -5.0),
    ("live",         r"\b(live\s+at|live\s+in|live\s+from|live\s+@|"
                     r"\(live\)|\[live\]|concert\s+version|tour\s+\d{4})\b", -4.0),
    ("acoustic",     r"\b(acoustic\s+version|acoustic\s+session|unplugged)\b", -2.5),
    ("cover",        r"\b(cover(ed)?\s+by|tribute\s+to|piano\s+cover|guitar\s+cover)\b", -4.0),
    ("demo",         r"\b(demo\s+version|early\s+demo|rough\s+demo)\b", -2.0),
    ("speed_edit",   r"\b(sped\s*up|slowed(\s*\+\s*reverb)?|8d\s+audio|nightcore|chopped\s*&?\s*screwed)\b", -7.0),
    ("radio_edit",   r"\b(radio\s+edit|clean\s+version|censored)\b", -1.5),
    ("rehearsal",    r"\b(rehearsal|soundcheck)\b", -3.0),
]
_VERSION_TAG_RES = [
    (label, re.compile(pat, re.IGNORECASE), delta)
    for label, pat, delta in _VERSION_TAG_RULES
]


# ── RTN-equivalent quality parsing ────────────────────────────────
# Comet's Stremio addon uses the rank-torrent-name (RTN) library to
# extract metadata from torrent names. RTN is video-tuned (resolution,
# codec, HDR, audio channels, …) — most of those signals don't apply
# to music. This is a music-focused subset that surfaces:
#   * audio format (FLAC > MP3 > AAC > others) and lossless flag
#   * bitrate when stamped in the name (320, V0, 256k, …)
#   * year (1973 / [2023] / (1995))
# The output rides on each source as ``version_tags`` for the picker
# to render, and feeds into ``_quality_score`` for ranking.

_FORMAT_RES = [
    ("flac",   re.compile(r"\bflac\b", re.I),                10),
    ("alac",   re.compile(r"\balac\b", re.I),                 9),
    ("ape",    re.compile(r"\bape\b",  re.I),                 8),
    ("mp3",    re.compile(r"\bmp3\b|\b320\s*kbps?\b|\bv0\b", re.I), 6),
    ("aac",    re.compile(r"\baac\b|\bm4a\b", re.I),          5),
    ("ogg",    re.compile(r"\bogg\b|\bopus\b", re.I),         5),
]

_BITRATE_RE = re.compile(r"\b(128|160|192|224|256|320)\s*k(?:bps)?\b", re.I)
_BITRATE_VBR_RE = re.compile(r"\bV0\b|\bvbr\s*0?\b", re.I)
_YEAR_RE = re.compile(r"(?:[\(\[\s])((?:19|20)\d{2})(?:[\)\]\s])")
_LOSSLESS_RE = re.compile(r"\bflac\b|\balac\b|\bape\b|\bwav\b|\blossless\b", re.I)


def _parse_torrent_quality(name: str) -> dict:
    """Return {format, bitrate, year, lossless, tags} for a torrent
    name. Designed for music: focuses on audio format + bitrate
    rather than video signals. ``tags`` is a list of human-readable
    badges the picker renders — same format as ``_detect_version_tags``
    output."""
    out: dict = {
        "format": None,
        "bitrate": None,
        "year": None,
        "lossless": False,
        "tags": [],
    }
    for label, regex, _weight in _FORMAT_RES:
        if regex.search(name):
            out["format"] = label
            break
    bm = _BITRATE_RE.search(name)
    if bm:
        out["bitrate"] = int(bm.group(1))
    elif _BITRATE_VBR_RE.search(name):
        out["bitrate"] = "V0"
    out["lossless"] = bool(_LOSSLESS_RE.search(name))
    ym = _YEAR_RE.search(name)
    if ym:
        out["year"] = int(ym.group(1))
    if out["format"]:
        out["tags"].append(out["format"].upper())
    if out["bitrate"]:
        out["tags"].append(f"{out['bitrate']}k" if isinstance(out["bitrate"], int) else str(out["bitrate"]))
    return out


def _quality_score(parsed: dict) -> float:
    """Numeric ranking signal from _parse_torrent_quality output.
    Higher = better. Lossless beats lossy; higher bitrates rank
    above lower; year is a tiebreaker (more recent = slightly
    preferred when format/bitrate are equal)."""
    score = 0.0
    fmt = parsed.get("format")
    if fmt:
        for label, _re, weight in _FORMAT_RES:
            if label == fmt:
                score += float(weight)
                break
    if parsed.get("lossless"):
        score += 4.0
    br = parsed.get("bitrate")
    if isinstance(br, int):
        # Map 128..320 → 0..2 linearly.
        score += max(0.0, min(2.0, (br - 128) / 96.0))
    elif br == "V0":
        score += 2.0
    yr = parsed.get("year")
    if yr and 1950 <= yr <= 2100:
        score += min(1.0, (yr - 1950) / 200.0)
    return score


def _detect_version_tags(name: str, queried_title: str) -> tuple[list[str], float]:
    """Return (tags, total_penalty). Tags whose keyword appears in
    the user's queried title are suppressed so explicit searches
    like "Live at Wembley" aren't demoted.

    Normalises non-alphanumerics in the torrent name to spaces before
    regex matching — many indexers (torrentdownload's RSS feed,
    audiobookbay slugs, etc.) join words with dashes/dots/underscores,
    which would otherwise prevent ``\\b(live\\s+at|...)\\b``-style
    patterns from firing.
    """
    title_lower = (queried_title or "").lower()
    name_norm = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    tags: list[str] = []
    penalty = 0.0
    for label, regex, delta in _VERSION_TAG_RES:
        keyword_in_title = any(
            tok in title_lower
            for tok in regex.pattern.split("|")
            if tok.isalpha() and len(tok) > 3
        )
        if keyword_in_title:
            continue
        if regex.search(name_norm):
            tags.append(label)
            penalty += delta
    return tags, penalty


def _file_has_title_phrase(file_path: str, track_title: str) -> bool:
    """Does this filename contain the track title as a contiguous
    word-boundary phrase? Matches `04 i miss you.flac` for title
    'I Miss You', rejects `09 i really wish i hated you.flac`.

    Tries the title both with and without parenthetical metadata so
    "A Milli (Album Version Explicit)" matches a file named "A Milli".

    Single-word titles (e.g. 'Drive') match anywhere as a whole word."""
    name = _normalize_title_phrase(os.path.basename(file_path))
    padded = f" {name} "
    for variant in _title_phrase_variants(track_title):
        if f" {variant} " in padded:
            return True
    return False


def _score_audio_file(file_path: str, track_title: str, track_artist: str = "") -> int:
    """Score how well an audio file path matches the desired track.
    Higher = better.

    The strongest signal is **phrase presence**: does the filename
    contain the title as a contiguous word-boundary phrase? Without
    this, an album torrent that doesn't actually contain the
    requested track would still pick its closest near-match (e.g.
    "I Really Wish I Hated You" wins for a search of "I Miss You"
    because they share the words "i" and "you"). The phrase bonus
    pushes any actual match far above any near-miss in the same
    torrent.

    Additional signals:
      - per-word title/artist matches (legacy fallback)
      - lossless format bonus (FLAC > AAC)
      - version-tag penalty for variants (instrumental, live, …)"""
    name = os.path.basename(file_path).lower()
    name_clean = re.sub(r"^\d+[\s\-_.]+", "", name)
    name_clean = os.path.splitext(name_clean)[0]

    title_words = set(re.findall(r"[a-z0-9]+", track_title.lower()))
    artist_words = set(re.findall(r"[a-z0-9]+", track_artist.lower()))
    name_words = set(re.findall(r"[a-z0-9]+", name_clean))

    title_matches = len(title_words & name_words)
    artist_matches = len(artist_words & name_words)

    ext = os.path.splitext(file_path)[1].lower()
    format_bonus = 10 if ext == ".flac" else 5 if ext in (".m4a", ".aac") else 0

    # _detect_version_tags returns negative deltas in the same range
    # as torrent-level scoring (~-10 to -50). Magnify here because
    # file scores are smaller (typical title_matches*10 ≈ 30); the
    # penalty has to outrank the original to push variants down.
    _file_tags, file_penalty = _detect_version_tags(name, track_title)
    version_penalty = int(file_penalty * 4)

    # Phrase bonus: filename contains the title as a contiguous
    # word-boundary phrase. Magnify so even a low-quality phrase
    # match outranks every near-miss in a wrong-album torrent.
    phrase_bonus = 1000 if _file_has_title_phrase(file_path, track_title) else 0

    return phrase_bonus + title_matches * 10 + artist_matches * 5 + format_bonus + version_penalty


