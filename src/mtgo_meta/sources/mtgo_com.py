"""Fetch Legacy event decklists from mtgo.com.

mtgo.com renders the decklist page from JS, but it embeds the full
JSON payload in a ``window.MTGO.decklists.data = {...};`` script tag.
We grab the raw HTML, regex the JSON out, and parse it.

Event index lives at https://www.mtgo.com/decklists/legacy and lists
recent events as anchor tags. Each event has a URL like
``/decklist/legacy-challenge-32-2026-05-1712842506``.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (mtgo-meta personal-use)"

DATA_RE = re.compile(
    r"window\.MTGO\.decklists\.data\s*=\s*(\{.*?\});",
    re.DOTALL,
)


def _event_link_re(format_slug: str) -> re.Pattern[str]:
    """Compile the event-href regex for a specific format index page.

    mtgo.com links its event pages as ``/decklist/<format>-<event>-…``
    where ``<event>`` is one of: league, challenge-32, challenge-64,
    showcase-(challenge|qualifier), preliminary. The format slug
    (legacy / vintage / modern / pauper / pioneer / standard) is the
    only thing that varies between corpora, so we build the regex
    per-format at scrape time.
    """
    return re.compile(
        rf'href="(/decklist/{re.escape(format_slug)}-'
        r'(?:league|challenge-32|challenge-64|showcase-(?:challenge|qualifier)|preliminary)'
        r'[^"]+)"',
        re.IGNORECASE,
    )


# Known MTGO format slugs we have URL coverage for. Add new ones here
# when MTGO introduces them; the API + classifier handle any string,
# but the scraper needs the exact path component.
KNOWN_FORMAT_SLUGS = (
    "legacy", "vintage", "modern", "pauper",
    "pioneer", "standard", "premodern",
)


@dataclass
class Card:
    qty: int
    name: str
    colors: list[str]          # ['W','U',...] (WUBRG single letters)
    card_type: str             # 'INSTNT','LAND','CREAT', etc. (MTGO encoding)


@dataclass
class Deck:
    event_id: str
    event_date: str           # YYYY-MM-DD
    event_type: str           # "challenge-32", "league", ...
    player: str
    place: int | None
    main_deck: list[Card]
    sideboard: list[Card]

    @property
    def all_unique_cards(self) -> set[str]:
        return {c.name for c in self.main_deck} | {c.name for c in self.sideboard}


def _http_get(url: str, timeout: float = 30.0, retries: int = 3) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                import time as _t
                _t.sleep(1.5 * (attempt + 1))
    assert last_err is not None
    raise last_err


def list_recent_events(format_slug: str = "legacy", days: int = 30) -> list[str]:
    """Return the list of event URLs from mtgo.com's index page for ``format_slug``.

    Pass "legacy" / "vintage" / "modern" / "pauper" / "pioneer" /
    "standard" / "premodern" (or whatever path component mtgo.com is
    currently using).
    """
    url = f"https://www.mtgo.com/decklists/{format_slug}"
    html = _http_get(url)
    link_re = _event_link_re(format_slug)
    urls = []
    seen = set()
    for m in link_re.finditer(html):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        urls.append("https://www.mtgo.com" + path)
    return urls


def list_recent_legacy_events(days: int = 30) -> list[str]:
    """Backward-compat shim for the original Legacy-only function."""
    return list_recent_events("legacy", days=days)


def _parse_date_from_url(url: str) -> str | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", url)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _event_type_from_url(url: str) -> str:
    if "challenge-32" in url:
        return "challenge-32"
    if "challenge-64" in url:
        return "challenge-64"
    if "showcase-challenge" in url:
        return "showcase-challenge"
    if "showcase-qualifier" in url:
        return "showcase-qualifier"
    if "preliminary" in url:
        return "preliminary"
    if "league" in url:
        return "league"
    return "unknown"


def fetch_event(url: str) -> list[Deck]:
    """Return all decks parsed from a single mtgo.com event URL."""
    html = _http_get(url)
    m = DATA_RE.search(html)
    if not m:
        return []
    data = json.loads(m.group(1))
    decks_raw = data.get("decklists") or []
    event_id = data.get("event_id", "")
    event_date = _parse_date_from_url(url) or ""
    event_type = _event_type_from_url(url)

    # Final standings (1..N) appear in data["final_rank"]; map loginid -> place.
    place_map: dict[str, int] = {}
    for rank, entry in enumerate(data.get("final_rank") or [], start=1):
        login = entry.get("loginid") if isinstance(entry, dict) else None
        if login:
            place_map[login] = rank

    _COLOR_MAP = {
        "COLOR_WHITE": "W",
        "COLOR_BLUE": "U",
        "COLOR_BLACK": "B",
        "COLOR_RED": "R",
        "COLOR_GREEN": "G",
    }

    def _make_card(raw: dict) -> Card | None:
        attrs = raw.get("card_attributes") or {}
        name = attrs.get("card_name")
        if not name:
            return None
        cols = [_COLOR_MAP[c] for c in attrs.get("colors", []) if c in _COLOR_MAP]
        return Card(
            qty=int(raw.get("qty", 0)),
            name=name,
            colors=cols,
            card_type=attrs.get("card_type") or "",
        )

    decks: list[Deck] = []
    for d in decks_raw:
        if not isinstance(d, dict):
            continue
        main = [c for c in (_make_card(x) for x in (d.get("main_deck") or [])) if c]
        side = [c for c in (_make_card(x) for x in (d.get("sideboard_deck") or [])) if c]
        decks.append(Deck(
            event_id=str(event_id),
            event_date=event_date,
            event_type=event_type,
            player=d.get("player") or "",
            place=place_map.get(d.get("loginid", "")),
            main_deck=main,
            sideboard=side,
        ))
    return decks


def fetch_recent_decks(
    format_slug: str = "legacy",
    days: int = 30,
    max_events: int = 60,
) -> list[Deck]:
    """Walk the given format's mtgo.com index and fetch every event
    within the time window."""
    urls = list_recent_events(format_slug, days=days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    decks: list[Deck] = []
    seen_events = 0
    for url in urls:
        date_str = _parse_date_from_url(url)
        if date_str and date_str < cutoff:
            continue
        seen_events += 1
        if seen_events > max_events:
            break
        try:
            decks.extend(fetch_event(url))
        except Exception as e:
            print(f"  fetch failed for {url}: {e}")
    return decks


def fetch_recent_legacy_decks(days: int = 30, max_events: int = 60) -> list[Deck]:
    """Backward-compat shim that calls the generalised fetcher."""
    return fetch_recent_decks("legacy", days=days, max_events=max_events)
