"""Real-Debrid client + top-level helpers.

Reference implementation for the debrid client protocol. The
top-level helpers (`_check_rd_cache`, `_add_and_wait`,
`_unrestrict_audio`, `fetch_rd_downloaded`, `_resolve_prowlarr_link`)
predate the multi-debrid abstraction and are still called directly
in a few resolve.* paths in server.py — RDClient just adapts them
to the four-method protocol.
"""
from __future__ import annotations

import asyncio
import re

import httpx

import cache_db
from clients._shared import (
    AUDIO_EXTS,
    RD_BASE,
    _RD_DOWNLOADED_TTL,
    _normalize_torrent_name,
    _rd_headers,
)


async def _fetch_rd_downloaded_uncached(api_key: str) -> tuple[set[str], set[str]]:
    headers = {"Authorization": f"Bearer {api_key}"}
    hashes: set[str] = set()
    names: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            page = 1
            while True:
                r = await client.get(
                    f"{RD_BASE}/torrents",
                    params={"limit": 100, "page": page},
                    headers=headers,
                )
                if r.status_code != 200:
                    break
                items = r.json()
                if not items:
                    break
                for t in items:
                    if t.get("status") != "downloaded":
                        continue
                    h = (t.get("hash") or "").upper()
                    if h:
                        hashes.add(h)
                    nm = _normalize_torrent_name(t.get("filename") or "")
                    if nm:
                        names.add(nm)
                if len(items) < 100:
                    break
                page += 1
    except Exception:
        pass
    return hashes, names


async def fetch_rd_downloaded(api_key: str) -> tuple[set[str], set[str]]:
    """Page through every torrent on the user's RD account. Returns
    (hashes, normalized_names) for the ones that are downloaded.
    Cached per-key for _RD_DOWNLOADED_TTL seconds (SQLite-backed,
    survives uvicorn reload + process restart)."""
    cached = cache_db._cache_get_debrid_library("rd", api_key)
    if cached is not None:
        return cached
    hashes, names = await _fetch_rd_downloaded_uncached(api_key)
    cache_db._cache_put_debrid_library("rd", api_key, hashes, names, ttl=_RD_DOWNLOADED_TTL)
    return hashes, names


async def _resolve_prowlarr_link(proxy_url: str) -> tuple:
    """Prowlarr proxy URLs redirect to magnet:// or serve .torrent bytes.
    httpx can't follow magnet:// redirects so handle manually.
    Returns (value, "magnet" | "torrent_bytes")."""
    async with httpx.AsyncClient(timeout=15) as probe:
        r = await probe.get(proxy_url, follow_redirects=False)
        location = r.headers.get("location", "")

        if location.startswith("magnet:") or location.startswith("magnet://"):
            magnet = (
                location.replace("magnet://", "magnet:", 1)
                if location.startswith("magnet://")
                else location
            )
            return magnet, "magnet"

        if r.status_code in (301, 302, 303, 307, 308) and location:
            r2 = await probe.get(location, follow_redirects=True)
            return r2.content, "torrent_bytes"

        if r.status_code == 200:
            return r.content, "torrent_bytes"

        raise RuntimeError(f"Unexpected proxy response: {r.status_code}")


async def _check_rd_cache(
    client: httpx.AsyncClient, api_key: str, info_hash: str
) -> dict | None:
    """Return RD torrent info dict if user already has this hash downloaded."""
    if not info_hash:
        return None
    try:
        r = await client.get(
            f"{RD_BASE}/torrents",
            params={"limit": 100},
            headers=_rd_headers(api_key),
            timeout=8,
        )
        if r.status_code != 200:
            return None
        for t in r.json():
            if (
                t.get("hash", "").upper() == info_hash.upper()
                and t.get("status") == "downloaded"
            ):
                info_r = await client.get(
                    f"{RD_BASE}/torrents/info/{t['id']}",
                    headers=_rd_headers(api_key),
                    timeout=8,
                )
                if info_r.status_code == 200:
                    return info_r.json()
    except Exception as e:
        print(f"[RD] cache check error: {e}")
    return None


