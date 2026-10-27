"""v13 trial ledger, stage ladder and learning curve (docs/blueprint/intraday-learning-loop-v13.md §3, §6).

    uv run python tools/learning_ledger.py report
    uv run python tools/learning_ledger.py check --against <older ledger.json>

The ledger (config/learning-v13/ledger.json) holds every combination or model
cell the intraday research has evaluated, whether it failed or not, so the
development gate can rise with the number of trials. Trials only move one
rung at a time (S0 -> S1 -> S2, or to closed from any open rung) and never
come back from closed. Strategies take at most 5 S1 and 3 S2 seats;
forecast cells (v12, v12b) are scored apart and take none. What a trial
was registered with (study, family, name, kind, development window, dev
metrics, cost version) never changes; a dev metric registered as null
because its run had not finished may be filled once. Each monthly cycle appends one
learning-curve row: out-of-sample mean, shrinkage against the development
mean, and the share of trials that left S1 upward.
"""

from __future__ import annotations

import argparse
import json
import statistics
from itertools import pairwise
from pathlib import Path
from statistics import NormalDist
from typing import Any

from momentum.config import SpecUndefinedError

LEDGER = Path(__file__).resolve().parents[1] / "config/learning-v13/ledger.json"
STAGES = ("S0", "S1", "S2", "closed")
NEXT = {"S0": ("S1", "closed"), "S1": ("S2", "closed"), "S2": ("closed",), "closed": ()}
SEATS = {"S1": 5, "S2": 3}
KINDS = ("strategy", "forecast")
FROZEN = ("study", "family", "name", "kind", "registered", "development", "dev", "cost_version")


def threshold(trials: int, alpha: float = 0.05) -> float:
    """Development t a strategy needs for S1: Bonferroni over every trial in the ledger, never below 2."""
    return max(2.0, NormalDist().inv_cdf(1 - alpha / max(trials, 1)))


def empty() -> dict[str, Any]:
    return {"version": 1, "trials": [], "curve": []}


def _find(book: dict, trial_id: str) -> dict:
    for t in book["trials"]:
        if t["id"] == trial_id:
            return t
    raise SpecUndefinedError(f"no trial {trial_id}")


def register(book: dict, entries: list[dict]) -> None:
    known = {t["id"] for t in book["trials"]}
    for entry in entries:
        if entry["id"] in known:
            raise SpecUndefinedError(f"{entry['id']} is already in the ledger")
        if entry["kind"] not in KINDS:
            raise SpecUndefinedError(f"unknown kind {entry['kind']}")
        known.add(entry["id"])
        book["trials"].append({**entry, "stage": "S0",
                               "history": [{"stage": "S0", "date": entry["registered"], "reason": "registered"}]})


def advance(book: dict, trial_id: str, stage: str, date: str, reason: str) -> None:
    t = _find(book, trial_id)
    if stage not in NEXT[t["stage"]]:
        raise SpecUndefinedError(f"{trial_id}: {t['stage']} -> {stage} is not a ladder step"
                                 + (" (closed trials stay closed)" if t["stage"] == "closed" else ""))
    if t["kind"] == "strategy" and stage in SEATS:
        taken = sum(o["kind"] == "strategy" and o["stage"] == stage for o in book["trials"])
        if taken >= SEATS[stage]:
            raise SpecUndefinedError(f"{stage} is full ({SEATS[stage]} strategies)")
        if stage == "S1" and t["dev"]["t"] < threshold(len(book["trials"])):
            raise SpecUndefinedError(f"{trial_id}: dev t {t['dev']['t']:.2f} is below the ledger threshold "
                                     f"{threshold(len(book['trials'])):.2f} ({len(book['trials'])} trials)")
    t["stage"] = stage
    t["history"].append({"stage": stage, "date": date, "reason": reason})


def validate(book: dict) -> None:
    ids = [t["id"] for t in book["trials"]]
    if len(set(ids)) != len(ids):
        raise SpecUndefinedError("duplicate trial ids")
    for t in book["trials"]:
        if t["stage"] not in STAGES or t["kind"] not in KINDS:
            raise SpecUndefinedError(f"{t['id']}: bad stage or kind")
        path = [h["stage"] for h in t["history"]]
        if path[0] != "S0" or path[-1] != t["stage"] or any(b not in NEXT[a] for a, b in pairwise(path)):
            raise SpecUndefinedError(f"{t['id']}: history {path} is not a ladder path")
    for stage, seats in SEATS.items():
        if sum(t["kind"] == "strategy" and t["stage"] == stage for t in book["trials"]) > seats:
            raise SpecUndefinedError(f"{stage} holds more than {seats} strategies")
    months = [row["month"] for row in book["curve"]]
    if len(set(months)) != len(months):
        raise SpecUndefinedError("a month appears twice in the learning curve")


