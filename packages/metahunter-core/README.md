# metahunter-core

Shared classifier + log parser for the Metahunter ecosystem.

Used by:

- **metahunter-app** (this repo, `..`) — the desktop client. Imports
  the classifier to label observed match bags locally.
- **metahunter-server** (separate repo, next phase) — the central
  ingest API. Re-classifies uploaded match bags server-side so
  classifier improvements apply retroactively to all historical data
  without requiring every desktop user to update their .exe.

## What's in here

- `classifier.py` — similarity-based deck classification, signature
  requirements, colour-identity inference, archetype-name
  normalisation (Show-and-Tell variants, Yorion suffix stripping,
  Blue Artifacts → Affinity/Izzet Artifacts renaming, etc.).
- `naming.py` + `colors.py` — colour-prefix construction
  (`Jeskai Control` from a `WUR` deck identity).
- `parser/game_log.py` — MTGO Match_GameLog binary parser. Extracts
  players, cards observed, cards cast, turns, winner, on-the-play
  status. Bumps `PARSER_VERSION` to trigger auto-reingest when
  parsing rules change.
- `parser/game_history.py` — NRBF (.NET BinaryFormatter) parser for
  MTGO's `mtgo_game_history` — the ground-truth format-per-match
  lookup.
- `sources/mtgo_com.py` — mtgo.com Challenge / League decklist
  scraper. Used offline by the corpus builder script.

## Why a separate package

Splitting the classifier out lets the server and the client share
exact same parsing + scoring logic without copy/paste drift. A
classifier fix lands in this package, both downstream apps pick it
up on their next install / redeploy.

## Local dev

```powershell
# From the metahunter-app repo root:
pip install -e packages/metahunter-core
pip install -e .
```

The client's `pyproject.toml` does NOT yet pin `metahunter-core` as
a published dependency — both packages install editably from the
same checkout. Once the server lands in its own repo (Phase 1b),
`metahunter-core` moves to its own GitHub repo and both client and
server consume it via a git URL.
