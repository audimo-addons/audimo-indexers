"""torrentdownload.info search.

Public, zero-config. Hits the RSS feed (/feed?q=…) instead of the
HTML page so we can regex-parse the XML without lxml. Each <item>:
  <title>…</title>
  <link>…/HASH</link>          ← info_hash is the last path segment (40 hex)
  <description>Size: X.X MB Seeds: N , Peers: N Hash: HASH</description>
"""
from __future__ import annotations

import asyncio
import re

import httpx

from ._shared import _album_collapses_to_artist, make_magnet


TORRENTDOWNLOAD_BASE = "https://www.torrentdownload.info"

_TD_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_TD_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_TD_HASH_RE = re.compile(r"<link>[^<]*/([A-Fa-f0-9]{40})</link>")
_TD_SEEDS_RE = re.compile(r"Seeds:\s*(\d+)")
_TD_SIZE_RE = re.compile(r"Size:\s*(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)", re.IGNORECASE)


async def _td_query(client: httpx.AsyncClient, q: str) -> list[dict]:
    try:
        r = await client.get(f"{TORRENTDOWNLOAD_BASE}/feed", params={"q": q})
        if r.status_code != 200:
            return []
        out: list[dict] = []
        for item in _TD_ITEM_RE.findall(r.text):
            mt = _TD_TITLE_RE.search(item)
            mh = _TD_HASH_RE.search(item)
            if not mt or not mh:
                continue
            name = mt.group(1).strip()
            ih = mh.group(1).upper()
            ms = _TD_SEEDS_RE.search(item)
            seeders = int(ms.group(1)) if ms else 0
            mz = _TD_SIZE_RE.search(item)
            size = 0
            if mz:
                n = float(mz.group(1))
                mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[mz.group(2).upper()]
                size = int(n * mult)
            out.append({
                "name": name,
                "seeders": seeders,
                "size": size,
                "rd_link": make_magnet(ih, name),
                "link_type": "magnet",
                "source": "torrentdownload",
                "info_hash": ih,
            })
        return out
    except Exception:
        return []


async def search_torrentdownload(artist: str, title: str, album: str = "") -> list[dict]:
    """Same multi-query strategy as search_apibay — see there for
    why the artist-only fallback exists."""
    queries: list[tuple[str, str]] = []
    album_collapses = bool(album and artist) and _album_collapses_to_artist(artist, album)
    if album and artist and not album_collapses:
        queries.append((f"{artist} {album}", "album"))
    if artist:
        queries.append((f"{artist} {title}", "track"))
    else:
        queries.append((title, "track"))
    if artist and (album_collapses or not album):
        queries.append((artist, "artist_fallback"))

    async with httpx.AsyncClient(timeout=15) as client:
        batch = await asyncio.gather(
            *(_td_query(client, q) for q, _ in queries[:3])
        )

    seen: set[str] = set()
    out: list[dict] = []
    for results, (_q, qtype) in zip(batch, queries):
        for r in results:
            k = r.get("info_hash") or r["name"]
            if k in seen:
                continue
            seen.add(k)
            r["query_type"] = qtype
            out.append(r)
    return out
