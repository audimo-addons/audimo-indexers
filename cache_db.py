"""SQLite cache + BEP-15 health verification.

Extracted from server.py. Public surface (used from server.py and
clients/): everything below that doesn't start with an underscore is
considered exported. Internal helpers (`_key_hash`) stay private.

Schema is four tables:
  debrid_library — keyed by (debrid_name, sha256(api_key))
    stores hashes + normalized names of the user's downloaded
    torrents at that debrid. TTL 60s (matches _RD_DOWNLOADED_TTL).
  indexer_query  — keyed by (indexer_name, query)
    stores raw scraper results so re-asking for the same album
    doesn't re-hit the indexer.
  torrent_files  — keyed by info_hash
    cached file lists from libtorrent metadata fetches.
  torrent_health — keyed by info_hash
    BEP-15 announce results: live seeder count + peer list.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from typing import Optional


_CACHE_DB_PATH = os.environ.get("AUDIMO_CACHE_DB") or os.path.join(
    os.path.expanduser("~"), ".audimo-indexers", "cache.db",
)


def _cache_init() -> None:
    """Create the cache DB and indexes. Idempotent."""
    os.makedirs(os.path.dirname(_CACHE_DB_PATH), exist_ok=True)
    with sqlite3.connect(_CACHE_DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS debrid_library (
                debrid TEXT NOT NULL,
                api_key_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                hashes TEXT NOT NULL,
                names TEXT NOT NULL,
                PRIMARY KEY (debrid, api_key_hash)
            );
            CREATE TABLE IF NOT EXISTS indexer_query (
                indexer TEXT NOT NULL,
                query TEXT NOT NULL,
                expires_at REAL NOT NULL,
                results TEXT NOT NULL,
                PRIMARY KEY (indexer, query)
            );
            -- Per-infohash file list, populated by libtorrent metadata
            -- fetches at search-time verification. Long TTL because
            -- file lists for a given infohash are immutable.
            CREATE TABLE IF NOT EXISTS torrent_files (
                info_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL,
                files TEXT NOT NULL  -- JSON array of file paths
            );
            -- BEP-15 announce results: live seeder count + peer list.
            -- Short TTL — peer lists go stale fast and seeder counts
            -- drift; 30 min is long enough that a back-to-back search
            -- doesn't re-scrape, short enough that the picker isn't
            -- handed dead peers an hour later.
            CREATE TABLE IF NOT EXISTS torrent_health (
                info_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL,
                seeders INTEGER NOT NULL,
                peers TEXT NOT NULL  -- JSON array of {ip, port}
            );
            CREATE INDEX IF NOT EXISTS idx_debrid_expires
              ON debrid_library(expires_at);
            CREATE INDEX IF NOT EXISTS idx_indexer_expires
              ON indexer_query(expires_at);
            CREATE INDEX IF NOT EXISTS idx_torrent_files_expires
              ON torrent_files(expires_at);
            CREATE INDEX IF NOT EXISTS idx_torrent_health_expires
              ON torrent_health(expires_at);
        """)


def _key_hash(api_key: str) -> str:
    """Stable, non-reversible identifier for an api_key. sha256
    truncated to 32 hex chars — collision-safe at our scale."""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:32]


def _cache_get_debrid_library(debrid: str, api_key: str) -> tuple[set[str], set[str]] | None:
    """Read cached (hashes, names) for a debrid+key pair. Returns
    None if missing or expired."""
    if not api_key:
        return None
    import time as _time
    try:
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            row = conn.execute(
                "SELECT hashes, names FROM debrid_library "
                "WHERE debrid = ? AND api_key_hash = ? AND expires_at > ?",
                (debrid, _key_hash(api_key), _time.time()),
            ).fetchone()
    except Exception as e:
        print(f"[cache] get debrid_library error: {e}")
        return None
    if not row:
        return None
    try:
        return set(json.loads(row[0])), set(json.loads(row[1]))
    except Exception:
        return None


def _cache_put_debrid_library(
    debrid: str, api_key: str,
    hashes: set[str], names: set[str],
    ttl: float = 60.0,
) -> None:
    if not api_key:
        return
    import time as _time
    try:
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO debrid_library "
                "(debrid, api_key_hash, expires_at, hashes, names) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    debrid, _key_hash(api_key), _time.time() + ttl,
                    json.dumps(sorted(hashes)),
                    json.dumps(sorted(names)),
                ),
            )
    except Exception as e:
        print(f"[cache] put debrid_library error: {e}")


