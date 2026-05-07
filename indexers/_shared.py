"""Helpers shared across indexer modules.

These were previously top-level functions in server.py; pulling them
into a sibling module lets each indexer module import only what it
needs without back-importing server.py (which would create a cycle —
server.py imports the indexers package).
"""
from __future__ import annotations

import os
import re
import urllib.parse


# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────

VIDEO_EXTS = (
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv",
)

VIDEO_KEYWORDS = (
    "1080p", "720p", "2160p", "4k", "x264", "x265", "h264", "h265",
    "hevc", "bluray", "blu-ray", "webrip", "web-dl", "hdtv", "dvdrip",
)

# Tracker pool used when synthesising magnet URIs from a bare info_hash.
# Same set audimo_aio ships with — keeps the behavior identical when the
# aggregator falls back from one to the other.
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://9.rarbg.com:2810/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://retracker.lanta-net.ru:2710/announce",
    "udp://tracker.dler.com:6969/announce",
    "udp://www.torrent.eu.org:451/announce",
    "https://tracker.gbitt.info/announce",
)


# ──────────────────────────────────────────────────────────────────
# Title-phrase normalisation (used by relevance + verification)
# ──────────────────────────────────────────────────────────────────


def _normalize_title_phrase(s: str) -> str:
    """Lowercase + collapse non-alphanumerics to single spaces.
    Used both for phrase-match scoring and the post-pick sanity
    check that rejects torrents lacking the requested track."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def _title_phrase_variants(track_title: str) -> list[str]:
    """Return search-phrase variants for the track title, ordered
    most→least specific.

    Spotify and iTunes attach metadata in parentheses that doesn't
    appear in actual filenames: "A Milli (Album Version Explicit)",
    "Mr. Brightside (Remastered 2011)", "Stay (feat. Justin Bieber)".
    Strict phrase-matching against the full title rejects every
    legitimate file. So we try the full title first (handles cases
    where the paren is canonical, e.g. Beatles "I Want You (She's
    So Heavy)") and fall back to the bare title with parentheticals
    stripped.
    """
    variants: list[str] = []
    full = _normalize_title_phrase(track_title)
    if full:
        variants.append(full)
    # Drop everything in (...) or [...] then re-normalise.
    stripped = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", track_title or "")
    bare = _normalize_title_phrase(stripped)
    if bare and bare != full:
        variants.append(bare)
    return variants


# ──────────────────────────────────────────────────────────────────
# Indexer helpers
# ──────────────────────────────────────────────────────────────────


def is_video(name: str) -> bool:
    n = (name or "").lower()
    if any(n.endswith(ext) for ext in VIDEO_EXTS):
        return True
    return any(kw in n for kw in VIDEO_KEYWORDS)


def make_magnet(info_hash: str, name: str) -> str:
    trackers = "".join(f"&tr={urllib.parse.quote(t)}" for t in TRACKERS)
    return (
        f"magnet:?xt=urn:btih:{info_hash}"
        f"&dn={urllib.parse.quote(name)}{trackers}"
    )


def _seed_bucket(seeders) -> int:
    """Bucket seeder count for ranking. Without bucketing, the sort
    treats 1 seeder and 50 seeders as adjacent values that quality
    can override — so a 1-seed FLAC ends up above a 140-seed MP3
    even though it'll likely never finish downloading. Buckets put
    practical availability ahead of audio fidelity.

      4  excellent (50+)  — fast, reliable
      3  good (20-49)
      2  ok (5-19)
      1  risky (1-4)      — might trickle in, might not
      0  dead (0)         — won't download
    """
    n = int(seeders or 0)
    if n >= 50: return 4
    if n >= 20: return 3
    if n >= 5:  return 2
    if n >= 1:  return 1
    return 0


def _torrent_name_relevance(name: str, title: str, artist: str, album: str) -> int:
    """Score how likely a torrent name maps to the requested track.

    Used to push the album torrent that *contains* the track above
    other albums by the same artist that don't. Without this signal
    the ranking falls through to seeders and year, so a freshly-
    released album with great seeders outranks a 20-year-old album
    that's actually the right one (e.g. searching "I Miss You" by
    blink-182 surfaces 'ONE MORE TIME (2023)' instead of the 2003
    self-titled album that contains the track).

    Score breakdown (additive, can go negative):
      +100  filename contains the title as a contiguous phrase
       +80  filename contains the album name (and album has tokens
            beyond the artist — distinguishing albums only)
       +40  discography / collection / complete / anthology
       +30  self-titled album match (album tokens ⊆ artist tokens)
       +30  greatest hits / best of
        +0  artist-only torrent with no other signals
       -80  music video / mtv (video, not audio)
       -60  video container hints (1080p / 720p / BDRip / x264 / etc.)
       -40  zip/rar archive (needs extraction; usually a single file)
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()

    name_norm = _norm(name)
    title_norm = _norm(title)
    album_norm = _norm(album)
    artist_norm = _norm(artist)
    name_words = set(name_norm.split())

    score = 0

    # Phrase-match against either the full title or the title with
    # parenthetical metadata stripped — Spotify titles like "A Milli
    # (Album Version Explicit)" never appear verbatim in torrent
    # names, so we'd miss legitimate album torrents otherwise.
    padded_name = f" {name_norm} "
    if title and any(f" {v} " in padded_name
                     for v in _title_phrase_variants(title)):
        score += 100

    # Album-phrase match. The +80 / +30 split distinguishes "real"
    # album matches (album name contains distinguishing tokens beyond
    # the artist name, e.g. "California (Deluxe)") from self-titled
    # matches where the album name IS just the artist name (Weezer's
    # coloured albums, Blink-182 2003, Future's mixtape, etc.).
    #
    # Self-titled albums are toxic for relevance: every other album
    # by that artist matches the artist substring AND would inherit
    # the "album name appears in torrent name" bonus, so a search for
    # an "I Miss You" track on Blink-182's self-titled album would
    # score +30 on every Blink-182 release in existence — Nine,
    # Enema Of The State, Cheshire Cat, all of them. The picker fills
    # with wrong-album false positives.
    #
    # Mitigation: skip the bonus entirely for self-titled cases. The
    # actual self-titled album torrent still has paths to score:
    # an explicit title-phrase match (+100), or discography /
    # greatest-hits / collection markers below (+40 / +30). A torrent
    # named just "Blink-182 (2003)" won't score from this rule, but
    # if it doesn't carry the title or a collection word, we can't
    # tell from the name alone whether it contains the track —
    # better to skip than to surface every album by the artist.
    artist_tokens = set(artist_norm.split())
    album_tokens = set(album_norm.split())
    self_titled = album_tokens and album_tokens.issubset(artist_tokens)
    if album_norm and f" {album_norm} " in f" {name_norm} " and not self_titled:
        score += 80

    if name_words & {"discography", "collection", "complete", "anthology", "boxset"}:
        score += 40

    if {"greatest", "hits"}.issubset(name_words) or {"best", "of"}.issubset(name_words):
        score += 30

    # ── Penalties ───────────────────────────────────────────────
    # Music videos and TV rips happen to share the artist/title
    # phrase but are useless as audio sources — penalise heavily so
    # they sink below real album torrents.
    if {"music", "video"}.issubset(name_words) or "mtv" in name_words:
        score -= 80
    # Video container/codec hints — a 1080p torrent is almost
    # certainly a video, not a music release.
    video_markers = {
        "1080p", "720p", "480p", "2160p", "4k",
        "bdrip", "bluray", "blu-ray", "webrip", "hdtv", "dvdrip",
        "x264", "x265", "h264", "h265", "hevc",
        "mkv", "mp4", "avi", "webm", "mov",
    }
    if name_words & video_markers:
        score -= 60
    # Zip/rar archives need post-download extraction and are usually
    # a single track (or worse, a partial release). Real album
    # torrents almost never package as a single .zip. The penalty is
    # large enough to push a title-phrase-match zip below a
    # discography that contains the same track.
    if name_words & {"zip", "rar", "7z"} or name_norm.endswith(" zip") or name_norm.endswith(" rar"):
        score -= 90

    return score


