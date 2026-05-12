"""AudiobookBay (audiobook-specific tracker; listings-only).

Public WordPress-themed site. The search page returns post stubs
with title + slug; we expose the slug as `topic_id`. resolve.stream
fetches the detail page on demand and reassembles the magnet from
the info hash + tracker list (the literal magnet:?… URI was
stripped from page templates years ago).
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse

import httpx


ABB_DEFAULT_BASE = "https://audiobookbay.fi"
ABB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_ABB_BOOKMARK_RE = re.compile(
    r'<a\s+[^>]*?href="([^"]+)"[^>]*?\brel="bookmark"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_ABB_INFOHASH_RE = re.compile(
    r"<td>\s*Info\s*Hash\s*:?\s*</td>\s*<td>\s*([a-fA-F0-9]{40})\s*</td>",
    re.IGNORECASE,
)
_ABB_TRACKER_RE = re.compile(
    r"<td(?:\s+[^>]*)?>\s*((?:udp|http|wss?)://[^\s<]+)\s*</td>",
    re.IGNORECASE,
)
# Pulls the inLanguage value from the detail page. ABB writes it like
# ``<span itemprop='inLanguage'>French</span>``. The listing pages
# don't expose this — the only reliable filter for untagged
# translations (e.g. ``1984 - George Orwell`` whose audio is actually
# French) is to fetch the detail page and read this field.
_ABB_LANGUAGE_RE = re.compile(
    r"itemprop=['\"]inLanguage['\"][^>]*>([^<]+)<",
    re.IGNORECASE,
)
_ABB_NON_ENGLISH_DETAIL_LANGS = {
    "french", "français", "francais",
    "german", "deutsch",
    "spanish", "español", "espanol", "castellano",
    "italian", "italiano",
    "dutch", "nederlands",
    "russian", "русский",
    "portuguese", "português", "portugues",
    "polish", "polski",
    "swedish", "svenska",
    "norwegian", "norsk",
    "danish", "dansk",
    "finnish", "suomi",
    "czech", "čeština", "cestina",
    "hungarian", "magyar",
    "turkish", "türkçe", "turkce",
    "greek", "ελληνικά",
    "romanian", "română",
    "ukrainian", "українська",
    "chinese", "中文",
    "japanese", "日本語",
    "korean", "한국어",
    "arabic", "العربية",
    "hindi", "हिन्दी",
    "hebrew", "עברית",
}
_ABB_MAGNET_RE = re.compile(
    r'magnet:\?xt=urn:btih:([A-Fa-f0-9]{40}|[A-Z2-7]{32})[^"\'\s<>]*',
    re.IGNORECASE,
)
_ABB_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into",
    "audiobook", "book", "vol", "volume", "edition", "of", "in",
    "on", "at", "by", "to", "an", "is", "as", "or",
}

# Title markers that signal a non-English edition on AudiobookBay.
# Their listings tag the language inline in titles like
# "1984 [german] - George Orwell" or "1984 (Spanish Edition)".
# A conservative substring match drops the obvious ones — items that
# don't tag a language get the benefit of the doubt (most English
# audiobooks ship without a tag).
_ABB_NON_ENGLISH_TITLE_MARKERS = (
    "[german]", "[deutsch]", "(german)", "(deutsche", "(spanish",
    "(french]", "(french)", "[french]", "[français]", "(français",
    "(italian)", "[italian]", "(italiano)",
    "(dutch)", "[dutch]", "(nederlands)",
    "(russian)", "[russian]", "(русский",
    "(portuguese)", "[portuguese]", "(português)",
    "(polish)", "[polish]", "polish edition",
    "(swedish)", "[swedish]", "(svenska)",
    "(chinese)", "[chinese]", "(中文)",
    "(japanese)", "[japanese]", "日本語",
    "(korean)", "[korean]", "한국어",
    "(arabic)", "[arabic]",
    "(hindi)", "[hindi]",
    "(turkish)", "[turkish]", "türkçe",
    "(czech)", "[czech]", "(čeština)",
    "(danish)", "[danish]",
    "(norwegian)", "[norwegian]",
    "(finnish)", "[finnish]",
    "(hungarian)", "[hungarian]",
    "(greek)", "[greek]",
    "(romanian)", "[romanian]",
    "(ukrainian)", "[ukrainian]",
    " edition)",   # generic catch-all for "(<Language> Edition)" patterns
    "ungekürzt",
    "hörspiel",
    "horspiel",
    "komplettfassung",
    "deutsche fassung",
)


def _abb_title_is_english(title: str) -> bool:
    """Best-effort English filter for ABB listings. Conservative: only
    rejects titles with explicit non-English markers; untagged titles
    pass through (most English audiobooks ship that way)."""
    if not title:
        return True
    t = title.lower()
    for marker in _ABB_NON_ENGLISH_TITLE_MARKERS:
        if marker in t:
            return False
    return True


def _abb_strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _abb_base(cfg: dict) -> str:
    base = (cfg.get("audiobookbay_base") or ABB_DEFAULT_BASE).strip()
    return base.rstrip("/")


def _abb_query_words(s: str) -> set[str]:
    toks = re.split(r"[^a-z0-9]+", (s or "").lower())
    return {w for w in toks if len(w) >= 4 and w not in _ABB_STOPWORDS}


def _abb_build_query(title: str, artist: str) -> str:
    """Empirically WP search behaves best with 2-3 distinctive lowercase
    tokens drawn from the title (and one author word as tiebreaker)."""
    title_toks: list[str] = []
    seen: set[str] = set()
    for w in re.split(r"\s+", (title or "").strip()):
        stripped = re.sub(r"[^A-Za-z0-9']", "", w).lower()
        if not stripped or stripped in seen or stripped in _ABB_STOPWORDS:
            continue
        if len(stripped) < 3:
            continue
        seen.add(stripped)
        title_toks.append(stripped)
        if len(title_toks) >= 3:
            break
    author_tok = ""
    for w in reversed(re.split(r"\s+", (artist or "").strip())):
        stripped = re.sub(r"[^A-Za-z0-9']", "", w).lower()
        if stripped and len(stripped) >= 4 and stripped not in seen:
            author_tok = stripped
            break
    if author_tok:
        title_toks.append(author_tok)
    return " ".join(title_toks)


_ABB_AUTHOR_DASH_RE = re.compile(r"\s+[-–—]\s+(.+?)\s+[-–—]\s+", re.IGNORECASE)


async def search_audiobookbay_books(cfg: dict, q: str, limit: int = 30) -> list[dict]:
    """Discovery search — returns book-shaped results (title/author/cover)
    rather than the source-shaped entries `search_audiobookbay` returns
    for resolve.sources.

    Same WP search backend; we just shape the output for the
    audiobook-discovery view instead of the source picker. AudiobookBay
    listings don't surface a separate cover URL in the results page,
    so cover stays empty — the frontend falls back to a Monogram.

    Author parsing is a best-effort string split — listings are titled
    in many forms ("Author - Title", "Title by Author", "Title
    (Unabridged)"). When parsing fails we leave author empty rather
    than guessing.
    """
    if not q:
        return []
    base = _abb_base(cfg)
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={"User-Agent": ABB_UA},
        ) as client:
            r = await client.get(f"{base}/", params={"s": q})
            if r.status_code != 200:
                return []
            body = r.text
    except Exception:
        return []
    out: list[dict] = []
    seen_slugs: set[str] = set()
    for href, raw_title in _ABB_BOOKMARK_RE.findall(body):
        title_text = _abb_strip_tags(raw_title)
        if not title_text:
            continue
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug in seen_slugs:
            continue
        # Same English-only filter as resolve.sources — drop tagged
        # translations from the discovery view too so the picker
        # doesn't surface them in the first place.
        if not _abb_title_is_english(title_text):
            continue
        seen_slugs.add(slug)
        # Best-effort author split. ABB titles vary
        # ("Title - Author", "Author - Title", multi-dash variants
        # like "Title - Edition - Author"). Heuristic: split on " - "
        # and take the last chunk as author IF it looks name-shaped
        # (short, ≤4 words, contains a capital letter or period).
        # Otherwise leave author blank — the frontend renders "—" and
        # the user can still recognise the book.
        author = ""
        title_clean = title_text
        if " - " in title_text:
            parts = [p.strip() for p in title_text.split(" - ") if p.strip()]
            if len(parts) >= 2:
                tail = parts[-1]
                word_count = len(tail.split())
                looks_like_name = (
                    len(tail) <= 60
                    and word_count <= 5
                    and any(c.isupper() for c in tail)
                )
                if looks_like_name:
                    author = tail
                    title_clean = " - ".join(parts[:-1])
        out.append({
            "title": title_clean,
            "author": author,
            "cover": "",
            "isbn": "",
            "year": 0,
            "subjects": ["audiobookbay"],
            "_abb_slug": slug,
        })
        if len(out) >= limit:
            break
    return out


async def search_audiobookbay(cfg: dict, ctx: dict) -> list[dict]:
    """Returns lazy entries — info_hash is empty until resolve.stream
    fetches the detail page. Filters out the recent-uploads fallback
    by requiring at least one query token to overlap the title."""
    if ctx.get("kind") != "audiobook":
        return []
    base = _abb_base(cfg)
    artist = ctx.get("artist", "")
    title = ctx.get("title", "")
    q = _abb_build_query(title, artist)
    if not q:
        return []
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={"User-Agent": ABB_UA},
        ) as client:
            r = await client.get(f"{base}/", params={"s": q})
            if r.status_code != 200:
                print(f"[abb] search status={r.status_code} q={q!r}")
                return []
            body = r.text
    except Exception as e:
        print(f"[abb] search error q={q!r}: {type(e).__name__}: {e}")
        return []

    query_tokens = _abb_query_words(f"{title} {artist}")
    out: list[dict] = []
    seen_slugs: set[str] = set()
    for href, raw_title in _ABB_BOOKMARK_RE.findall(body):
        title_text = _abb_strip_tags(raw_title)
        if not title_text:
            continue
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug in seen_slugs:
            continue
        if query_tokens and not (query_tokens & _abb_query_words(title_text)):
            continue
        # Drop non-English editions inline (ABB tags language in the
        # title for translated releases — "1984 [german]", "1984
        # (Spanish Edition)", etc.). Without this the picker showed
        # the user a German radio play and a French translation
        # ranked above the actual English audiobook.
        if not _abb_title_is_english(title_text):
            continue
        seen_slugs.add(slug)
        out.append({
            "name": title_text,
            "seeders": 0,
            "size": 0,
            "rd_link": "",
            "link_type": "magnet",
            "source": "audiobookbay",
            "info_hash": "",
            "topic_id": slug,
        })
    out = out[:25]
    # Eager top-N detail hydration — populates info_hash on the rows
    # the user is likely to click first, so the downstream BEP-15
    # verify step can attach live seeder counts. Without this every
    # ABB row arrived at the picker as "?↑ unknown seeders" which made
    # ranking impossible. Lazy fallback for the long tail keeps total
    # search latency bounded.
    try:
        await _abb_hydrate_all(cfg, out)
    except Exception as e:
        print(f"[abb] eager hydrate error: {type(e).__name__}: {e}")
    out = [e for e in out if not e.get("_drop_non_english")]
    print(f"[abb] q={q!r} returned={len(out)} (post-filter, all hydrated)")
    return out


async def _abb_fetch_detail(cfg: dict, slug: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """Fetch one detail page and return ``{magnet, info_hash, trackers}``.
    Returns None if the page wasn't reachable or didn't carry an info
    hash. The optional shared client lets callers (e.g. the eager
    top-N hydrator below) reuse a single connection pool across
    parallel fetches."""
    base = _abb_base(cfg)
    own_client = client is None
    try:
        if own_client:
            client = httpx.AsyncClient(
                timeout=15, follow_redirects=True,
                headers={"User-Agent": ABB_UA},
            )
        body = ""
        for path in (f"/abss/{slug}/", f"/audio-books/{slug}/", f"/{slug}/"):
            r = await client.get(f"{base}{path}")
            if r.status_code == 200:
                body = r.text
                break
        if not body:
            print(f"[abb] detail no usable path slug={slug}")
            return None
        info_hash = ""
        magnet = ""
        m = _ABB_MAGNET_RE.search(body)
        if m:
            magnet = m.group(0)
            ih_in_magnet = re.search(r"btih:([A-Fa-f0-9]{40}|[A-Z2-7]{32})", magnet)
            if ih_in_magnet:
                info_hash = ih_in_magnet.group(1).lower()
        if not info_hash:
            ih_m = _ABB_INFOHASH_RE.search(body)
            if not ih_m:
                print(f"[abb] detail no info hash slug={slug}")
                return None
            info_hash = ih_m.group(1).lower()
        trackers: list[str] = []
        seen: set[str] = set()
        for t in _ABB_TRACKER_RE.findall(body):
            t = t.strip()
            if not t or t in seen:
                continue
            seen.add(t)
            trackers.append(t)
            if len(trackers) >= 30:
                break
        if not magnet:
            tr_params = "".join(
                "&tr=" + urllib.parse.quote(t, safe="") for t in trackers
            )
            magnet = f"magnet:?xt=urn:btih:{info_hash}{tr_params}"
        lang_m = _ABB_LANGUAGE_RE.search(body)
        language = lang_m.group(1).strip() if lang_m else ""
        print(f"[abb] detail slug={slug} ih={info_hash[:12]} trackers={len(trackers)} lang={language!r}")
        return {
            "magnet": magnet,
            "info_hash": info_hash,
            "trackers": trackers,
            "language": language,
        }
    except Exception as e:
        print(f"[abb] detail error slug={slug}: {type(e).__name__}: {e}")
        return None
    finally:
        if own_client and client is not None:
            await client.aclose()


async def _abb_fetch_magnet(cfg: dict, slug: str) -> str | None:
    """Back-compat wrapper kept for resolve.stream callers that just
    want the magnet URI."""
    detail = await _abb_fetch_detail(cfg, slug)
    return detail["magnet"] if detail else None


# Concurrency cap on detail fetches. ABB's frontend tolerates ~8
# parallel connections from one client without throttling; going
# higher risks 429s and adds little latency benefit (the HTML pages
# are tiny). 25 fetches at ~500ms each, 8-wide, finishes in ~2s.
_ABB_HYDRATE_CONCURRENCY = 8


async def _abb_hydrate_all(cfg: dict, entries: list[dict]) -> None:
    """Fan out detail fetches for every listing entry so we can drop
    non-English releases (ABB doesn't tag language in the listing —
    e.g. ``1984 - George Orwell`` was silently the French full-cast
    Lambert Wilson dub). Mutates entries in place: stamps
    ``info_hash`` / ``rd_link`` / ``trackers`` on English rows, sets
    ``_drop_non_english`` on translations.

    Bounded by ``_ABB_HYDRATE_CONCURRENCY`` so we don't hammer
    AudiobookBay with 25+ simultaneous requests. Failures stay lazy
    (`info_hash=""`) and the picker falls back to "?↑" for those
    rows — caller is expected to keep them in the result list since
    we can't prove they're not English."""
    targets = [e for e in entries if not e.get("info_hash")]
    if not targets:
        return
    sem = asyncio.Semaphore(_ABB_HYDRATE_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True,
        headers={"User-Agent": ABB_UA},
    ) as client:
        async def fetch_one(entry):
            async with sem:
                return await _abb_fetch_detail(cfg, entry["topic_id"], client=client)
        results = await asyncio.gather(
            *(fetch_one(e) for e in targets),
            return_exceptions=True,
        )
    for entry, detail in zip(targets, results):
        if isinstance(detail, BaseException) or not detail:
            # Couldn't read the detail page — leave the row alone.
            # It'll show "?↑" seeders; if the user clicks it,
            # resolve.stream re-fetches the detail page at play
            # time and the user just hears a non-English audiobook.
            # That's a worse failure mode than dropping unknowns,
            # but dropping every "couldn't reach detail page" row
            # would empty the picker on a flaky network.
            continue
        # Drop hydrated rows whose detail page declares a non-English
        # language. The listing's plain title is unreliable —
        # "1984 - George Orwell" displays the same way for the
        # English audiobook and for the French full-cast dub.
        lang = (detail.get("language") or "").strip().lower()
        if lang and lang in _ABB_NON_ENGLISH_DETAIL_LANGS:
            entry["_drop_non_english"] = True
            continue
        entry["info_hash"] = detail["info_hash"]
        # The internal field name is `rd_link` (the resolve_sources
        # serialiser at server.py maps `rd_link → link` on output);
        # writing to `link` here would silently drop on the wire.
        entry["rd_link"] = detail["magnet"]
        entry["link_type"] = "magnet"
        if detail.get("trackers"):
            entry["trackers"] = detail["trackers"]


# Back-compat alias for any caller that imported the old name.
_abb_hydrate_top = _abb_hydrate_all
