"""BitSearch search (formerly SolidTorrents — solidtorrents.to now 301s here).

bitsearch.to is a public meta-search aggregator with a JSON API.
`category=audio` covers both music and audiobooks. Response shape:
  { "success": true, "results": [
      { "infohash": "…", "title": "…", "size": <bytes>,
        "seeders": <int>, "leechers": <int>, "category": <int>,
        "verified": <bool>, "updatedAt": "…" }, ... ] }
"""
from __future__ import annotations

import asyncio

import httpx

from ._shared import _album_collapses_to_artist, make_magnet


BITSEARCH_BASE = "https://bitsearch.to"


async def _bitsearch_query(client: httpx.AsyncClient, q: str) -> list[dict]:
    try:
        r = await client.get(
            f"{BITSEARCH_BASE}/api/v1/search",
            params={"q": q, "category": "audio", "sort": "seeders"},
        )
        if r.status_code != 200:
            return []
        body = r.json()
        if not body.get("success"):
            return []
        out: list[dict] = []
        for row in body.get("results") or []:
            ih = (row.get("infohash") or "").strip()
            name = row.get("title") or ""
            if not name or not ih:
                continue
            seeders = int(row.get("seeders") or 0)
            size = int(row.get("size") or 0)
            out.append({
                "name": name,
                "seeders": seeders,
                "size": size,
                "rd_link": make_magnet(ih, name),
                "link_type": "magnet",
                "source": "bitsearch",
                "info_hash": ih.upper(),
            })
        return out
    except Exception:
        return []


async def search_bitsearch(artist: str, title: str, album: str = "") -> list[dict]:
    """Parallel queries (artist+album, artist+title, artist-fallback),
    dedupe by info_hash. Same strategy as search_apibay — see there
    for why the artist-only fallback exists."""
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
            *(_bitsearch_query(client, q) for q, _ in queries[:3])
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