async def _add_and_wait(
    client: httpx.AsyncClient,
    api_key: str,
    rd_link: str,
    name: str,
    link_type: str = "magnet",
    info_hash: str = "",
    on_progress=None,
    torrent_bytes: bytes | None = None,
) -> dict | None:
    """Add magnet/torrent to RD, wait for manifest, select audio, poll until ready.

    Includes the bug fixes from earlier this week:
    - Waits for status to leave ``magnet_conversion`` before selectFiles
      (otherwise RD 404s with parameter_missing)
    - selectFiles retries on 404/503
    """
    if info_hash:
        cached = await _check_rd_cache(client, api_key, info_hash)
        if cached:
            return cached

    try:
        if torrent_bytes:
            r = await client.put(
                f"{RD_BASE}/torrents/addTorrent",
                content=torrent_bytes,
                headers={
                    **_rd_headers(api_key),
                    "Content-Type": "application/octet-stream",
                },
                timeout=20,
            )
        elif rd_link.startswith("magnet:"):
            r = await client.post(
                f"{RD_BASE}/torrents/addMagnet",
                data={"magnet": rd_link},
                headers=_rd_headers(api_key),
                timeout=15,
            )
        else:
            resolved, resolved_type = await _resolve_prowlarr_link(rd_link)
            if resolved_type == "magnet":
                r = await client.post(
                    f"{RD_BASE}/torrents/addMagnet",
                    data={"magnet": resolved},
                    headers=_rd_headers(api_key),
                    timeout=15,
                )
            else:
                r = await client.put(
                    f"{RD_BASE}/torrents/addTorrent",
                    content=resolved,
                    headers={
                        **_rd_headers(api_key),
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=20,
                )
        r.raise_for_status()
        torrent_id = r.json().get("id")
    except Exception as e:
        print(f"[RD] add failed for {name[:50]!r}: {type(e).__name__}: {e}")
        return None

    # Wait for RD to leave magnet_conversion before calling selectFiles.
    files: list[dict] = []
    info_status = ""
    for attempt in range(8):
        await asyncio.sleep(0.7 if attempt == 0 else 1.5)
        try:
            info_r = await client.get(
                f"{RD_BASE}/torrents/info/{torrent_id}",
                headers=_rd_headers(api_key),
                timeout=10,
            )
            info_r.raise_for_status()
            info = info_r.json()
            files = info.get("files", [])
            info_status = info.get("status", "")
            if files and info_status != "magnet_conversion":
                break
        except Exception as e:
            print(f"[RD] manifest error: {e}")

    if not files:
        print(f"[RD] manifest never populated (status={info_status!r}) — aborting")
        return None

    audio_ids = [
        str(f["id"]) for f in files
        if any(f.get("path", "").lower().endswith(ext) for ext in AUDIO_EXTS)
    ]
    select_val = ",".join(audio_ids) if audio_ids else "all"

    for sel_attempt in range(4):
        try:
            sel_r = await client.post(
                f"{RD_BASE}/torrents/selectFiles/{torrent_id}",
                data={"files": select_val},
                headers=_rd_headers(api_key),
                timeout=10,
            )
            if sel_r.status_code < 400:
                break
            if sel_r.status_code in (404, 503) and sel_attempt < 3:
                await asyncio.sleep(1.5)
                continue
            print(f"[RD] selectFiles failed status={sel_r.status_code} "
                  f"body={sel_r.text[:200]} files={select_val!r}")
            break
        except Exception as e:
            print(f"[RD] selectFiles error: {e}")
            break

    for attempt in range(40):
        await asyncio.sleep(1.5 if attempt < 10 else 3)
        try:
            info_r = await client.get(
                f"{RD_BASE}/torrents/info/{torrent_id}",
                headers=_rd_headers(api_key),
                timeout=10,
            )
            info_r.raise_for_status()
            info = info_r.json()
        except Exception as e:
            print(f"[RD] poll error: {e}")
            continue

        status = info.get("status")
        pct = info.get("progress", 0)
        if on_progress:
            try:
                await on_progress(min(int(pct * 0.9), 90), f"Downloading via RD · {pct}%")
            except Exception:
                pass

        if status == "downloaded" and info.get("links"):
            return info
        if status in ("error", "dead", "magnet_error", "virus"):
            return None
        if attempt >= 19 and pct == 0 and status == "downloading":
            return None

    return None


async def _unrestrict_audio(
    client: httpx.AsyncClient, api_key: str, links: list, title: str,
    artist: str = "",
) -> dict | None:
    """Unrestrict RD links concurrently, return best audio match for title.
    Same scoring as audimo_aio: full-title substring beats per-word, plus
    a tiny size tiebreaker. Returns None if nothing matches meaningfully
    so the caller can surface ``no_audio`` instead of a wrong file."""
    # Deferred import: server.py owns the title-phrase parsing and we
    # want clients/ to stay free of any reverse-import cycle at module
    # load time.
    from server import _title_phrase_variants

    async def try_unrestrict(link):
        try:
            r = await client.post(
                f"{RD_BASE}/unrestrict/link",
                data={"link": link},
                headers=_rd_headers(api_key),
                timeout=15,
            )
            if r.status_code != 200:
                return None
            d = r.json()
            fn = d.get("filename", "")
            ext = ("." + fn.rsplit(".", 1)[-1].lower()) if "." in fn else ""
            if ext in AUDIO_EXTS:
                return {
                    "filename": fn,
                    "filesize": d.get("filesize", 0),
                    "download": d.get("download"),
                    "mimeType": d.get("mimeType") or "audio/mpeg",
                    "rd_link": link,
                }
        except Exception as e:
            print(f"[RD] unrestrict error: {e}")
        return None

    results = await asyncio.gather(*[try_unrestrict(l) for l in links[:50]])
    audio_files = [x for x in results if x]
    if not audio_files:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())

    # Try multiple title variants — the full title and a paren-stripped
    # version. Spotify-style titles like "A Milli (Album Version
    # Explicit)" never appear verbatim in actual filenames, and the
    # stricter title_norm-substring check rejects every candidate.
    # Picking the highest-scoring file across all variants handles
    # both Beatles-style canonical-paren titles and metadata-suffix
    # titles uniformly.
    title_variants = _title_phrase_variants(title) or [_norm(title).strip()]
    artist_words = [w for w in _norm(artist).split() if len(w) >= 3]

    def score_against(f, title_norm):
        title_words = [w for w in title_norm.split() if len(w) >= 2]
        fn_norm = _norm(f["filename"])
        full = 2.0 if title_norm and title_norm in fn_norm else 0.0
        word = (
            sum(1 for w in title_words if w in fn_norm) / max(len(title_words), 1)
            if title_words else 0.0
        )
        art = (
            sum(1 for w in artist_words if w in fn_norm) / max(len(artist_words), 1) * 0.3
            if artist_words else 0.0
        )
        size_bonus = min(f["filesize"] / 50_000_000, 1.0) * 0.05
        return full + word + art + size_bonus

    def best_score(f):
        return max((score_against(f, v) for v in title_variants), default=0.0)

    audio_files.sort(key=best_score, reverse=True)
    best = audio_files[0]
    # Only enforce the score threshold when we actually have title
    # words to match against. The stripped-variant path uses the
    # cleanest title-words set, so a 0.5 threshold against THAT is
    # the right gate.
    primary_words = [w for w in (title_variants[-1] or "").split() if len(w) >= 2]
    if primary_words and best_score(best) < 0.5:
        return None
    return best


class RDClient:
    """Wraps the existing top-level RD functions so they conform to
    the debrid client protocol. Behaviour identical to pre-Phase-4."""

    name = "rd"
    label = "Real-Debrid"
    source_label_cached = "RD Cache"
    source_label_live = "RD"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch_downloaded(self) -> tuple[set[str], set[str]]:
        return await fetch_rd_downloaded(self.api_key)

    async def find_cached(self, client: httpx.AsyncClient, info_hash: str) -> dict | None:
        return await _check_rd_cache(client, self.api_key, info_hash)

    async def add_and_wait(
        self,
        client: httpx.AsyncClient,
        rd_link: str,
        name: str,
        link_type: str = "magnet",
        info_hash: str = "",
        on_progress=None,
        torrent_bytes: bytes | None = None,
    ) -> dict | None:
        return await _add_and_wait(
            client, self.api_key, rd_link, name, link_type, info_hash,
            on_progress, torrent_bytes=torrent_bytes,
        )

    async def unrestrict_audio(
        self, client: httpx.AsyncClient, links: list, title: str, artist: str = "",
    ) -> dict | None:
        return await _unrestrict_audio(client, self.api_key, links, title, artist)