def _cache_get_indexer_query(indexer: str, query: str) -> list[dict] | None:
    """Read cached scraper results for an indexer+query pair."""
    import time as _time
    try:
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            row = conn.execute(
                "SELECT results FROM indexer_query "
                "WHERE indexer = ? AND query = ? AND expires_at > ?",
                (indexer, query, _time.time()),
            ).fetchone()
    except Exception as e:
        print(f"[cache] get indexer_query error: {e}")
        return None
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _cache_put_indexer_query(
    indexer: str, query: str,
    results: list[dict],
    ttl: float = 600.0,
) -> None:
    import time as _time
    try:
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO indexer_query "
                "(indexer, query, expires_at, results) VALUES (?, ?, ?, ?)",
                (indexer, query, _time.time() + ttl, json.dumps(results)),
            )
    except Exception as e:
        print(f"[cache] put indexer_query error: {e}")


def _cache_get_torrent_files(info_hash: str) -> list[str] | None:
    """Read cached file list for a torrent. Returns None on miss/expiry."""
    if not info_hash:
        return None
    import time as _time
    try:
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            row = conn.execute(
                "SELECT files FROM torrent_files "
                "WHERE info_hash = ? AND expires_at > ?",
                (info_hash.upper(), _time.time()),
            ).fetchone()
    except Exception as e:
        print(f"[cache] get torrent_files error: {e}")
        return None
    if not row:
        return None
    try:
        return list(json.loads(row[0]))
    except Exception:
        return None


def _cache_put_torrent_files(
    info_hash: str, files: list[str],
    ttl: float = 30 * 24 * 3600.0,  # 30 days
) -> None:
    """File lists for a given infohash are immutable, so a long TTL
    is safe. Empty file lists ARE cached too — sentinel for "we
    tried, no peers had metadata". Avoid hammering the swarm.
    Use a shorter TTL (1h) for empty results so a transient
    no-peers situation doesn't poison the cache permanently."""
    if not info_hash:
        return
    import time as _time
    try:
        effective_ttl = ttl if files else 3600.0
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO torrent_files "
                "(info_hash, expires_at, files) VALUES (?, ?, ?)",
                (info_hash.upper(), _time.time() + effective_ttl, json.dumps(files)),
            )
    except Exception as e:
        print(f"[cache] put torrent_files error: {e}")


