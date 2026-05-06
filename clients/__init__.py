"""Debrid client implementations.

Each client conforms to the four-method protocol:
  fetch_downloaded()   → (set[hash], set[normalized_name])
  find_cached(client, info_hash) → torrent-info dict | None
  add_and_wait(client, link, name, link_type, info_hash, on_progress, torrent_bytes)
                       → torrent-info dict | None
  unrestrict_audio(client, links, title, artist) → audio dict | None

Public API (importable from server.py):
  from clients import (
      RDClient, AllDebridClient, TorBoxClient,
      PremiumizeClient, EasyDebridClient, DebridLinkClient,
      _active_debrid,
      _normalize_torrent_name, _extract_btih,
  )
"""
from __future__ import annotations

from clients._shared import (
    AUDIO_EXTS,
    RD_BASE,
    _RD_DOWNLOADED_TTL,
    _extract_btih,
    _normalize_torrent_name,
    _rd_headers,
)
from clients.alldebrid import AllDebridClient
from clients.debridlink import DebridLinkClient
from clients.easydebrid import EasyDebridClient
from clients.premiumize import PremiumizeClient
from clients.rd import (
    RDClient,
    _add_and_wait,
    _check_rd_cache,
    _unrestrict_audio,
    fetch_rd_downloaded,
)
from clients.torbox import TorBoxClient


def _active_debrid(cfg: dict):
    """Return the active debrid client (or None if no key is configured).

    Priority: RD → AllDebrid → TorBox → Premiumize → Debrid-Link →
    EasyDebrid. To force a specific debrid, set only that key.
    """
    rd_key = (cfg.get("rd_api_key") or "").strip()
    if rd_key:
        return RDClient(rd_key)
    ad_key = (cfg.get("alldebrid_api_key") or "").strip()
    if ad_key:
        return AllDebridClient(ad_key)
    tb_key = (cfg.get("torbox_api_key") or "").strip()
    if tb_key:
        return TorBoxClient(tb_key)
    pm_key = (cfg.get("premiumize_api_key") or "").strip()
    if pm_key:
        return PremiumizeClient(pm_key)
    dl_key = (cfg.get("debridlink_api_key") or "").strip()
    if dl_key:
        return DebridLinkClient(dl_key)
    ed_key = (cfg.get("easydebrid_api_key") or "").strip()
    if ed_key:
        return EasyDebridClient(ed_key)
    return None


__all__ = [
    "RDClient",
    "AllDebridClient",
    "TorBoxClient",
    "PremiumizeClient",
    "EasyDebridClient",
    "DebridLinkClient",
    "_active_debrid",
    "AUDIO_EXTS",
    "RD_BASE",
    "_RD_DOWNLOADED_TTL",
    "_rd_headers",
    "_normalize_torrent_name",
    "_extract_btih",
    "_add_and_wait",
    "_check_rd_cache",
    "_unrestrict_audio",
    "fetch_rd_downloaded",
]
