# mtgo-meta

Personal MTGO match-log scraper and meta dashboard. Reads the local game
logs MTGO writes under `%LocalAppData%\Apps\2.0\Data\...\AppFiles\` and
turns them into structured match data you can analyse.

Scope (v0): parse `Match_GameLog_<GUID>.dat` files into a list of
(player, card_seen) pairs plus the match outcome. Everything else
builds on that.

## Layout

```
src/mtgo_meta/
    config.py        Paths and discovery
    parser/
        game_log.py  Parser for Match_GameLog_*.dat
scripts/
    parse_one.py     Dev helper: parse one file and pretty-print it
tests/
```

## Quickstart (dev)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python scripts\parse_one.py <path-to-Match_GameLog_*.dat>
```
