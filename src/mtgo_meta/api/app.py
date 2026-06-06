"""FastAPI backend for Metahunter.

Serves a JSON API for the React frontend at /api/*. Reads parsed match
data from the local SQLite store written by scripts/ingest.py (and
kept current by scripts/watch.py). Archetype classification runs on
the fly per request and is cached per process via a small LRU.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from metahunter_core.classifier import (
    SKIP_LABEL,
    build_card_colors,
    build_card_weights,
    classify,
    classify_by_similarity,
    colour_prefix,
    colour_prefix_to_set,
    infer_color_identity,
    load_legacy,
    recompute_deck_color_identity,
)
from mtgo_meta.paths import (
    available_corpora,
    corpus_path,
    default_db_path,
    format_data_dir,
    web_dist_dir,
)
from mtgo_meta.store import open_store

FORMAT_DATA = format_data_dir()
DEFAULT_DB = default_db_path()
WEB_DIST = web_dist_dir()

SIGNATURE_NOISE = {
    "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
    "Orc Army Token", "Samurai Token", "Skeleton Token", "Clue Token",
    "Treasure Token", "Food Token", "Goblin Token", "Soldier Token",
    "Spirit Token", "Zombie Token", "Servo Token", "Thopter Token",
    "Dwarf Token", "Goat Token", "Beast Token", "Snake Token",
    "Insect Token", "Wolf Token", "Bird Token", "Cat Token",
    "Saproling Token", "Elemental Token", "Dragon Token",
    "Angel Token", "Knight Token", "Demon Token", "Frog Token",
    "Eldrazi Token", "Plant Token", "Construct Token",
    "Copy Token", "Emblem Token", "Monk Token",
    "The Initiative", "The Monarch", "Undercity",
}


def _signature(cards: list[str], n: int = 8) -> list[tuple[str, int]]:
    return Counter(c for c in cards if c not in SIGNATURE_NOISE).most_common(n)


def create_app(db_path: Path | None = None) -> FastAPI:
    db = db_path or DEFAULT_DB
    app = FastAPI(title="Metahunter", version="0.0.1")

    # Phase 2: surface the local /api/upload/* routes — consent
    # gate, leaderboard toggle, server-data wipe button.
    from mtgo_meta.api.upload_routes import router as upload_router
    app.include_router(upload_router)

    # Phase 6: in-app auto-updater. Routes are no-ops when no newer
    # release exists upstream — the background worker handles the
    # GitHub poll + download, the frontend banner consumes state.
    from mtgo_meta.api.updater_routes import router as updater_router
    app.include_router(updater_router)

    # Allow the Vite dev server (default 5173) to talk to us in dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://metahunter.localhost:5173",
        ],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    # ---- shared lazy-loaded resources -----------------------------------
    archetypes, fallbacks = load_legacy(FORMAT_DATA)

    # Load every per-format corpus file we can find. The dict keys are
    # the format names as the API exposes them ("Legacy", "Vintage", …).
    # When a request comes in with ?format=X, we look up that corpus
    # for similarity classification; if the corpus is missing the
    # classifier falls back to a colour-code label.
    #
    # ``universal_card_colors`` is a merged colour-lookup table built
    # from all loaded corpora — used as a fallback so colour inference
    # still works for formats without their own corpus.
    corpora: dict[str, dict] = {}
    universal_card_colors: dict[str, str] = {}
    for fmt_name in available_corpora():
        cp = corpus_path(fmt_name)
        if not cp.exists():
            continue
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        decks = data.get("decks", [])
        if not decks:
            continue
        for d in decks:
            d["color_identity"] = recompute_deck_color_identity(d)
        weights = build_card_weights(decks)
        colors = build_card_colors(decks)
        corpora[fmt_name] = {"decks": decks, "weights": weights, "colors": colors}
        # Merge into universal table — first-seen wins. Legacy gets
        # priority simply because it's the largest dataset.
        for name, col in colors.items():
            if name not in universal_card_colors:
                universal_card_colors[name] = col

    # Legacy-shaped variables kept around so call sites that don't yet
    # care about format still work. They reference whichever corpus is
    # the default ("Legacy" if it loaded, else the first available).
    default_fmt = "Legacy" if "Legacy" in corpora else next(iter(corpora), "Legacy")
    corpus_decks = corpora.get(default_fmt, {}).get("decks", [])
    card_weights = corpora.get(default_fmt, {}).get("weights", {})
    card_colors = universal_card_colors or corpora.get(default_fmt, {}).get("colors", {})

    @lru_cache(maxsize=20000)
    def _classify_cached(
        cards_key: tuple[str, ...], cast_key: tuple[str, ...], fmt: str,
    ) -> str:
        """Classify a bag of cards into a specific archetype name.

        Format-aware: looks up ``fmt`` in the loaded corpora and uses
        that one for similarity scoring. When no corpus exists for the
        requested format, falls back to the colour-identity inference
        (returning "UR", "WUR", etc.) so the user still sees something
        meaningful — they just won't get archetype names without a
        format-specific corpus.
        """
        cards = list(cards_key)
        cast = list(cast_key) if cast_key else None
        fmt_corpus = corpora.get(fmt)
        if fmt_corpus and fmt_corpus["decks"]:
            name, _s, _d = classify_by_similarity(
                cards, fmt_corpus["decks"],
                fmt_corpus["weights"], fmt_corpus["colors"],
                cast_cards=cast,
            )
            return name
        # No corpus for this format — produce a colour-code label.
        from metahunter_core.classifier import (
            SKIP_LABEL, _LAND_NAMES, _TOKENS, infer_color_identity,
        )
        if not cards:
            return SKIP_LABEL
        obs = set(cards)
        informative = obs - _LAND_NAMES - _TOKENS
        if len(informative) < 5:
            return SKIP_LABEL
        cols = infer_color_identity(cards, universal_card_colors, cast)
        return cols or "Colourless"

    def _classify(
        cards: list[str],
        cast: list[str] | None = None,
        fmt: str = "Legacy",
    ) -> str:
        cards_key = tuple(sorted(set(cards)))
        cast_key = tuple(sorted(set(cast))) if cast else ()
        return _classify_cached(cards_key, cast_key, fmt or "Legacy")

    def _is_colour_code(label: str) -> bool:
        if not label or label == SKIP_LABEL:
            return False
        if label == "Colourless":
            return True
        return 1 <= len(label) <= 5 and all(c in "WUBRG" for c in label)

    def _consensus(nbrs: list[str]) -> str | None:
        """Pick a consensus archetype from neighbour labels.

        Rules in order of strength — first to fire wins:

        1. **Majority** — the most-frequent name has strictly more than
           half the votes. Clean and conservative.

        2. **Colour-family** — every neighbour shares the same colour
           prefix (Jeskai, Grixis, Bant, 5-Color, …), even if the
           tail differs. Pick the most-common specific name within the
           family. Catches "Jeskai Control"/"Jeskai Tempo" streaks.

        3. **Plurality** — when neither above fires but ≥2 neighbours
           agree on a single name AND that name beats every other name
           individually, use it. This rescues the case where the focal
           match is an outlier in a streak that has SOME variety
           (e.g. 2 Grixis Control + 1 Jeskai Control + 1 Grixis Tempo
           — Grixis Control wins as plurality even though it's not a
           strict majority).
        """
        if not nbrs:
            return None
        count = Counter(nbrs)
        most_common, n = count.most_common(1)[0]
        # 1) Strict majority
        if n * 2 > len(nbrs):
            return most_common
        # 2) Colour-family
        prefixes = {colour_prefix(name) for name in nbrs}
        prefixes.discard(None)
        if len(prefixes) == 1:
            family = next(iter(prefixes))
            in_family = [name for name in nbrs if colour_prefix(name) == family]
            return Counter(in_family).most_common(1)[0][0]
        # 3) Plurality with ≥2 votes and clear leader
        if n >= 2:
            ties = [name for name, c in count.items() if c == n]
            if len(ties) == 1:
                return most_common
        return None

    def _session_user_decks(
        matches: list, user: str, fmt: str = "Legacy"
    ) -> dict[str, str]:
        """Classify each match's user-side deck, then upgrade any
        colour-code or single-anomaly labels using nearby league
        history.

        Neighbour window: the previous 2 and next 2 matches (by time),
        skipping SKIP_LABEL and colour-code labels — i.e. we look at
        the four nearest specific-archetype matches regardless of how
        many days apart they are. This is the user's preference: a
        league game today and another league game three days from now
        are still the same deck.

        Consensus rule: majority OR same colour-family (see
        ``_consensus``). The strict unanimous gate has been retired;
        a single mis-labelled match in a 5-game streak no longer blocks
        the override.

        SKIP_LABEL labels are NEVER upgraded — the user wants too-thin
        matches dropped from every aggregation, not patched up by
        guesswork from neighbours.
        """
        sorted_m = sorted(
            [m for m in matches if user in m.players],
            key=lambda m: m.log_mtime or 0,
        )
        labels: dict[str, str] = {}
        # Cache each match's observed colour identity so the session
        # inference loop can reject colour-incompatible upgrades.
        # Looked up from the per-format corpus's card_colors table.
        fmt_colors = corpora.get(fmt, {}).get("colors") or universal_card_colors
        observed_colors_by_id: dict[str, frozenset[str]] = {}
        for m in sorted_m:
            cards = m.cards_by_player.get(user, [])
            cast = m.cards_cast_by_player.get(user, [])
            labels[m.match_id] = _classify(cards, cast, fmt=fmt)
            observed_colors_by_id[m.match_id] = frozenset(
                infer_color_identity(cards, fmt_colors, cast)
            )

        NEIGHBOUR_COUNT = 3  # ±3 specific-archetype games on each side
        # First pass: snapshot the original labels so neighbour-lookup
        # is based on what was independently classified, not on what
        # we've already overridden in this loop. Otherwise an early
        # override could propagate to its successors.
        original = dict(labels)
        for i, m in enumerate(sorted_m):
            focal = original[m.match_id]
            if focal == SKIP_LABEL:
                # The user explicitly wants too-thin matches ignored.
                continue
            nbrs: list[str] = []
            before = 0
            j = i - 1
            while j >= 0 and before < NEIGHBOUR_COUNT:
                lbl = original[sorted_m[j].match_id]
                if lbl != SKIP_LABEL and not _is_colour_code(lbl):
                    nbrs.append(lbl)
                    before += 1
                j -= 1
            after = 0
            j = i + 1
            while j < len(sorted_m) and after < NEIGHBOUR_COUNT:
                lbl = original[sorted_m[j].match_id]
                if lbl != SKIP_LABEL and not _is_colour_code(lbl):
                    nbrs.append(lbl)
                    after += 1
                j += 1
            consensus = _consensus(nbrs)
            if consensus is None or consensus == focal:
                continue
            # Colour-compatibility gate. The consensus archetype's
            # name-prefix encodes its colour pool (Grixis = U+B+R,
            # Jeskai = W+U+R, Sultai = U+B+G, …). For the upgrade to
            # be honest, the focal match's observed colours must fit
            # INSIDE that pool — the user may have failed to cast a
            # given colour in a single short game (UB observed for a
            # Grixis deck where no red spell hit the table), but they
            # can never have a colour the deck doesn't run.
            #
            # Examples:
            #   * UB observed,  Grixis (UBR) consensus → ALLOW  (no
            #     red cast this match — fine, the deck is Grixis).
            #   * UR observed,  Jeskai (WUR) consensus → ALLOW  (no
            #     white cast — fine).
            #   * BRG observed, Sultai (UBG) consensus → REJECT (R
            #     can't be in a Sultai deck regardless of neighbours).
            #
            # Same shape as the raw classifier's name-prefix check,
            # but with the subset direction matching the "I-may-have-
            # missed-a-colour-but-never-added-one" semantics for
            # session-based upgrades.
            con_prefix = colour_prefix(consensus)
            if con_prefix is not None:
                pool = colour_prefix_to_set(con_prefix)
                obs_cols = observed_colors_by_id.get(m.match_id, frozenset())
                if pool and obs_cols and not obs_cols.issubset(pool):
                    continue
            # Override when consensus disagrees with the focal label.
            # We always override colour-codes; for specific-archetype
            # focals we require ≥2 neighbours so a single-match anomaly
            # can't flip what's actually a real deck switch.
            if _is_colour_code(focal) or len(nbrs) >= 2:
                labels[m.match_id] = consensus
        return labels

    def _all_matches(
        days: int | None,
        from_ts: float | None = None,
        to_ts: float | None = None,
        fmt: str = "Legacy",
    ) -> list:
        """Pull matches from the store, scoped to one MTG format.

        Filtering precedence:
          * If ``from_ts`` is given it's the lower bound (inclusive).
          * Else if ``days`` is given the lower bound is now - days * 86400.
          * ``to_ts`` is an inclusive upper bound when present.
          * ``fmt`` filters by detected format. Defaults to "Legacy" so
            a Vintage-playing friend on the same MTGO client doesn't
            pollute the dashboard. Pass an empty string to disable.

        Old rows that pre-date format detection have format=NULL; we
        treat those as Legacy by default since the corpus is Legacy.
        """
        cutoff_low: float | None = None
        if from_ts is not None:
            cutoff_low = from_ts
        elif days is not None:
            cutoff_low = time.time() - days * 86400
        with open_store(db) as store:
            rows = store.iter_matches(since_mtime=cutoff_low)
        if to_ts is not None:
            rows = [r for r in rows if (r.log_mtime or 0) <= to_ts]
        if fmt:
            # NULL format (legacy data from older parser versions) is
            # treated as the default ("Legacy").
            rows = [r for r in rows if (r.format or "Legacy") == fmt]
        return rows

    @lru_cache(maxsize=1)
    def _primary_user() -> str | None:
        """The single MTGO username this dashboard belongs to.

        Computed once from the FULL database — not the per-request
        filtered subset — so format filtering or date ranges that drop
        all of the user's own matches don't suddenly cause the
        dashboard to claim some opponent or roommate's account is "the
        user". Cached for the lifetime of the app process.

        MTGO's AppFiles folders accumulate data from every account that
        ever logged in on a given Windows user, so a casual second
        account played by a relative still ends up in our DB. We
        unambiguously pick the player with the most matches as the
        owner of this dashboard.
        """
        with open_store(db) as store:
            rows = store.iter_matches()
        c: Counter[str] = Counter()
        for r in rows:
            for p in r.players:
                c[p] += 1
        return c.most_common(1)[0][0] if c else None

    # ---- endpoints ------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "db": str(db),
            "corpus_decks": len(corpus_decks),
            "archetypes": len(archetypes),
        }

    def _resolve_user(override: str | None) -> str | None:
        """Pick the MTGO account this request applies to.

        Honours an explicit ``?user=X`` query param when present, so
        the dashboard can be re-pointed at any second account on the
        same machine via the AccountPicker. Falls back to the
        primary user when no override is given.
        """
        return override or _primary_user()

    @app.get("/api/accounts")
    def accounts() -> list[dict[str, Any]]:
        """Local MTGO accounts that have played on this machine.

        An MTGO ``AppFiles\\<hash>`` folder is per-account: every match
        log inside it has that account as one of the two players. So
        we group matches by their containing folder, find the player
        who dominates each folder, and call them a local account.

        Opponents who happened to face the user many times are NOT
        local — they appear in one match per folder, never as the
        dominant player.

        Threshold: a folder needs ≥3 matches before we consider it
        large enough to host a real account (avoids quirks from a
        bootstrap folder MTGO might create with one stale log).
        """
        from collections import defaultdict
        from pathlib import Path as _Path

        with open_store(db) as store:
            rows = store.iter_matches()
        folder_matches: dict[str, list] = defaultdict(list)
        for r in rows:
            if r.log_path:
                folder = _Path(r.log_path).parent.name
                folder_matches[folder].append(r)

        # Per folder: pick the player with the highest share of
        # appearances. Aggregate across folders so an account that's
        # spread over multiple MTGO update folders shows up once.
        locals_: dict[str, dict[str, Any]] = {}
        for folder, ms in folder_matches.items():
            if len(ms) < 3:
                continue
            c: Counter[str] = Counter()
            for m in ms:
                for p in m.players:
                    c[p] += 1
            if not c:
                continue
            local_name, local_n = c.most_common(1)[0]
            last = max((m.log_mtime or 0.0) for m in ms if local_name in m.players)
            entry = locals_.setdefault(
                local_name,
                {"user": local_name, "matches": 0, "last_played": 0.0},
            )
            entry["matches"] += local_n
            if last > entry["last_played"]:
                entry["last_played"] = last

        out = sorted(locals_.values(), key=lambda x: -x["matches"])
        return out

    @app.get("/api/formats")
    def formats(
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, int]:
        """Match counts grouped by detected format, for the selected user.

        Restricting to one user keeps a second MTGO account on the
        same Windows session — Samu_27 played by a relative, a friend
        who logged in once — from inflating the format counts and
        luring you into a section that's actually their data.
        """
        user = _resolve_user(user_override)
        with open_store(db) as store:
            rows = store.iter_matches()
        c: Counter[str] = Counter()
        for r in rows:
            if user is None or user in r.players:
                c[r.format or "Legacy"] += 1
        return dict(c)

    @app.get("/api/me")
    def me(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, Any]:
        matches = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return {"user": None, "total_matches": 0, "window_days": days}
        user_decks = _session_user_decks(matches, user, fmt=fmt)
        wins = losses = games_w = games_l = 0
        counted = 0
        # On-the-play / on-the-draw splits. Determined by whose name is
        # m.first_player (the player who chose to play first in game 1).
        # Matches where the parser couldn't capture first_player drop
        # out of these counters but still count toward overall stats.
        play_w = play_l = draw_w = draw_l = 0
        first_mtime = None
        last_mtime = None
        for m in matches:
            if user not in m.players:
                continue
            if user_decks.get(m.match_id, SKIP_LABEL) == SKIP_LABEL:
                continue  # too little data — user wants these ignored
            opponent = next((p for p in m.players if p != user), None)
            # MTGO has no draws, so a missing match_winner is a parser
            # miss — user wants those ignored everywhere, not surfaced
            # as "undecided" filler that inflates the counters.
            if not opponent or not m.match_winner:
                continue
            counted += 1
            won = m.match_winner == user
            if won:
                wins += 1
            elif m.match_winner == opponent:
                losses += 1
            for g in m.games:
                if g.get("winner") == user:
                    games_w += 1
                elif g.get("loser") == user:
                    games_l += 1
            # Play / draw split. Skip when first_player wasn't captured.
            if m.first_player == user:
                if won: play_w += 1
                else:   play_l += 1
            elif m.first_player == opponent:
                if won: draw_w += 1
                else:   draw_l += 1
            if first_mtime is None or m.log_mtime < first_mtime:
                first_mtime = m.log_mtime
            if last_mtime is None or m.log_mtime > last_mtime:
                last_mtime = m.log_mtime
        decided = counted
        play_total = play_w + play_l
        draw_total = draw_w + draw_l
        return {
            "user": user,
            "window_days": days,
            "total_matches": counted,
            "decided_matches": decided,
            "match_wins": wins,
            "match_losses": losses,
            "match_winrate": (wins / decided) if decided else None,
            "game_wins": games_w,
            "game_losses": games_l,
            "game_winrate": (games_w / (games_w + games_l)) if (games_w + games_l) else None,
            "play_wins": play_w,
            "play_losses": play_l,
            "play_winrate": (play_w / play_total) if play_total else None,
            "draw_wins": draw_w,
            "draw_losses": draw_l,
            "draw_winrate": (draw_w / draw_total) if draw_total else None,
            "first_match_at": first_mtime,
            "last_match_at": last_mtime,
        }

    def _signature_for_bag(bag: list[str]) -> list[tuple[str, int]]:
        return Counter(
            c for c in bag if c not in SIGNATURE_NOISE
        ).most_common(8)

    @app.get("/api/decks")
    def decks(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        matches = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []
        user_decks = _session_user_decks(matches, user, fmt=fmt)
        agg: dict[str, dict[str, float]] = defaultdict(
            lambda: {"wins": 0, "losses": 0, "last_played": 0.0}
        )
        for m in matches:
            if user not in m.players:
                continue
            opponent = next((p for p in m.players if p != user), None)
            if not opponent or not m.match_winner:
                continue
            arch = user_decks.get(m.match_id, SKIP_LABEL)
            if arch == SKIP_LABEL:
                continue  # too little data — user wants these ignored
            if m.match_winner == user:
                agg[arch]["wins"] += 1
            elif m.match_winner == opponent:
                agg[arch]["losses"] += 1
            if (m.log_mtime or 0) > agg[arch]["last_played"]:
                agg[arch]["last_played"] = m.log_mtime or 0

        # Noise pruning per user request: drop buckets with fewer than 5
        # matches whose last appearance is more than 30 days ago. Those
        # are almost always misclassifications (a colour code that
        # session inference couldn't upgrade, a one-shot brew that got
        # mis-labelled, etc.) and they clutter the panel without
        # carrying any actionable signal.
        #
        # We only apply this pruning when the window has enough data
        # for the threshold to be meaningful. If the user's format
        # selection (or a tight date range) leaves only a handful of
        # matches, hiding sub-5-match buckets would empty the panel.
        # Threshold: pruning kicks in once aggregations total ≥ 25 or
        # so — under that we surface everything we have.
        MIN_MATCHES = 5
        STALE_SECONDS = 30 * 86400
        PRUNE_FLOOR = 25
        now = time.time()
        window_total = sum(
            int(rec["wins"]) + int(rec["losses"]) for rec in agg.values()
        )
        prune_enabled = window_total >= PRUNE_FLOOR

        out = []
        for name, rec in agg.items():
            total = int(rec["wins"]) + int(rec["losses"])
            stale = (now - rec["last_played"]) > STALE_SECONDS
            if prune_enabled and total < MIN_MATCHES and stale:
                continue
            out.append({
                "archetype": name,
                "wins": int(rec["wins"]),
                "losses": int(rec["losses"]),
                "total": total,
                "last_played": rec["last_played"],
                "winrate": rec["wins"] / total if total else None,
            })
        out.sort(key=lambda x: -x["total"])
        return out

    @app.get("/api/opponents")
    def opponents(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        matches = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []
        agg: dict[str, dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0})
        for m in matches:
            if user not in m.players:
                continue
            opponent = next((p for p in m.players if p != user), None)
            if not opponent or not m.match_winner:
                continue
            arch = _classify(
                m.cards_by_player.get(opponent, []),
                m.cards_cast_by_player.get(opponent, []),
                fmt=fmt,
            )
            if arch == SKIP_LABEL:
                continue
            if m.match_winner == user:
                agg[arch]["wins"] += 1
            elif m.match_winner == opponent:
                agg[arch]["losses"] += 1
        out = []
        for name, rec in agg.items():
            total = rec["wins"] + rec["losses"]
            out.append({
                "archetype": name,
                "wins": rec["wins"],
                "losses": rec["losses"],
                "total": total,
                "winrate": rec["wins"] / total if total else None,
            })
        out.sort(key=lambda x: -x["total"])
        return out

    @app.get("/api/matches")
    def matches(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        your_deck: str | None = None,
        their_deck: str | None = None,
        opponent: str | None = None,
        result: str | None = Query(None, pattern="^(W|L)$"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=500),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, Any]:
        all_m = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        user_decks = _session_user_decks(all_m, user, fmt=fmt) if user else {}
        rows = []
        for m in all_m:
            if user not in m.players:
                continue
            opp = next((p for p in m.players if p != user), None)
            if not opp:
                continue
            yarch = user_decks.get(m.match_id, SKIP_LABEL)
            oarch = _classify(
                m.cards_by_player.get(opp, []),
                m.cards_cast_by_player.get(opp, []),
                fmt=fmt,
            )
            # Honour the user's rule: matches with insufficient data
            # are completely ignored — never shown in the table.
            if yarch == SKIP_LABEL or oarch == SKIP_LABEL:
                continue
            # MTGO has no draws — a missing match_winner means we
            # couldn't parse a clear winner. Drop the row entirely
            # rather than display it as a "?" placeholder.
            if not m.match_winner:
                continue
            won = m.match_winner == user
            res = "W" if won else "L"
            # Filters are case-insensitive substring matches so "delv"
            # finds "Izzet Delver" / "Sultai Delver" / etc.
            if your_deck and your_deck.lower() not in yarch.lower():
                continue
            if their_deck and their_deck.lower() not in oarch.lower():
                continue
            if opponent and opponent.lower() not in opp.lower():
                continue
            if result and res != result:
                continue
            # MTGO logs "X chooses to play first" at the start of game 1
            # and we capture that as m.first_player. Match-level "on the
            # play" status is conventionally defined by game 1.
            if m.first_player is None:
                on_play: bool | None = None
            else:
                on_play = m.first_player == user
            rows.append({
                "match_id": m.match_id,
                "log_mtime": m.log_mtime,
                "opponent": opp,
                "your_deck": yarch,
                "their_deck": oarch,
                "match_winner": m.match_winner,
                "result": res,
                "your_games": sum(1 for g in m.games if g.get("winner") == user),
                "their_games": sum(1 for g in m.games if g.get("winner") == opp),
                "turns": m.turns,
                "on_play": on_play,
            })
        rows.sort(key=lambda r: -(r["log_mtime"] or 0))
        total = len(rows)
        start = (page - 1) * page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": rows[start:start + page_size],
        }

    @app.get("/api/matchups")
    def matchups(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        your_deck: str | None = None,
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        all_m = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []
        user_decks = _session_user_decks(all_m, user, fmt=fmt)
        cell: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"wins": 0, "losses": 0}
        )
        for m in all_m:
            if user not in m.players:
                continue
            opp = next((p for p in m.players if p != user), None)
            if not opp or not m.match_winner:
                continue
            yarch = user_decks.get(m.match_id, SKIP_LABEL)
            oarch = _classify(
                m.cards_by_player.get(opp, []),
                m.cards_cast_by_player.get(opp, []),
                fmt=fmt,
            )
            if yarch == SKIP_LABEL or oarch == SKIP_LABEL:
                continue
            if your_deck and yarch != your_deck:
                continue
            key = (yarch, oarch)
            if m.match_winner == user:
                cell[key]["wins"] += 1
            elif m.match_winner == opp:
                cell[key]["losses"] += 1
        out = []
        for (y, o), rec in cell.items():
            total = rec["wins"] + rec["losses"]
            out.append({
                "your_deck": y,
                "their_deck": o,
                "wins": rec["wins"],
                "losses": rec["losses"],
                "total": total,
                "winrate": rec["wins"] / total if total else None,
            })
        out.sort(key=lambda r: (-r["total"], r["your_deck"], r["their_deck"]))
        return out

    @app.get("/api/timeline")
    def timeline(
        days: int | None = Query(None, ge=1, le=3650),
        from_ts: float | None = Query(None, alias="from"),
        to_ts: float | None = Query(None, alias="to"),
        fmt: str = Query("Legacy", alias="format"),
        user_override: str | None = Query(None, alias="user"),
    ) -> list[dict[str, Any]]:
        """Per-day match aggregates for the active user.

        Returns ascending-by-date list of:
          { date, matches, wins, losses, winrate, ts }
        Days without any matches are filled with zeros so the chart's
        x-axis is continuous.
        """
        from datetime import date, datetime, timedelta, timezone

        all_m = _all_matches(days, from_ts, to_ts, fmt=fmt)
        user = _resolve_user(user_override)
        if not user:
            return []

        buckets: dict[str, dict[str, Any]] = {}
        first_d: date | None = None
        last_d: date | None = None
        for m in all_m:
            if user not in m.players:
                continue
            opp = next((p for p in m.players if p != user), None)
            # Skip matches with no detected winner — MTGO has no draws,
            # so these are parser misses the user wants invisible.
            if not opp or not m.log_mtime or not m.match_winner:
                continue
            d = datetime.fromtimestamp(m.log_mtime, tz=timezone.utc).date()
            key = d.isoformat()
            b = buckets.setdefault(key, {"date": key, "matches": 0, "wins": 0, "losses": 0})
            b["matches"] += 1
            if m.match_winner == user:
                b["wins"] += 1
            elif m.match_winner == opp:
                b["losses"] += 1
            if first_d is None or d < first_d:
                first_d = d
            if last_d is None or d > last_d:
                last_d = d

        if not buckets or first_d is None or last_d is None:
            return []

        # Fill missing days with zero rows.
        out: list[dict[str, Any]] = []
        cursor = first_d
        while cursor <= last_d:
            key = cursor.isoformat()
            b = buckets.get(key) or {"date": key, "matches": 0, "wins": 0, "losses": 0}
            decided = b["wins"] + b["losses"]
            b["winrate"] = (b["wins"] / decided) if decided else None
            b["ts"] = int(datetime(cursor.year, cursor.month, cursor.day, tzinfo=timezone.utc).timestamp())
            out.append(b)
            cursor += timedelta(days=1)
        return out

    @app.get("/api/match/{match_id}")
    def match(
        match_id: str,
        user_override: str | None = Query(None, alias="user"),
    ) -> dict[str, Any]:
        # Disable the format filter for a direct lookup — the user came
        # in with an exact match_id and shouldn't get an empty response
        # just because the dashboard happens to be on a different format.
        all_m = _all_matches(None, fmt="")
        for m in all_m:
            if m.match_id == match_id:
                user = _resolve_user(user_override)
                cards_by_player = m.cards_by_player
                cards_cast_by_player = m.cards_cast_by_player
                # Classify each player using the match's own detected
                # format so a Vintage match isn't matched against the
                # Legacy corpus and vice-versa.
                match_fmt = m.format or "Legacy"
                arch = {
                    p: _classify(
                        cards_by_player.get(p, []),
                        cards_cast_by_player.get(p, []),
                        fmt=match_fmt,
                    )
                    for p in m.players
                }
                signatures = {
                    p: _signature(cards_by_player.get(p, []), n=15) for p in m.players
                }
                return {
                    "match_id": m.match_id,
                    "log_path": m.log_path,
                    "log_mtime": m.log_mtime,
                    "players": m.players,
                    "first_player": m.first_player,
                    "turns": m.turns,
                    "match_winner": m.match_winner,
                    "score": [m.score_won, m.score_lost] if m.score_won is not None else None,
                    "games": m.games,
                    "archetypes": arch,
                    "signatures": {p: [{"name": n, "count": c} for n, c in sigs]
                                   for p, sigs in signatures.items()},
                    "cards_by_player": cards_by_player,
                    "user": user,
                }
        raise HTTPException(404, "match not found")

    @app.get("/api/match/{match_id}/log")
    def match_log(match_id: str) -> dict[str, Any]:
        """Return the human-readable text content of the match's .dat
        log file.

        MTGO's Match_GameLog_*.dat is a mostly-text binary format with
        sparse control bytes scattered through it. We decode the raw
        bytes as latin-1 (round-trips arbitrary bytes safely) and then
        substitute the binary noise with a thin marker so the
        printable parts (player names, card tokens, attack/cast/draw
        events) stay readable in the UI.

        Capped at 200 KB of input to keep huge logs from bloating the
        response. That's well above any real MTGO match log.
        """
        all_m = _all_matches(None, fmt="")
        m = next((row for row in all_m if row.match_id == match_id), None)
        if m is None:
            raise HTTPException(404, "match not found")
        try:
            raw = Path(m.log_path).read_bytes()
        except (OSError, FileNotFoundError):
            raise HTTPException(404, "log file unavailable")
        MAX_BYTES = 200_000
        truncated = len(raw) > MAX_BYTES
        if truncated:
            raw = raw[:MAX_BYTES]
        # latin-1 is the standard "decode any byte to a single
        # codepoint" trick. We then strip the non-printable control
        # bytes — the C0 control range (\x00-\x1f except \t\n\r),
        # DEL (\x7f), the C1 control range (\x80-\x9f) and the rest of
        # the high-byte range (\xa0-\xff) which is binary record
        # padding in MTGO's log format. Whatever survives is
        # printable ASCII (player names, card tokens, event verbs) —
        # exactly what the user wants to read.
        text = raw.decode("latin-1", errors="replace")
        import re as _re
        # Replace every non-ASCII-printable byte with a newline so the
        # readable text segments stand on their own lines.
        text = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\xff]+", "\n", text)
        # Collapse runs of blank lines / whitespace.
        text = _re.sub(r"\n{2,}", "\n", text)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        return {
            "match_id": match_id,
            "log_path": m.log_path,
            "size_bytes": len(raw),
            "truncated": truncated,
            "text": text,
        }

    # Serve the built frontend in production. The SPA's client-side
    # router owns every non-/api route, so we serve index.html as the
    # fallback for anything not found in /assets/.
    if WEB_DIST.exists() and (WEB_DIST / "index.html").exists():
        from fastapi.responses import FileResponse

        app.mount(
            "/assets",
            StaticFiles(directory=str(WEB_DIST / "assets")),
            name="web-assets",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):  # noqa: ARG001
            return FileResponse(WEB_DIST / "index.html")

    return app


# Convenience for `uvicorn mtgo_meta.api.app:app`.
app = create_app()
