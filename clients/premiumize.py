"""Premiumize.me client.

Wire shape differs noticeably from RD/AD/TB:
  * Auth is via ``?apikey=...`` query param on EVERY request (not
    a Bearer header). Premiumize's API predates the bearer-header
    convention.
  * Response envelope: {status: "success"|"error", message?, ...}.
  * Cached torrents are added almost instantly via /transfer/create
    — no separate "fast path" needed; we always go through the
    transfer create + poll flow, and cached items finish on the
    first poll.
  * Files are returned with a `stream_link` URL that's directly
    playable from a browser — no /unrestrict step.
"""
from __future__ import annotations

import asyncio
import re

import httpx

from clients._shared import AUDIO_EXTS


class PremiumizeClient:
    """Premiumize.me backend.

    Wire shape differs noticeably from RD/AD/TB:
      * Auth is via ``?apikey=...`` query param on EVERY request (not
        a Bearer header). Premiumize's API predates the bearer-header
        convention.
      * Response envelope: {status: "success"|"error", message?, ...}.
      * Cached torrents are added almost instantly via /transfer/create
        — no separate "fast path" needed; we always go through the
        transfer create + poll flow, and cached items finish on the
        first poll.
      * Files are returned with a `stream_link` URL that's directly
        playable from a browser — no /unrestrict step.
      * No ``find_cached`` fast path: a cache hit doesn't yield file
        URLs without first staging the transfer, and staging is
        instant for cached items anyway. So we let `add_and_wait`
        handle both cached and uncached uniformly.
    """

    name = "premiumize"
    label = "Premiumize"
    source_label_cached = "Premiumize Cache"
    source_label_live = "Premiumize"

    BASE = "https://www.premiumize.me/api"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _params(self, **extra):
        # apikey rides on every request as a query param.
        return {"apikey": self.api_key, **extra}

    @staticmethod
    def _ok(resp_json) -> bool:
        return isinstance(resp_json, dict) and resp_json.get("status") == "success"

    async def fetch_downloaded(self) -> tuple[set[str], set[str]]:
        """Premiumize doesn't expose a "user's downloaded torrents by
        hash" endpoint cleanly — folder-walking would be many API
        calls per search and is rate-limit risky. Skip the
        /resolve/sources cache flagging for Premiumize and let
        /resolve/stream's add_and_wait stage anything cached at
        play time (still ~instant for cached, just no pre-flight
        ⚡ badge).

        Future: walk /api/folder/list at root + recurse, parsing
        torrent file names. Cache aggressively (10min+ TTL via
        SQLite cache) since the result rarely changes."""
        return set(), set()

    async def find_cached(
        self, client: httpx.AsyncClient, info_hash: str,
    ) -> dict | None:
        """No fast path — see class docstring. add_and_wait handles
        cache hits identically to misses, just faster on hits."""
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
        """Stage the magnet via /transfer/create, poll /transfer/list
        until our transfer's status is "finished", then list its
        folder to get the per-file stream URLs."""
        magnet = rd_link
        if not magnet or not magnet.startswith("magnet:"):
            if info_hash:
                magnet = f"magnet:?xt=urn:btih:{info_hash}"
            else:
                print(f"[pm] no usable magnet for {name[:50]!r}")
                return None
        try:
            r = await client.post(
                f"{self.BASE}/transfer/create",
                params=self._params(),
                data={"src": magnet},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[pm] transfer/create http {r.status_code}: {r.text[:200]}")
                return None
            resp = r.json()
            if not self._ok(resp):
                print(f"[pm] transfer/create error: {resp.get('message')!r}")
                return None
            transfer_id = resp.get("id")
            if not transfer_id:
                print("[pm] transfer/create: no id in response")
                return None
        except Exception as e:
            print(f"[pm] transfer/create error: {type(e).__name__}: {e}")
            return None

        # Poll /transfer/list. Premiumize doesn't accept ?id= filtering
        # — we always get the full list and find ours.
        for attempt in range(40):
            await asyncio.sleep(1.5 if attempt < 10 else 3)
            try:
                r = await client.get(
                    f"{self.BASE}/transfer/list",
                    params=self._params(),
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                resp = r.json()
                if not self._ok(resp):
                    continue
                t = next(
                    (x for x in (resp.get("transfers") or []) if x.get("id") == transfer_id),
                    None,
                )
                if not t:
                    # Transfer might have already moved into the user's
                    # cloud and been removed from /transfer/list. Treat
                    # as "finished, but we lost the folder ref" — bail.
                    print("[pm] transfer disappeared from list before finish")
                    return None
            except Exception as e:
                print(f"[pm] poll error: {e}")
                continue

            status = (t.get("status") or "").lower()
            progress = t.get("progress")
            if isinstance(progress, (int, float)):
                pct = int(progress * 100)
            else:
                pct = 0
            if on_progress:
                try:
                    await on_progress(min(int(pct * 0.9), 90),
                                      f"Downloading via Premiumize · {pct}%")
                except Exception:
                    pass

            if status in ("finished", "seeding", "success"):
                folder_id = t.get("folder_id")
                if not folder_id:
                    print("[pm] transfer finished without folder_id")
                    return None
                files = await self._list_folder_files(client, folder_id)
                return {
                    "id": transfer_id,
                    "hash": (info_hash or "").lower(),
                    "name": t.get("name") or name,
                    "links": files,
                }
            if status in ("error", "deleted", "banned", "timeout"):
                msg = t.get("message") or status
                print(f"[pm] transfer failed: {msg!r}")
                return None

        return None

    async def _list_folder_files(
        self, client: httpx.AsyncClient, folder_id: str,
    ) -> list[dict]:
        """Walk a finished-transfer's folder, returning a flat list of
        audio file dicts {name, size, stream_link, link} suitable for
        unrestrict_audio scoring. Recurses into sub-folders one level
        deep (album folders sometimes contain a CD1/CD2 split)."""
        out: list[dict] = []

        async def walk(fid: str, depth: int) -> None:
            try:
                r = await client.get(
                    f"{self.BASE}/folder/list",
                    params=self._params(id=fid),
                    timeout=10,
                )
                if r.status_code != 200:
                    return
                resp = r.json()
                if not self._ok(resp):
                    return
                for entry in (resp.get("content") or []):
                    if entry.get("type") == "folder":
                        if depth < 2:  # cap recursion
                            await walk(entry.get("id"), depth + 1)
                        continue
                    name = entry.get("name") or ""
                    if not any(name.lower().endswith(ext) for ext in AUDIO_EXTS):
                        continue
                    out.append({
                        "name": name,
                        "size": entry.get("size") or 0,
                        # Prefer stream_link (range-friendly progressive
                        # delivery); fall back to plain link.
                        "stream_link": entry.get("stream_link") or entry.get("link") or "",
                        "link": entry.get("link") or entry.get("stream_link") or "",
                        "mimetype": entry.get("mime_type") or "",
                    })
            except Exception as e:
                print(f"[pm] folder/list error: {e}")

        await walk(folder_id, 0)
        return out

    async def unrestrict_audio(
        self, client: httpx.AsyncClient, links: list, title: str, artist: str = "",
    ) -> dict | None:
        """links here are {name, size, stream_link, link, mimetype}
        dicts. Premiumize files are already streamable URLs — no
        unrestrict step. Pick the best audio match by name and
        return its stream_link as the download URL."""
        if not links:
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

        sorted_links = sorted(links, key=score, reverse=True)
        best = sorted_links[0]
        if title_words and score(best) < 0.5:
            return None

        stream_url = best.get("stream_link") or best.get("link")
        if not stream_url:
            return None
        return {
            "filename": best.get("name") or "",
            "filesize": best.get("size") or 0,
            "download": stream_url,
            "mimeType": best.get("mimetype") or "audio/mpeg",
            "rd_link": stream_url,
        }
