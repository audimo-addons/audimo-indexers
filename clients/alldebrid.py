"""AllDebrid client. Wire shape:
  * Bearer auth on every endpoint
  * /v4 for most endpoints, /v4.1 for magnet/status (newer POST API)
  * Standard envelope: {status: success/error, data}
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


class AllDebridClient:
    """AllDebrid backend. API is similar to RD in concept but
    different in shape — responses are wrapped in
    {"status":"success","data":{...}}, magnets carry a numeric `id`,
    and statuses use string codes ("Ready", "Downloading", …).

    Uses /v4 for most endpoints and /v4.1 for magnet/status (per the
    docs: "Enabled POST requests and changed the default API usage").
    """

    name = "alldebrid"
    label = "AllDebrid"
    source_label_cached = "AllDebrid Cache"
    source_label_live = "AllDebrid"

    BASE_V4 = "https://api.alldebrid.com/v4"
    BASE_V41 = "https://api.alldebrid.com/v4.1"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _data(resp_json: dict) -> dict | None:
        """Unwrap AllDebrid's success/error envelope. Returns the
        inner data dict on success, None on error."""
        if not isinstance(resp_json, dict):
            return None
        if resp_json.get("status") != "success":
            err = (resp_json.get("error") or {})
            print(f"[ad] error code={err.get('code')!r} message={err.get('message')!r}")
            return None
        return resp_json.get("data") or None

    async def fetch_downloaded(self) -> tuple[set[str], set[str]]:
        """Pull the user's full magnet history; return (hashes,
        normalized_names) for entries whose status is Ready (status
        string == "Ready" or statusCode == 4).

        Cached per-key (SQLite-backed) for _RD_DOWNLOADED_TTL secs."""
        cached = _cache_get_debrid_library("alldebrid", self.api_key)
        if cached is not None:
            return cached

        hashes: set[str] = set()
        names: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # POST /v4.1/magnet/status with no filter = all magnets.
                r = await client.post(
                    f"{self.BASE_V41}/magnet/status",
                    headers=self._headers(),
                )
                if r.status_code != 200:
                    return set(), set()
                data = self._data(r.json())
                if not data:
                    return set(), set()
                for m in (data.get("magnets") or []):
                    status = (m.get("status") or "").lower()
                    if status != "ready":
                        continue
                    h = (m.get("hash") or "").upper()
                    if h:
                        hashes.add(h)
                    nm = _normalize_torrent_name(m.get("filename") or "")
                    if nm:
                        names.add(nm)
        except Exception as e:
            print(f"[ad] fetch_downloaded error: {type(e).__name__}: {e}")
        _cache_put_debrid_library("alldebrid", self.api_key, hashes, names, ttl=_RD_DOWNLOADED_TTL)
        return hashes, names

    async def find_cached(
        self, client: httpx.AsyncClient, info_hash: str,
    ) -> dict | None:
        """Return a {'links': [...], 'id': N, 'hash': ...} dict if
        the user has this hash already cached. Otherwise None."""
        if not info_hash:
            return None
        try:
            r = await client.post(
                f"{self.BASE_V41}/magnet/status",
                headers=self._headers(),
                timeout=8,
            )
            if r.status_code != 200:
                return None
            data = self._data(r.json())
            if not data:
                return None
            for m in (data.get("magnets") or []):
                if (m.get("hash") or "").upper() != info_hash.upper():
                    continue
                if (m.get("status") or "").lower() != "ready":
                    continue
                # /magnet/files for the actual link list (status
                # response doesn't include full per-file links).
                links = await self._magnet_files(client, m.get("id"))
                if not links:
                    continue
                return {
                    "id": m.get("id"),
                    "hash": (m.get("hash") or info_hash).lower(),
                    "links": links,
                    "filename": m.get("filename"),
                }
        except Exception as e:
            print(f"[ad] find_cached error: {e}")
        return None

    async def _magnet_files(
        self, client: httpx.AsyncClient, magnet_id: int | str,
    ) -> list[str]:
        """Get the list of file hosting links for a ready magnet."""
        if not magnet_id:
            return []
        try:
            r = await client.post(
                f"{self.BASE_V4}/magnet/files",
                headers=self._headers(),
                data={"id[]": str(magnet_id)},
                timeout=10,
            )
            if r.status_code != 200:
                return []
            data = self._data(r.json())
            if not data:
                return []
            out: list[str] = []
            # Response shape: { magnets: [{ id, files: [{n, s, l}, …] }] }
            for entry in (data.get("magnets") or []):
                for f in (entry.get("files") or []):
                    link = f.get("l") or f.get("link")
                    if link:
                        out.append(link)
            return out
        except Exception as e:
            print(f"[ad] magnet/files error: {e}")
            return []

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
        """Submit magnet, poll until ready, return links-bearing dict.

        AllDebrid's /magnet/upload returns ``ready: true`` immediately
        for already-cached magnets — no polling needed. Otherwise we
        poll /magnet/status until status==Ready.
        """
        magnet_value = rd_link
        if not magnet_value or not magnet_value.startswith("magnet:"):
            # AllDebrid's /magnet/upload accepts the bare hash too
            # when no full magnet URI is available.
            if info_hash:
                magnet_value = info_hash
            else:
                print(f"[ad] no usable magnet for {name[:50]!r}")
                return None
        try:
            r = await client.post(
                f"{self.BASE_V4}/magnet/upload",
                headers=self._headers(),
                data={"magnets[]": magnet_value},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[ad] upload http {r.status_code}: {r.text[:200]}")
                return None
            data = self._data(r.json())
            if not data:
                return None
            magnets = data.get("magnets") or []
            if not magnets:
                return None
            mg = magnets[0]
            if mg.get("error"):
                print(f"[ad] magnet rejected: {mg['error']}")
                return None
            magnet_id = mg.get("id")
        except Exception as e:
            print(f"[ad] upload error: {type(e).__name__}: {e}")
            return None

        # Fast path: ready immediately (cached).
        # Otherwise poll /magnet/status until Ready or terminal-error.
        for attempt in range(40):
            await asyncio.sleep(1.5 if attempt < 10 else 3)
            try:
                r = await client.post(
                    f"{self.BASE_V41}/magnet/status",
                    headers=self._headers(),
                    data={"id": str(magnet_id)},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                data = self._data(r.json())
                if not data:
                    continue
                magnets = data.get("magnets") or []
                m = magnets[0] if magnets else None
                if not m:
                    continue
            except Exception as e:
                print(f"[ad] poll error: {e}")
                continue

            status = (m.get("status") or "").lower()
            downloaded = m.get("downloaded") or 0
            size = m.get("size") or 0
            pct = int(downloaded / size * 100) if size > 0 else 0
            if on_progress:
                try:
                    await on_progress(min(int(pct * 0.9), 90), f"Downloading via AllDebrid · {pct}%")
                except Exception:
                    pass

            if status == "ready":
                links = await self._magnet_files(client, magnet_id)
                if not links:
                    return None
                return {
                    "id": magnet_id,
                    "hash": (m.get("hash") or info_hash).lower(),
                    "links": links,
                    "filename": m.get("filename"),
                }
            if status in ("error", "expired"):
                return None

        return None

    async def unrestrict_audio(
        self, client: httpx.AsyncClient, links: list, title: str, artist: str = "",
    ) -> dict | None:
        """Unrestrict each link via /link/unlock, then pick the best
        audio file using the same scoring as the RD path."""
        async def try_unlock(link: str) -> dict | None:
            try:
                r = await client.post(
                    f"{self.BASE_V4}/link/unlock",
                    headers=self._headers(),
                    data={"link": link},
                    timeout=15,
                )
                if r.status_code != 200:
                    return None
                data = self._data(r.json())
                if not data:
                    return None
                stream_url = data.get("link")
                if not stream_url:
                    return None
                fn = data.get("filename") or ""
                ext = ("." + fn.rsplit(".", 1)[-1].lower()) if "." in fn else ""
                if ext not in AUDIO_EXTS:
                    return None
                return {
                    "filename": fn,
                    "filesize": data.get("filesize", 0),
                    "download": stream_url,
                    "mimeType": "audio/mpeg",  # AD doesn't return mime
                    "rd_link": link,
                }
            except Exception as e:
                print(f"[ad] unlock error: {e}")
                return None

        results = await asyncio.gather(*[try_unlock(l) for l in links[:50]])
        audio_files = [x for x in results if x]
        if not audio_files:
            return None

        # Reuse the same scoring as _unrestrict_audio so multi-track
        # albums pick the right song.
        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
        title_norm = _norm(title).strip()
        title_words = [w for w in title_norm.split() if len(w) >= 2]
        artist_words = [w for w in _norm(artist).split() if len(w) >= 3]

        def score(f):
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

        audio_files.sort(key=score, reverse=True)
        best = audio_files[0]
        if title_words and score(best) < 0.5:
            return None
        return best
