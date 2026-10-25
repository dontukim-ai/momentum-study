"""Cut a research input so every validation window lies on or after a date.

A study's first holdout starts at calendar index ``warmup + development +
embargo`` (``ResearchAgent._plan``), and each later cycle moves forward. To
test on sessions no earlier study has seen (explore v5 §6.4, 2026-10-25), the
input starts exactly that many sessions before the first unseen session:

    uv run python tools/forward_input.py --input data/krx/research-fwd-full \
        --policy artifacts/<run>/policy.json --first-holdout 2024-07-01 \
        --out data/krx/research-fwd

Development and warmup still use earlier (already seen) sessions; only
holdouts move. Prices and sessions before the cut are dropped. Actions are
kept from the cut on: nothing is held before it, because every evaluation
window starts from cash at or after the cut. Security records are copied
whole, since event-sourced states set before the cut still hold after it.
Writes report.json with the cut, counts and input hashes. Outputs stay in
data/krx/ (not redistributed, kept out of Git).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

from momentum.agent.contracts import policy_from_dict
from momentum.config import SpecUndefinedError

FILES = ("prices.csv", "securities.jsonl", "actions.json", "sessions.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cut_session(sessions: list[date], offset: int, first_holdout: date) -> tuple[date, date]:
    """Return (first kept session, first holdout session)."""
    index = next((i for i, d in enumerate(sessions) if d >= first_holdout), None)
    if index is None:
        raise SpecUndefinedError(f"no session on or after {first_holdout}")
    if index < offset:
        raise SpecUndefinedError(
            f"only {index} sessions before {sessions[index]}; {offset} are needed"
        )
    return sessions[index - offset], sessions[index]


def forward_input(source: Path, policy_path: Path, first_holdout: date, out: Path) -> dict:
    policy = policy_from_dict(json.loads(policy_path.read_text(encoding="utf-8")))
    offset = policy.warmup_sessions + policy.development_sessions + policy.embargo_sessions
    sessions = [date.fromisoformat(s) for s in json.loads((source / "sessions.json").read_text())]
    start, holdout = cut_session(sessions, offset, first_holdout)
    out.mkdir(parents=True, exist_ok=True)
    kept_sessions = [d.isoformat() for d in sessions if d >= start]
    (out / "sessions.json").write_text(json.dumps(kept_sessions) + "\n", encoding="utf-8")
    rows = kept = 0
    with (source / "prices.csv").open(newline="", encoding="utf-8") as src, (
        out / "prices.csv"
    ).open("w", newline="", encoding="utf-8") as dst:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=reader.fieldnames or [])
        writer.writeheader()
        for row in reader:
            rows += 1
            if date.fromisoformat(row["session"]) >= start:
                writer.writerow(row)
                kept += 1
    actions = json.loads((source / "actions.json").read_text(encoding="utf-8"))
    kept_actions = [a for a in actions if date.fromisoformat(a["ex_date"]) >= start]
    (out / "actions.json").write_text(
        json.dumps(kept_actions, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    shutil.copyfile(source / "securities.jsonl", out / "securities.jsonl")
    report = {
        "first_kept_session": start.isoformat(),
        "first_holdout_session": holdout.isoformat(),
        "offset_sessions": offset,
        "sessions": [len(sessions), len(kept_sessions)],
        "price_rows": [rows, kept],
        "actions": [len(actions), len(kept_actions)],
        "source_sha256": {name: _sha256(source / name) for name in FILES},
        "output_sha256": {name: _sha256(out / name) for name in FILES},
    }
    (out / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--first-holdout", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.out.resolve() == args.input.resolve():
        raise SystemExit("--out must differ from --input")
    try:
        report = forward_input(args.input, args.policy, args.first_holdout, args.out)
    except SpecUndefinedError as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps({k: v for k, v in report.items() if "sha256" not in k}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
