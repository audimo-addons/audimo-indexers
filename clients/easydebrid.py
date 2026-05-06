"""EasyDebrid client.

Tiny API surface (smaller than the others):
  * Bearer auth on every endpoint.
  * ``POST /v1/link/lookupdetails`` with ``{"urls": [magnet]}``
    bulk-checks cache + returns file lists.
  * ``POST /v1/link/generate`` with ``{"url": <hosted URL>}``
    produces the streamable URL.
  * ``GET /v1/user/details`` for auth verify.

EasyDebrid only handles cached torrents — there's no
"stage and wait" flow. Uncached torrents fall through to
libtorrent, same as the other backends' cache misses.
"""
from __future__ import annotations

import re

import httpx

from clients._shared import AUDIO_EXTS


class EasyDebridClient:
    """EasyDebrid backend.

    Tiny API surface (smaller than the others):
      * Bearer auth on every endpoint.
      * ``POST /v1/link/lookupdetails`` with ``{"urls": [magnet]}``
        bulk-checks cache + returns file lists.
      * ``POST /v1/link/generate`` with ``{"url": <hosted URL>}``
        produces the streamable URL.
      * ``GET /v1/user/details`` for auth verify.

    EasyDebrid only handles cached torrents — there's no
    "stage and wait" flow. Uncached torrents fall through to
    libtorrent, same as the other backends' cache misses.
    """

    name = "easydebrid"
    label = "EasyDebrid"
    source_label_cached = "EasyDebrid Cache"
    source_label_live = "EasyDebrid"

    BASE = "https://easydebrid.com/api"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def fetch_downloaded(self) -> tuple[set[str], set[str]]:
        """No "library" concept on EasyDebrid (every play is a one-shot
        link generation). Pre-flight cache flagging is skipped — the
        ⚡ badge won't appear for EasyDebrid users, but the live flow
        still hits cache via /lookupdetails per pick."""
        return set(), set()

    async def find_cached(
        self, client: httpx.AsyncClient, info_hash: str,
    ) -> dict | None:
        if not info_hash:
            return None
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
        try:
            r = await client.post(
                f"{self.BASE}/v1/link/lookupdetails",
                headers=self._headers(),
                json={"urls": [magnet]},
                timeout=10,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            results = (data.get("result") or [])
            if not results:
                return None
            top = results[0]
            if not top.get("cached"):
                return None
            files = top.get("files") or []
            if not files:
                return None
            # Each file dict: {name, size, url} — `url` is the hosted
            # link we feed to /link/generate at unrestrict time.
            return {
                "id": None,  # EasyDebrid is stateless
                "hash": info_hash.lower(),
                "name": top.get("name"),
                "links": [
                    {
                        "name": f.get("name") or "",
                        "size": f.get("size") or 0,
                        "url": f.get("url") or "",
                    }
                    for f in files
                ],
            }
        except Exception as e:
            print(f"[ed] lookupdetails error: {type(e).__name__}: {e}")
            return None

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
        """EasyDebrid has no "wait" flow — cached torrents resolve
        synchronously via /lookupdetails; uncached ones return None
        and the resolve.stream caller falls through to libtorrent."""
        cached = await self.find_cached(client, info_hash)
        return cached  # may be None

    async def unrestrict_audio(
        self, client: httpx.AsyncClient, links: list, title: str, artist: str = "",
    ) -> dict | None:
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

        # Generate the streamable URL for the chosen file.
        try:
            r = await client.post(
                f"{self.BASE}/v1/link/generate",
                headers=self._headers(),
                json={"url": best.get("url", "")},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[ed] generate http {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            stream_url = data.get("url")
            if not stream_url:
                return None
            return {
                "filename": data.get("filename") or best.get("name") or "",
                "filesize": data.get("size") or best.get("size") or 0,
                "download": stream_url,
                "mimeType": "audio/mpeg",
                "rd_link": stream_url,
            }
        except Exception as e:
            print(f"[ed] generate error: {type(e).__name__}: {e}")
            return None
