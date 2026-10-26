"""Write listed-share runs from saved FSC stock quotes (explore v7 §3).

Example:
  uv run python tools/fsc_shares.py --root data/krx/raw/fsc \
    --sessions data/krx/research-fwd-full/sessions.json --out data/krx/research-v7/shares.csv

Every session in --sessions must have saved quotes. The CSV feeds
``momentum-agent --shares``. Keep the output out of Git (no redistribution).
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from momentum.config import SpecUndefinedError
from momentum.krx.fsc_shares import share_runs, write_runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    sessions = [date.fromisoformat(d) for d in json.loads(args.sessions.read_text())]
    try:
        runs, report = share_runs(args.root, sessions)
    except (SpecUndefinedError, OSError, ValueError, KeyError) as exc:
        print(f"fsc-shares: {type(exc).__name__}: {exc}")
        return 1
    write_runs(runs, args.out)
    print(f"fsc-shares: {report['sessions']} sessions, {report['rows']} rows, "
          f"{report['missing_count_rows']} rows without a count, {report['runs']} runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
