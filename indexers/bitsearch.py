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

from ._shared import build_search_queries, make_magnet


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
    """Run the standard 3-query set in parallel, dedupe by info_hash."""
    queries = build_search_queries(title, artist, album)
    if not queries:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        batch = await asyncio.gather(
            *(_bitsearch_query(client, q) for q, _ in queries)
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
