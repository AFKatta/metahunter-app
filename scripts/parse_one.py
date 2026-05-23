"""Parse a single Match_GameLog_*.dat file and pretty-print the result.

Usage:
    python scripts/parse_one.py <path>
    python scripts/parse_one.py            # picks a recent one automatically
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make `src/` importable when running this script straight from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtgo_meta.config import find_mtgo_appfiles_dir
from metahunter_core.parser import parse_game_log


def pick_default() -> Path | None:
    folder = find_mtgo_appfiles_dir()
    if not folder:
        return None
    logs = sorted(
        folder.glob("Match_GameLog_*.dat"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return logs[0] if logs else None


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = pick_default()
        if path is None:
            print("Could not locate MTGO AppFiles folder.", file=sys.stderr)
            return 1
        print(f"[auto-picked largest log] {path.name}")

    pm = parse_game_log(path)
    out = pm.model_dump()
    # Trim the huge multiset for display; show counts instead.
    from collections import Counter
    out["cards_by_player_counts"] = {
        p: Counter(cs).most_common()
        for p, cs in pm.cards_by_player.items()
    }
    out.pop("cards_by_player", None)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
