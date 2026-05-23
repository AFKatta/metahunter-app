"""CLI wrapper around mtgo_meta.ingest.ingest_all.

Usage:
    python scripts/ingest.py                  # incremental
    python scripts/ingest.py --db custom.sqlite
    python scripts/ingest.py --full           # reparse every log
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtgo_meta.ingest import ingest_all
from mtgo_meta.paths import default_db_path


def main() -> int:
    args = sys.argv[1:]
    db = default_db_path()
    full = False
    while args:
        a = args.pop(0)
        if a == "--db" and args:
            db = Path(args.pop(0))
        elif a == "--full":
            full = True
        else:
            print(f"Unknown arg: {a}", file=sys.stderr)
            return 2
    ingest_all(db_path=db, full=full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
