"""RuTracker (private tracker — needs bb_session cookie).

Russian tracker. Login has a captcha so we can't auth ourselves —
the user pastes their bb_session cookie value, and we attach it to
every request. At play time we fetch the .torrent file via dl.php
(with auth, so the announce URL embeds the user's passkey) and pass
it to libtorrent/RD as torrent_bytes — bare magnets stripped from
the topic page lose the passkey and never find peers.
"""
from __future__ import annotations

import re

import httpx

from ._shared import _album_collapses_to_artist
from cache_db import _cache_get_indexer_query, _cache_put_indexer_query


RUTRACKER_BASE = "https://rutracker.org/forum"
RUTRACKER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
RT_FETCH_CAP = 5  # cap parallel topic-page fetches per query


def _rt_cookie_jar(bb_session: str) -> dict:
    s = (bb_session or "").strip()
    if s.lower().startswith("bb_session="):
        s = s[len("bb_session="):]
    if ";" in s:
        s = s.split(";", 1)[0].strip()
    if not s:
        return {}
    return {"bb_session": s, "bb_ssl": "1"}


_RT_ROW_RE = re.compile(
    r'<tr[^>]*class="[^"]*hl-tr[^"]*"[^>]*>(.*?)</tr>',
    re.DOTALL,
)
_RT_TOPIC_RE = re.compile(
    r'<a\s+[^>]*?data-topic_id="(\d+)"[^>]*?>(.*?)</a>',
    re.DOTALL,
)
_RT_SEEDERS_RE = re.compile(r'class="[^"]*seedmed[^"]*"[^>]*>\s*(\d+)')
_RT_SIZE_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(KB|MB|GB|TB)', re.IGNORECASE)
_RT_MAGNET_RE = re.compile(r'magnet:\?xt=urn:btih:([A-Fa-f0-9]{40})[^"\'\s<>]*')


def _rt_parse_size(text: str) -> int:
    m = _RT_SIZE_RE.search(text)
    if not m:
        return 0
    n = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[unit]
    return int(n * mult)


def _rt_strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


async def _rt_topic_torrent_bytes(bb_session: str, topic_id: str) -> bytes | None:
    """Fetch the authenticated .torrent for a rutracker topic_id.

    The .torrent's announce URL embeds the user's passkey — that's
    what makes peer connections actually work on a private tracker.
    Bare magnets stripped from the topic page lose this and can never
    find peers. Local libtorrent uses the .torrent directly.
    """
    cookies = _rt_cookie_jar(bb_session)
    if not cookies or not topic_id:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            cookies=cookies,
            headers={"User-Agent": RUTRACKER_UA},
        ) as client:
            r = await client.get(f"{RUTRACKER_BASE}/dl.php", params={"t": topic_id})
            if r.status_code != 200:
                print(f"[rutracker] dl.php status={r.status_code} t={topic_id}")
                return None
            ct = (r.headers.get("content-type") or "").lower()
            if "bittorrent" not in ct and r.content[:1] != b"d":
                snippet = r.text[:200].replace("\n", " ")
                print(f"[rutracker] dl.php non-torrent (ct={ct}) t={topic_id} body={snippet!r}")
                return None
            return r.content
    except Exception as e:
        print(f"[rutracker] dl.php failed t={topic_id}: {type(e).__name__}: {e}")
        return None


_RT_QUERY_TTL = 600.0


async def _rutracker_query(client: httpx.AsyncClient, q: str) -> list[dict]:
    cached = _cache_get_indexer_query("rutracker", q)
    if cached is not None:
        print(f"[rutracker] q={q!r} cache hit ({len(cached)} results)")
        return [dict(r) for r in cached]
    try:
        r = await client.get(f"{RUTRACKER_BASE}/tracker.php", params={"nm": q})
        if r.status_code != 200:
            print(f"[rutracker] search status={r.status_code} q={q!r}")
            return []
        body = r.text
        is_guest = ("login_username" in body and "login_password" in body)
        rows = _RT_ROW_RE.findall(body)
        print(f"[rutracker] search q={q!r} rows={len(rows)} body_bytes={len(body)} guest={is_guest}")
        if is_guest:
            return []
        if rows and not _RT_TOPIC_RE.search(rows[0]):
            sample = re.sub(r"\s+", " ", rows[0])[:1200]
            print(f"[rutracker] row regex miss; sample row: {sample}")
        parsed: list[tuple[str, str, int, int]] = []
        for row in rows:
            mt = _RT_TOPIC_RE.search(row)
            if not mt:
                continue
            topic_id = mt.group(1)
            name = _rt_strip_html(mt.group(2))
            ms = _RT_SEEDERS_RE.search(row)
            seeders = int(ms.group(1)) if ms else 0
            size = _rt_parse_size(row)
            if not name:
                continue
            parsed.append((topic_id, name, seeders, size))
        parsed.sort(key=lambda x: x[2], reverse=True)
        top = parsed[:RT_FETCH_CAP]
        if not top:
            return []
        out: list[dict] = []
        for tid, name, seeders, size in top:
            out.append({
                "name": name,
                "seeders": seeders,
                "size": size,
                # rd_link/link_type are placeholders — resolve.stream
                # for rutracker fetches the .torrent via topic_id.
                "rd_link": "",
                "link_type": "magnet",
                "source": "rutracker",
                "info_hash": "",
                "topic_id": tid,
            })
        print(f"[rutracker] q={q!r} returned={len(out)} (no viewtopic fetch)")
        _cache_put_indexer_query("rutracker", q, [dict(r) for r in out], ttl=_RT_QUERY_TTL)
        return out
    except Exception:
        return []


async def search_rutracker(
    bb_session: str, artist: str, title: str, album: str = ""
) -> list[dict]:
    """Sequential queries (rutracker rate-limits parallel ~20s/each).
    Tries the strongest query first and falls back if it 0-results:

      1. {artist} {album}     — full-album uploads are richest
      2. {artist} {title}     — single/track-named uploads
      3. {artist}              — broad fallback (catches discographies,
                                 self-titled albums, niche releases)

    Without the chain, an album the user requested that rutracker
    doesn't index by that exact name (e.g. '8 Mile (Music From And
    Inspired By The Motion Picture)' for 'Lose Yourself') returns
    nothing — even though rutracker has Eminem discographies that
    contain the track."""
    cookies = _rt_cookie_jar(bb_session)
    if not cookies:
        print("[rutracker] search aborted: bb_session cookie not provided")
        return []

    album_collapses = bool(album and artist) and _album_collapses_to_artist(artist, album)
    queries: list[tuple[str, str]] = []
    if album and artist and not album_collapses:
        queries.append((f"{artist} {album}", "album"))
    if artist and title:
        queries.append((f"{artist} {title}", "track"))
    elif title:
        queries.append((title, "track"))
    if artist:
        queries.append((artist, "artist_fallback"))

    seen: set[str] = set()
    out: list[dict] = []
    async with httpx.AsyncClient(
        timeout=25, follow_redirects=True, cookies=cookies,
        headers={"User-Agent": RUTRACKER_UA},
    ) as client:
        for q, qtype in queries:
            results = await _rutracker_query(client, q)
            for r in results:
                k = r.get("info_hash") or r["name"]
                if k in seen:
                    continue
                seen.add(k)
                r["query_type"] = qtype
                out.append(r)
            # Stop as soon as we have something — rutracker queries
            # are slow and the fallback chain only exists to recover
            # from 0-results, not to broaden a working query.
            if out:
                break
    return out