def build_search_queries(
    title: str, artist: str, album: str = ""
) -> list[tuple[str, str]]:
    """Return the standard (query, qtype) set every indexer runs.

    Three queries:
      1. ``{artist} {album}``      — "album"        (skipped without an
         album, or when the album collapses to the artist)
      2. ``{artist} discography``  — "discography"  (catches multi-album
         dumps named "Discography" / similar)
      3. ``{artist} {title}``      — "track"        (catches single-track
         uploads or torrents named after the song)

    server.py merges all three queries' results across all indexers,
    dedupes by info_hash, and sorts by seeders. The point: predictable,
    inspectable behaviour ("did the album show up?") instead of a
    relevance-scoring black box that occasionally surfaced unrelated
    junk via per-indexer fallback chains.
    """
    out: list[tuple[str, str]] = []
    artist = (artist or "").strip()
    title = (title or "").strip()
    album = (album or "").strip()
    if artist and album and not _album_collapses_to_artist(artist, album):
        out.append((f"{artist} {album}", "album"))
    if artist:
        out.append((f"{artist} discography", "discography"))
    if artist and title:
        out.append((f"{artist} {title}", "track"))
    elif title:
        out.append((title, "track"))
    return out


def _album_collapses_to_artist(artist: str, album: str) -> bool:
    """Return True when the album+artist query reduces to artist alone.

    Indexers tokenise queries case-insensitively and dedupe repeated
    words, so an album with the same name as the artist (Future's
    self-titled mixtape, Weezer's coloured albums, ABBA, etc.) makes
    "{artist} {album}" collapse to just the artist — which then matches
    every torrent with that word in the title (e.g. "Future Mask Off"
    plus "Moby - Future Quiet" plus "Fleetwood Mac - Future Games").
    Skip the album-broadening query in that case so the artist+title
    query alone drives results.
    """
    def _toks(s: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t}
    a = _toks(artist)
    b = _toks(album)
    if not a or not b:
        return False
    # Album adds zero new words on top of artist → the merged query
    # is the artist alone. ("Future" / "FUTURE" → both {"future"}.)
    # An album like "Weezer (Blue Album)" has extra distinguishing
    # tokens ("blue", "album") so this returns False and the album
    # query keeps running.
    return b.issubset(a)


def _files_contain_track(files: list[str], title: str) -> bool:
    """Return True when any filename in the list matches the title as
    a contiguous phrase. The verification primitive — used to stamp
    sources with `verified=True/False` so the picker can show ✅ /
    drop ❌ rather than guess from the torrent name.

    Mirrors `_file_has_title_phrase` (which operates on a single path
    at download time) but for the search-time bulk-verification path.
    """
    variants = _title_phrase_variants(title)
    if not variants:
        return False
    needles = [f" {v} " for v in variants]
    for f in files or []:
        # Strip path; only the basename is the song's filename. A
        # nested folder named after the title shouldn't false-positive
        # (the file inside has to actually be the track).
        leaf = (f or "").replace("\\", "/").split("/")[-1]
        leaf_norm = re.sub(r"\s+", " ",
                          re.sub(r"[^a-z0-9]+", " ", leaf.lower())).strip()
        padded = f" {leaf_norm} "
        if any(n in padded for n in needles):
            return True
    return False