def _same(old: Any, new: Any) -> bool:
    """Equal, except that a dev metric still null at registration (results pending) may be filled once."""
    if isinstance(old, dict) and isinstance(new, dict):
        return old.keys() == new.keys() and all(v is None or new[k] == v for k, v in old.items())
    return old == new


def check_append_only(old: dict, new: dict) -> None:
    """Trials and curve rows may be added and trials may move up the ladder; nothing else changes."""
    validate(new)
    after = {t["id"]: t for t in new["trials"]}
    for t in old["trials"]:
        if t["id"] not in after:
            raise SpecUndefinedError(f"{t['id']} was removed")
        changed = [k for k in FROZEN if not _same(t.get(k), after[t["id"]].get(k))]
        if changed or after[t["id"]]["history"][:len(t["history"])] != t["history"]:
            raise SpecUndefinedError(f"{t['id']}: frozen fields changed {changed or ['history']}")
    if new["curve"][:len(old["curve"])] != old["curve"]:
        raise SpecUndefinedError("recorded learning-curve rows changed")


def _left_s1(t: dict) -> str | None:
    path = [h["stage"] for h in t["history"]]
    if "S1" not in path or path[-1] == "S1":
        return None
    return path[path.index("S1") + 1]


def cycle(book: dict, month: str, results: dict[str, dict]) -> dict:
    """Append one learning-curve row from this month's out-of-sample results of S1/S2 trials."""
    if any(row["month"] == month for row in book["curve"]):
        raise SpecUndefinedError(f"{month} is already recorded")
    rows = []
    for trial_id, oos in results.items():
        t = _find(book, trial_id)
        if t["stage"] not in ("S1", "S2"):
            raise SpecUndefinedError(f"{trial_id} is not in S1/S2")
        dev = t["dev"].get("mean_daily")
        rows.append({"id": trial_id, **oos, "shrinkage": oos["mean_daily"] / dev if dev else None})
    shrink = [r["shrinkage"] for r in rows if r["shrinkage"] is not None]
    left = [s for s in (_left_s1(t) for t in book["trials"]) if s is not None]
    row = {"month": month, "candidates": len(rows),
           "oos_mean_daily_bp": statistics.fmean(r["mean_daily"] for r in rows) * 1e4 if rows else None,
           "shrinkage_median": statistics.median(shrink) if shrink else None,
           "promotion_hit_rate": left.count("S2") / len(left) if left else None,
           "trials": len(book["trials"]), "threshold": threshold(len(book["trials"])), "rows": rows}
    book["curve"].append(row)
    return row


def load(path: Path = LEDGER) -> dict:
    book = json.loads(path.read_text(encoding="utf-8"))
    validate(book)
    return book


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("report", "check"))
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    parser.add_argument("--against", type=Path, help="check: the earlier ledger this one must extend")
    args = parser.parse_args(argv)
    try:
        book = load(args.ledger)
        if args.action == "check":
            if args.against is not None:
                check_append_only(json.loads(args.against.read_text(encoding="utf-8")), book)
            print("ledger ok")
            return 0
    except SpecUndefinedError as exc:
        print(f"learning-ledger: STOP: {exc}")
        return 1
    trials = book["trials"]
    print(f"trials {len(trials)}; development gate t >= {threshold(len(trials)):.2f}")
    for study in sorted({t["study"] for t in trials}):
        mine = [t for t in trials if t["study"] == study]
        stages = {s: sum(t["stage"] == s for t in mine) for s in STAGES}
        print(f"  {study:<6} {len(mine):>3}  " + "  ".join(f"{s} {n}" for s, n in stages.items()))
    for t in trials:
        if t["stage"] in ("S1", "S2"):
            print(f"  {t['stage']} {t['id']} ({t['kind']}) since {t['history'][-1]['date']}")
    for row in book["curve"]:
        print(f"{row['month']}: {row['candidates']} live, OOS {row['oos_mean_daily_bp']} bp/day, "
              f"shrinkage {row['shrinkage_median']}, promotion hit {row['promotion_hit_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
