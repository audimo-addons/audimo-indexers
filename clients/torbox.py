"""TorBox client. Wire shape:
  * Bearer auth on every endpoint
  * Standard envelope: {success, error, detail, data}
  * /v1/api/torrents/checkcached?hash=H1,H2 — bulk cache flag
  * /v1/api/torrents/createtorrent (multipart) — submit magnet
  * /v1/api/torrents/mylist?id=N — poll/list user's torrents
  * /v1/api/torrents/requestdl?token=KEY&torrent_id=N&file_id=M
    — generate streamable URL (token in URL because <audio src=...>
    can't carry an Authorization header)
  * /v1/api/user/me — auth verify
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


class TorBoxClient:
    """TorBox backend. Wire shape:
      * Bearer auth on every endpoint
      * Standard envelope: {success, error, detail, data}
      * /v1/api/torrents/checkcached?hash=H1,H2 — bulk cache flag
      * /v1/api/torrents/createtorrent (multipart) — submit magnet
      * /v1/api/torrents/mylist?id=N — poll/list user's torrents
      * /v1/api/torrents/requestdl?token=KEY&torrent_id=N&file_id=M
        — generate streamable URL (token in URL because <audio src=...>
        can't carry an Authorization header)
      * /v1/api/user/me — auth verify

    Field-name idiosyncrasies (vs RD/AD):
      * torrent objects have ``id`` (we treat as torrent_id) + ``hash``
      * file objects have ``id``, ``name``, ``size``, ``mimetype``
      * download_finished: bool indicates a torrent is fully ready
      * `links` doesn't exist as URL strings — files are addressed by
        (torrent_id, file_id) and resolved at play time via /requestdl.
        Our `unrestrict_audio` therefore receives a list of
        {torrent_id, file_id, name, size} dicts (not URL strings).
    """

    name = "torbox"
    label = "TorBox"
    source_label_cached = "TorBox Cache"
    source_label_live = "TorBox"

    BASE = "https://api.torbox.app/v1/api"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _data(resp_json: dict):
        if not isinstance(resp_json, dict):
            return None
        if not resp_json.get("success"):
            err = resp_json.get("error") or resp_json.get("detail") or "unknown"
            print(f"[tb] error: {err}")
            return None
        return resp_json.get("data")

    async def fetch_downloaded(self) -> tuple[set[str], set[str]]:
        """Walk the user's mylist; return (hashes, normalized_names) for
        torrents whose download_finished flag is True.

        Cached per-key (SQLite-backed) for _RD_DOWNLOADED_TTL secs."""
        cached = _cache_get_debrid_library("torbox", self.api_key)
        if cached is not None:
            return cached

        hashes: set[str] = set()
        names: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self.BASE}/torrents/mylist",
                    headers=self._headers(),
                )
                if r.status_code != 200:
                    return set(), set()
                data = self._data(r.json())
                if not data:
                    return set(), set()
                # `data` is a list of torrent dicts.
                for t in data:
                    if not t.get("download_finished"):
                        continue
                    h = (t.get("hash") or "").upper()
                    if h:
                        hashes.add(h)
                    nm = _normalize_torrent_name(t.get("name") or "")
                    if nm:
                        names.add(nm)
        except Exception as e:
            print(f"[tb] fetch_downloaded error: {type(e).__name__}: {e}")
        _cache_put_debrid_library("torbox", self.api_key, hashes, names, ttl=_RD_DOWNLOADED_TTL)
        return hashes, names

    async def _find_in_mylist(
        self, client: httpx.AsyncClient, info_hash: str,
    ) -> dict | None:
        """Look up an info_hash in the user's mylist. Returns the
        torrent dict (with files) if present and finished. None otherwise."""
        try:
            r = await client.get(
                f"{self.BASE}/torrents/mylist",
                headers=self._headers(),
                timeout=10,
            )
            if r.status_code != 200:
                return None
            data = self._data(r.json())
            if not data:
                return None
            ih = info_hash.upper()
            for t in data:
                if (t.get("hash") or "").upper() == ih and t.get("download_finished"):
                    return t
        except Exception as e:
            print(f"[tb] mylist error: {e}")
        return None

    async def find_cached(
        self, client: httpx.AsyncClient, info_hash: str,
    ) -> dict | None:
        """If the user already has this hash downloaded, return a
        normalized links-bearing dict so the resolve.stream fast path
        can unrestrict it directly. None falls through to add_and_wait
        (which is also fast for TorBox-cached but not-yet-added hashes —
        /createtorrent returns instantly for those).
        """
        if not info_hash:
            return None
        t = await self._find_in_mylist(client, info_hash)
        if not t:
            return None
        return {
            "id": t.get("id"),
            "hash": (t.get("hash") or info_hash).lower(),
            "name": t.get("name"),
            "links": [
                {
                    "torrent_id": t.get("id"),
                    "file_id": f.get("id"),
                    "name": f.get("name") or f.get("short_name") or "",
                    "size": f.get("size") or 0,
                    "mimetype": f.get("mimetype") or "",
                }
                for f in (t.get("files") or [])
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
        """POST /createtorrent with the magnet, then poll /mylist?id=N
        until download_finished. TorBox-cached torrents finish almost
        immediately; uncached ones download server-side at
        TorBox-network speeds."""
        magnet = rd_link
        if not magnet or not magnet.startswith("magnet:"):
            if info_hash:
                magnet = f"magnet:?xt=urn:btih:{info_hash}"
            else:
                print(f"[tb] no usable magnet for {name[:50]!r}")
                return None
        try:
            # TorBox /createtorrent is multipart/form-data. httpx
            # generates the right Content-Type when ``files=`` is used
            # with text fields encoded as (None, value).
            r = await client.post(
                f"{self.BASE}/torrents/createtorrent",
                headers=self._headers(),
                files={"magnet": (None, magnet)},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"[tb] createtorrent http {r.status_code}: {r.text[:200]}")
                return None
            data = self._data(r.json())
            if not data:
                return None
            torrent_id = data.get("torrent_id") or data.get("id")
            if not torrent_id:
                print(f"[tb] createtorrent: no torrent_id in response")
                return None
        except Exception as e:
            print(f"[tb] createtorrent error: {type(e).__name__}: {e}")
            return None

        # Poll /mylist?id=N until download_finished. TorBox finishes
        # cached torrents within a few seconds; uncached ones are
        # subject to server-side speed (still typically 1-2 minutes
        # for a music album).
        for attempt in range(40):
            await asyncio.sleep(1.5 if attempt < 10 else 3)
            try:
                r = await client.get(
                    f"{self.BASE}/torrents/mylist",
                    headers=self._headers(),
                    params={"id": str(int(torrent_id))},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                payload = self._data(r.json())
                if payload is None:
                    continue
                # When `id` is set the API returns a single torrent
                # dict (not a list).
                t = payload if isinstance(payload, dict) else (payload[0] if payload else None)
                if not t:
                    continue
            except Exception as e:
                print(f"[tb] poll error: {e}")
                continue

            progress_pct = int((t.get("progress") or 0) * 100)
            state = (t.get("download_state") or "").lower()
            if on_progress:
                try:
                    await on_progress(
                        min(int(progress_pct * 0.9), 90),
                        f"Downloading via TorBox · {progress_pct}%",
                    )
                except Exception:
                    pass

            if t.get("download_finished"):
                return {
                    "id": t.get("id"),
                    "hash": (t.get("hash") or info_hash).lower(),
                    "name": t.get("name"),
                    "links": [
                        {
                            "torrent_id": t.get("id"),
                            "file_id": f.get("id"),
                            "name": f.get("name") or f.get("short_name") or "",
                            "size": f.get("size") or 0,
                            "mimetype": f.get("mimetype") or "",
                        }
                        for f in (t.get("files") or [])
                    ],
                }
            if state in ("error", "stalled (no_seeds)", "stalled"):
                return None

        return None

    async def unrestrict_audio(
        self, client: httpx.AsyncClient, links: list, title: str, artist: str = "",
    ) -> dict | None:
        """links here are {torrent_id, file_id, name, size, mimetype}
        dicts (NOT URL strings — TorBox addresses files by id, not URL).
        Pick the best audio match by name, then call /requestdl to get
        the actual streamable URL.

        Note: /requestdl takes the api_key as a query param (`token`)
        instead of a header — the resulting URL is meant to be hit
        directly by an <audio> element which can't set headers.
        """
        # Filter to audio extensions first; some torrents bundle .nfo,
        # .jpg, .cue, etc. that we shouldn't even consider.
        candidates = [
            f for f in links
            if any((f.get("name") or "").lower().endswith(ext) for ext in AUDIO_EXTS)
        ]
        if not candidates:
            return None

        # Reuse RD/AD-style scoring so multi-track album torrents pick
        # the right song. Operates on the file's `name` field.
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

        # Resolve the chosen file's streamable URL via /requestdl.
        try:
            r = await client.get(
                f"{self.BASE}/torrents/requestdl",
                headers=self._headers(),
                params={
                    "token": self.api_key,
                    "torrent_id": str(int(best["torrent_id"])),
                    "file_id": str(int(best["file_id"])),
                    "redirect": "false",
                },
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[tb] requestdl http {r.status_code}: {r.text[:200]}")
                return None
            payload = self._data(r.json())
            if not payload:
                return None
            # On success, `data` is the URL string itself. Some legacy
            # responses return {"data": {"url": "..."}} — handle both.
            stream_url = (
                payload if isinstance(payload, str)
                else (payload.get("url") or payload.get("link"))
                if isinstance(payload, dict) else None
            )
            if not stream_url:
                return None
            return {
                "filename": best.get("name") or "",
                "filesize": best.get("size") or 0,
                "download": stream_url,
                "mimeType": best.get("mimetype") or "audio/mpeg",
                "rd_link": stream_url,  # TorBox doesn't have a
                                        # separate "raw" link — reuse.
            }
        except Exception as e:
            print(f"[tb] requestdl error: {type(e).__name__}: {e}")
            return None