async def _cache_purge_expired_loop() -> None:
    """Periodic cleanup of expired rows so the cache file doesn't
    grow forever. Runs every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        try:
            import time as _time
            with sqlite3.connect(_CACHE_DB_PATH) as conn:
                d1 = conn.execute(
                    "DELETE FROM debrid_library WHERE expires_at < ?",
                    (_time.time(),),
                ).rowcount
                d2 = conn.execute(
                    "DELETE FROM indexer_query WHERE expires_at < ?",
                    (_time.time(),),
                ).rowcount
                d3 = conn.execute(
                    "DELETE FROM torrent_files WHERE expires_at < ?",
                    (_time.time(),),
                ).rowcount
                d4 = conn.execute(
                    "DELETE FROM torrent_health WHERE expires_at < ?",
                    (_time.time(),),
                ).rowcount
            if d1 or d2 or d3 or d4:
                print(f"[cache] purged {d1} debrid + {d2} indexer + {d3} files + {d4} health expired row(s)")
        except Exception as e:
            print(f"[cache] purge error: {type(e).__name__}: {e}")


# ── Torrent health verification (BEP-15 announce) ───────────────────
#
# Two wins for the user:
#
#   1. Drop dead torrents before they reach the picker. The indexer's
#      stale seeder counts are routinely off — apibay reports "10
#      seeders" for a torrent that's had zero peers for months. A
#      live announce reveals reality and we hide the corpse.
#
#   2. Hand verified peer endpoints to the bundled streaming server's
#      /<ih>/create. libtorrent connects them directly, skipping the
#      30-90s DHT bootstrap on first-launch / cold-DHT scenarios. This
#      is what makes Stremio first-launch feel instant — Torrentio
#      verifies + returns peers; we do the same.

import bep15

_HEALTH_CACHE_TTL_S = 30 * 60  # 30 min — peer lists go stale fast.
# Verification budget. Capped concurrency to avoid socket-table
# exhaustion on the addon host (DO droplets have small ulimits) and
# to stay polite to the public-tracker pool.
_HEALTH_VERIFY_CONCURRENCY = 16


def _cache_get_health(info_hash: str) -> Optional[dict]:
    if not info_hash:
        return None
    import time as _time
    try:
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            row = conn.execute(
                "SELECT seeders, peers, expires_at FROM torrent_health "
                "WHERE info_hash = ? AND expires_at > ?",
                (info_hash.upper(), _time.time()),
            ).fetchone()
        if not row:
            return None
        seeders, peers_json, _exp = row
        return {"seeders": int(seeders), "peers": json.loads(peers_json or "[]")}
    except Exception as e:
        print(f"[cache] get torrent_health error: {e}")
        return None


def _cache_put_health(info_hash: str, seeders: int, peers: list, ttl: float = _HEALTH_CACHE_TTL_S) -> None:
    if not info_hash:
        return
    import time as _time
    try:
        with sqlite3.connect(_CACHE_DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO torrent_health "
                "(info_hash, expires_at, seeders, peers) VALUES (?, ?, ?, ?)",
                (info_hash.upper(), _time.time() + ttl, int(seeders), json.dumps(peers)),
            )
    except Exception as e:
        print(f"[cache] put torrent_health error: {e}")


async def _verify_one(info_hash: str, sem: asyncio.Semaphore, trackers: list) -> dict:
    """Run BEP-15 announce against trackers, cache, return health dict."""
    cached = _cache_get_health(info_hash)
    if cached is not None:
        return cached
    async with sem:
        result = await bep15.verify_torrent(
            info_hash, list(trackers),
            per_tracker_timeout=4.0,
            max_trackers=5,
        )
    # Cache even zero-seeder results — stops re-verifying confirmed-
    # dead torrents on every search until the TTL expires.
    _cache_put_health(info_hash, result["seeders"], result["peers"])
    return {"seeders": result["seeders"], "peers": result["peers"]}


async def _verify_sources(sources: list, trackers: list, *, overall_timeout_s: float = 8.0) -> list:
    """Annotate sources with live seeder counts + verified peers, drop
    confirmed-dead torrents (seeders=0 after a tracker actually
    responded). Sources that didn't get a tracker response keep their
    indexer-supplied seeder count and pass through unchanged.

    Runs all unique infohashes in parallel under a concurrency cap.
    Hard overall budget: a slow tracker pool shouldn't make every
    search wait its full per-tracker timeout × number of sources."""
    if not sources:
        return sources
    # Group sources by info_hash so duplicates (same torrent surfaced
    # by multiple indexers) only verify once.
    by_hash: dict[str, list] = {}
    for s in sources:
        ih = (s.get("info_hash") or "").lower()
        if not ih or len(ih) != 40:
            continue
        by_hash.setdefault(ih, []).append(s)
    if not by_hash:
        return sources

    sem = asyncio.Semaphore(_HEALTH_VERIFY_CONCURRENCY)
    tasks = {ih: asyncio.create_task(_verify_one(ih, sem, trackers)) for ih in by_hash}
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=overall_timeout_s,
        )
    except asyncio.TimeoutError:
        # Cancel still-running probes; partial results are still
        # written through the per-task `_cache_put_health` path so
        # next search benefits even from this aborted run.
        for t in tasks.values():
            if not t.done():
                t.cancel()

    out: list = []
    dropped = 0
    seeded_hashes: set[str] = set()
    for ih, group in by_hash.items():
        task = tasks.get(ih)
        health = task.result() if (task and task.done() and not task.cancelled() and not task.exception()) else None
        for s in group:
            if health is None:
                # Verification didn't complete — keep the source as-is.
                out.append(s)
                continue
            seeders = health["seeders"]
            peers = health["peers"]
            if seeders == 0 and not peers:
                # Confirmed dead by every tracker that responded.
                dropped += 1
                continue
            # Replace stale indexer count with the live one when we
            # actually saw seeders; otherwise keep what the indexer
            # told us (some trackers don't return accurate counts).
            if seeders > 0:
                s = {**s, "seeders": seeders}
            if peers:
                s = {**s, "peers": peers}
            seeded_hashes.add(ih)
            out.append(s)
    # Drop any source whose info_hash didn't appear in by_hash (no
    # info_hash supplied) AND keep going — those weren't candidates
    # for verification in the first place.
    leftover = [s for s in sources if (s.get("info_hash") or "").lower() not in by_hash]
    out.extend(leftover)
    if dropped:
        print(f"[verify] dropped {dropped} dead torrent(s); seeded {len(seeded_hashes)}")
    return out


def start_cache() -> None:
    """Initialise the cache DB and kick off the periodic purge loop.
    Called from server.py's startup hook."""
    _cache_init()
    asyncio.create_task(_cache_purge_expired_loop())
