"""Prowlarr search.

Prowlarr is a user-hosted indexer aggregator (Sonarr-family). It
fans queries out across many trackers and returns a normalized
JSON shape. Requires the user's own Prowlarr instance + API key.
"""
from __future__ import annotations

import asyncio

import httpx

from ._shared import build_search_queries, make_magnet


def extract_rd_link(item: dict) -> tuple[str | None, str | None]:
    """Pick the best playable link from a Prowlarr search result.

    Returns (link, link_type) where link_type is one of:
      * ``torrent_proxy``  – Prowlarr URL that serves .torrent bytes (preferred)
      * ``magnet_proxy``   – Prowlarr URL that redirects to a real magnet
      * ``magnet``         – literal magnet:?… URI
    """
    download_url = item.get("downloadUrl", "")
    magnet_url = item.get("magnetUrl", "")
    info_hash = item.get("infoHash", "")
    name = item.get("title", "")
    if download_url and download_url.startswith("http"):
        return download_url, "torrent_proxy"
    if magnet_url and magnet_url.startswith("http"):
        return magnet_url, "magnet_proxy"
    if magnet_url and magnet_url.startswith("magnet:"):
        return magnet_url, "magnet"
    if info_hash and len(info_hash) in (40, 32):
        return make_magnet(info_hash, name), "magnet"
    return None, None


async def _prowlarr_query(
    client: httpx.AsyncClient, base: str, key: str, q: str
) -> list[dict]:
    try:
        r = await client.get(
            f"{base}/api/v1/search",
            params={"query": q, "type": "search", "limit": 50, "offset": 0},
            headers={"X-Api-Key": key},
        )
        if r.status_code != 200:
            return []
        out: list[dict] = []
        for item in r.json():
            name = item.get("title", "")
            if not name:
                continue
            link, link_type = extract_rd_link(item)
            if not link:
                continue
            out.append({
                "name": name,
                "seeders": int(item.get("seeders", 0)),
                "size": int(item.get("size", 0)),
                "rd_link": link,
                "link_type": link_type,
                "source": item.get("indexer", "Prowlarr"),
                "info_hash": item.get("infoHash", ""),
            })
        return out
    except Exception:
        return []


async def search_prowlarr(
    base: str, key: str, artist: str, title: str, album: str = ""
) -> list[dict]:
    """Run the standard 3-query set in parallel, dedupe by info_hash."""
    queries = build_search_queries(title, artist, album)
    if not queries:
        return []
    async with httpx.AsyncClient(timeout=25) as client:
        batch = await asyncio.gather(
            *(_prowlarr_query(client, base, key, q) for q, _ in queries)
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
