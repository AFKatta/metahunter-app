# metahunter-app

Local MTGO match-log scraper + classifier + React dashboard. Reads
the game logs MTGO writes under
`%LocalAppData%\Apps\2.0\Data\...\AppFiles\` and turns them into a
queryable personal meta dashboard.

This is the **desktop client** of the broader Metahunter ecosystem.
The shared classifier + log parser lives in
[`packages/metahunter-core/`](packages/metahunter-core/) so the
(eventual) central ingest server can re-classify uploaded matches
with the same logic.

## Repos in the ecosystem

| Repo | Purpose | Status |
|---|---|---|
| `metahunter-app` (this one) | Desktop client (.exe) | active |
| `metahunter-core` | Shared classifier + parser, vendored at `packages/metahunter-core/` | in-tree until Phase 1b |
| `metahunter-server` | Central FastAPI ingest + read API | upcoming |
| `metahunter-web` | Public meta site | upcoming |

## Layout

```
src/mtgo_meta/                Local-only code (filesystem, SQLite, FastAPI)
    api/        local FastAPI dashboard server
    store/      SQLite persistence
    ingest.py   scan MTGO log dir → DB
    config.py   find ClickOnce AppFiles dirs
    paths.py    user-data dir, corpus paths, frozen vs dev resolution
packages/metahunter-core/     Shared library (classifier + parser)
    src/metahunter_core/
        classifier.py
        naming.py + colors.py
        parser/   game_log.py + game_history.py
        sources/  mtgo_com.py (corpus builder)
scripts/                      One-shot dev / diagnostic scripts
web/                          Vite + React + Tailwind dashboard
data/                         Local SQLite + corpus JSON (gitignored)
```

## Dev setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
# Install the shared library first (editable), then the client (also editable).
pip install -e packages/metahunter-core
pip install -e ".[dev]"
# Frontend
cd web ; npm install ; cd ..
```

## Run the local dashboard

```powershell
python scripts\serve.py
# opens http://localhost:8000
```

## Build the .exe

```powershell
.\build.ps1
```

Produces `dist/metahunter.exe`.
