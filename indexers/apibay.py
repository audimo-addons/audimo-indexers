"""apibay (PirateBay) search.

Same scraper that lived in audimo_aio. apibay.org is the public
JSON API behind thepiratebay.org. No auth, no setup. The response
shape:

  [{ "id": "...", "name": "...", "info_hash": "...",
     "leechers": "...", "seeders": "...", "num_files": "...",
     "size": "...", "username": "...", "added": "...",
     "status": "...", "category": "...", "imdb": "" }, ...]

When the search has no results apibay returns a single sentinel row
with id "0" and an all-zero info_hash — we filter that out.
"""
from __future__ import annotations

import asyncio

import httpx

from ._shared import build_search_queries, make_magnet


APIBAY_BASE = "https://apibay.org"


async def _apibay_files(client: httpx.AsyncClient, topic_id: str) -> list[str]:
    """Fetch filenames for an apibay torrent via /f.php?id=<id>.

    Response shape: ``[{"name": ["song.flac"], "size": ["12345"]}, ...]``
    (apibay wraps every value in a one-element list).

    Returns [] on failure — the caller treats that as "unverified",
    not "wrong torrent". Apibay sometimes 404s on older topics, and
    we'd rather keep the source visible than drop it for a transient
    indexer hiccup.
    """
    if not topic_id:
        return []
    try:
        r = await client.get(f"{APIBAY_BASE}/f.php", params={"id": topic_id},
                             timeout=4.0)
        if r.status_code != 200:
            return []
        rows = r.json()
        names: list[str] = []
        for row in rows:
            n = row.get("name")
            if isinstance(n, list) and n:
                names.append(str(n[0]))
            elif isinstance(n, str):
                names.append(n)
        # apibay returns a sentinel row when it doesn't have the file
        # list indexed: [{"name":["Filelist not found"],...}]. Treat as
        # "unknown", not "no files" — otherwise verification flips
        # those torrents to verified=False and drops them.
        if len(names) == 1 and names[0].strip().lower() == "filelist not found":
            return []
        return names
    except Exception:
        return []


async def _apibay_query(client: httpx.AsyncClient, q: str) -> list[dict]:
    try:
        r = await client.get(f"{APIBAY_BASE}/q.php", params={"q": q, "cat": "100"})
        if r.status_code != 200:
            return []
        rows = r.json()
        out: list[dict] = []
        for row in rows:
            ih = (row.get("info_hash") or "").strip()
            name = row.get("name") or ""
            if not name or not ih or ih == "0000000000000000000000000000000000000000":
                continue
            seeders = int(row.get("seeders") or 0)
            size = int(row.get("size") or 0)
            out.append({
                "name": name,
                "seeders": seeders,
                "size": size,
                "rd_link": make_magnet(ih, name),
                "link_type": "magnet",
                "source": "apibay",
                "info_hash": ih.upper(),
                # Numeric topic id — apibay's f.php?id=N file-list
                # endpoint takes this, not info_hash. Captured here so
                # the verification pass can confirm the torrent
                # actually contains the requested track.
                "_apibay_id": str(row.get("id") or "").strip(),
            })
        return out
    except Exception:
        return []


async def search_apibay(artist: str, title: str, album: str = "") -> list[dict]:
    """Run the standard (album / discography / track) query set in
    parallel and dedupe by info_hash. Ranking lives at the server.py
    merge level — this just returns everything the queries surface."""
    queries = build_search_queries(title, artist, album)
    if not queries:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        batch = await asyncio.gather(
            *(_apibay_query(client, q) for q, _ in queries)
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
