# metahunter-app

Local MTGO match-log scraper + classifier + React dashboard. Reads
the game logs MTGO writes under
`%LocalAppData%\Apps\2.0\Data\...\AppFiles\` and turns them into a
queryable personal meta dashboard.

This is the **desktop client** of the broader Metahunter ecosystem.
The shared classifier + log parser lives in
[**metahunter-core**](https://github.com/AFKatta/metahunter-core),
expected to be cloned as a sibling directory.

## Repos in the ecosystem

| Repo | Purpose | Status |
|---|---|---|
| **metahunter-app** (this one) | Desktop client (.exe) | active |
| [metahunter-core](https://github.com/AFKatta/metahunter-core) | Shared classifier + parser library | active |
| metahunter-server | Central FastAPI ingest + read API | upcoming |
| metahunter-web | Public meta site | upcoming |

The two active repos must be cloned as siblings:

```
C:\Code\
├── metahunter-core\
└── metahunter-app\        (this repo)
```

## Layout

```
src/mtgo_meta/                Local-only code (filesystem, SQLite, FastAPI)
    api/        local FastAPI dashboard server
    store/      SQLite persistence
    ingest.py   scan MTGO log dir → DB
    config.py   find ClickOnce AppFiles dirs
    paths.py    user-data dir, corpus paths, frozen vs dev resolution
scripts/                      One-shot dev / diagnostic scripts
web/                          Vite + React + Tailwind dashboard
data/                         Local SQLite + corpus JSON (gitignored)
```

The classifier + parser live in the sibling
[**metahunter-core**](https://github.com/AFKatta/metahunter-core)
repo. See its README for what's in there.

## Dev setup

```powershell
cd C:\Code
git clone https://github.com/AFKatta/metahunter-core.git
git clone https://github.com/AFKatta/metahunter-app.git
cd metahunter-app
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
# Install the sibling shared library editable, then this app editable.
pip install -e ..\metahunter-core
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
