"""Debrid-Link.fr client.

Wire shape:
  * Bearer auth on every endpoint.
  * Base URL: https://debrid-link.fr/api/v2/
  * ``POST /seedbox/add`` form {url, wait, async} — submit magnet.
    With ``wait=true``, blocks until cached check + initial
    download happens server-side.
  * ``GET /seedbox/list`` query {ids, page, perPage} — list user's
    seedboxes (entries with files arrays).
  * ``POST /downloader/add`` form {url} — unrestrict a hosted URL
    (used for files-from-finished-seedbox if needed).
  * ``GET /account/infos`` for auth verify.

Response envelope: ``{success: bool, value: ..., error: ...}``.
"""
from __future__ import annotations

import asyncio
import re

import httpx

from cache_db import _cache_get_debrid_library, _cache_put_debrid_library
from clients._shared import (
    AUDIO_EXTS,
    _RD_DOWNLOADED_TTL,
    _normalize_torrent_name,
)


class DebridLinkClient:
    """Debrid-Link.fr backend.

    Wire shape:
      * Bearer auth on every endpoint.
      * Base URL: https://debrid-link.fr/api/v2/
      * ``POST /seedbox/add`` form {url, wait, async} — submit magnet.
        With ``wait=true``, blocks until cached check + initial
        download happens server-side.
      * ``GET /seedbox/list`` query {ids, page, perPage} — list user's
        seedboxes (entries with files arrays).
      * ``POST /downloader/add`` form {url} — unrestrict a hosted URL
        (used for files-from-finished-seedbox if needed).
      * ``GET /account/infos`` for auth verify.

    Response envelope: ``{success: bool, value: ..., error: ...}``.
    """

    name = "debridlink"
    label = "Debrid-Link"
    source_label_cached = "Debrid-Link Cache"
    source_label_live = "Debrid-Link"

    BASE = "https://debrid-link.fr/api/v2"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _value(resp_json):
        if not isinstance(resp_json, dict):
            return None
        if not resp_json.get("success"):
            err = resp_json.get("error") or "unknown"
            print(f"[dl] error: {err}")
            return None
        return resp_json.get("value")

    async def fetch_downloaded(self) -> tuple[set[str], set[str]]:
        cached = _cache_get_debrid_library("debridlink", self.api_key)
        if cached is not None:
            return cached
        hashes: set[str] = set()
        names: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self.BASE}/seedbox/list",
                    headers=self._headers(),
                )
                if r.status_code != 200:
                    return set(), set()
                value = self._value(r.json())
                if not value:
                    return set(), set()
                for sb in value:
                    # status code 4 == "ready" / fully cached on DL.
                    if sb.get("status") not in (4, "ready"):
                        if not sb.get("files"):
                            continue
                    h = (sb.get("hashString") or "").upper()
                    if h:
                        hashes.add(h)
                    nm = _normalize_torrent_name(sb.get("name") or "")
                    if nm:
                        names.add(nm)
        except Exception as e:
            print(f"[dl] fetch_downloaded error: {type(e).__name__}: {e}")
        _cache_put_debrid_library("debridlink", self.api_key, hashes, names, ttl=_RD_DOWNLOADED_TTL)
        return hashes, names

    async def _find_in_seedbox_list(
        self, client: httpx.AsyncClient, info_hash: str,
    ) -> dict | None:
        try:
            r = await client.get(
                f"{self.BASE}/seedbox/list",
                headers=self._headers(),
                timeout=10,
            )
            if r.status_code != 200:
                return None
            value = self._value(r.json())
            if not value:
                return None
            ih = info_hash.upper()
            for sb in value:
                if (sb.get("hashString") or "").upper() == ih and sb.get("files"):
                    return sb
        except Exception as e:
            print(f"[dl] seedbox/list error: {e}")
        return None

    async def find_cached(
        self, client: httpx.AsyncClient, info_hash: str,
    ) -> dict | None:
        if not info_hash:
            return None
        sb = await self._find_in_seedbox_list(client, info_hash)
        if not sb:
            return None
        return {
            "id": sb.get("id"),
            "hash": (sb.get("hashString") or info_hash).lower(),
            "name": sb.get("name"),
            "links": [
                {
                    "name": f.get("name") or "",
                    "size": f.get("size") or 0,
                    "downloadUrl": f.get("downloadUrl") or "",
                }
                for f in (sb.get("files") or [])
            ],
        }

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
        magnet = rd_link
        if not magnet or not magnet.startswith("magnet:"):
            if info_hash:
                magnet = f"magnet:?xt=urn:btih:{info_hash}"
            else:
                return None
        try:
            r = await client.post(
                f"{self.BASE}/seedbox/add",
                headers=self._headers(),
                data={"url": magnet, "wait": "true", "async": "false"},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"[dl] seedbox/add http {r.status_code}: {r.text[:200]}")
                return None
            value = self._value(r.json())
            if not value:
                return None
            seedbox_id = value.get("id")
            if not seedbox_id:
                return None
            # When wait=true the response often already has files;
            # check before polling.
            if value.get("files"):
                return {
                    "id": seedbox_id,
                    "hash": (value.get("hashString") or info_hash).lower(),
                    "name": value.get("name"),
                    "links": [
                        {
                            "name": f.get("name") or "",
                            "size": f.get("size") or 0,
                            "downloadUrl": f.get("downloadUrl") or "",
                        }
                        for f in (value.get("files") or [])
                    ],
                }
        except Exception as e:
            print(f"[dl] seedbox/add error: {type(e).__name__}: {e}")
            return None

        # Poll seedbox/list?ids=N until status==4 (done).
        for attempt in range(40):
            await asyncio.sleep(1.5 if attempt < 10 else 3)
            try:
                r = await client.get(
                    f"{self.BASE}/seedbox/list",
                    headers=self._headers(),
                    params={"ids": str(seedbox_id)},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                value = self._value(r.json())
                if not value:
                    continue
                sb = value[0] if value else None
                if not sb:
                    continue
            except Exception as e:
                print(f"[dl] poll error: {e}")
                continue

            if on_progress:
                downloaded = sb.get("downloadPercent") or 0
                try:
                    await on_progress(min(int(downloaded * 0.9), 90),
                                      f"Downloading via Debrid-Link · {int(downloaded)}%")
                except Exception:
                    pass

            if sb.get("status") in (4, "ready") and sb.get("files"):
                return {
                    "id": sb.get("id"),
                    "hash": (sb.get("hashString") or info_hash).lower(),
                    "name": sb.get("name"),
                    "links": [
                        {
                            "name": f.get("name") or "",
                            "size": f.get("size") or 0,
                            "downloadUrl": f.get("downloadUrl") or "",
                        }
                        for f in (sb.get("files") or [])
                    ],
                }
            if sb.get("status") in (-1, "error"):
                return None

        return None

    async def unrestrict_audio(
        self, client: httpx.AsyncClient, links: list, title: str, artist: str = "",
    ) -> dict | None:
        # files from /seedbox/* already carry a downloadUrl that's
        # directly playable — no /downloader/add unrestrict needed
        # for the common case.
        candidates = [
            f for f in links
            if any((f.get("name") or "").lower().endswith(ext) for ext in AUDIO_EXTS)
        ]
        if not candidates:
            return None

        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
        title_norm = _norm(title).strip()
        title_words = [w for w in title_norm.split() if len(w) >= 2]
        artist_words = [w for w in _norm(artist).split() if len(w) >= 3]

        def score(f):
            fn_norm = _norm(f.get("name", ""))
            full = 2.0 if title_norm and title_norm in fn_norm else 0.0
            word = (
                sum(1 for w in title_words if w in fn_norm) / max(len(title_words), 1)
                if title_words else 0.0
            )
            art = (
                sum(1 for w in artist_words if w in fn_norm) / max(len(artist_words), 1) * 0.3
                if artist_words else 0.0
            )
            size_bonus = min((f.get("size") or 0) / 50_000_000, 1.0) * 0.05
            return full + word + art + size_bonus

        candidates.sort(key=score, reverse=True)
        best = candidates[0]
        if title_words and score(best) < 0.5:
            return None

        stream_url = best.get("downloadUrl") or ""
        if not stream_url:
            return None
        return {
            "filename": best.get("name") or "",
            "filesize": best.get("size") or 0,
            "download": stream_url,
            "mimeType": "audio/mpeg",
            "rd_link": stream_url,
        }
