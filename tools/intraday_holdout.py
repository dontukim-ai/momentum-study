"""Freeze the holdout candidates and run the holdout once (intraday-dev-plan-v11 §4, §5).

Freeze (cloud, committed as a PR before any holdout bar is read):

    uv run python tools/intraday_holdout.py freeze --select "C1 ..." "C5-A ..." \
        --universe data/kis/etf-universe-v2.json --out config/intraday-v11/manifest.json

Run (Mac, once):

    uv run python tools/intraday_holdout.py run --manifest config/intraday-v11/manifest.json \
        --bars data/kis/minute-history --record data/kis/intraday-holdout-run.json

The run reads holdout bars only through a manifest whose code hashes match
the files now on disk, and refuses a second run (the record file). The last
WARMUP development days go in first so candidates have their history, but
no candidate trades before HOLDOUT_START and only holdout days are scored.
Verdict per candidate, fixed in §4: net > 0, t >= 1.5 and max drawdown no
worse than holding 069500 at 95% over the same days.

Report only (added before the freeze, after the development run, never
judged): an overnight candidate (C5-A) also runs with the same weights held
open-to-close only and held all day, so the overnight gain can be told apart
from simply holding a rising market.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
import intraday_dev as dev  # tools are loaded as plain modules

engine, candidates, history = dev.engine, dev.candidates, dev.history
HOLDOUT_START, HOLDOUT_END = history.HOLDOUT_START, date(2026, 10, 23)
WARMUP, MAX_PICKS = 21, 5
CODE = ("tools/intraday_engine.py", "tools/intraday_candidates.py", "tools/intraday_holdout.py")
CRITERIA = {"net_return": 0.0, "t": 1.5, "mdd": "<= 069500 95% hold"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(selected: list[str], universe: tuple[str, ...], out: Path, frozen_at: str | None = None) -> dict:
    names = {c.name: c.candidate for c in candidates.configs(universe)}
    if not 1 <= len(selected) <= MAX_PICKS or len(set(selected)) != len(selected):
        raise SpecUndefinedError(f"select 1..{MAX_PICKS} distinct combinations")
    unknown = [s for s in selected if s not in names]
    if unknown:
        raise SpecUndefinedError(f"unknown combinations: {unknown}")
    if len({names[s] for s in selected}) != len(selected):
        raise SpecUndefinedError("at most one combination per candidate (§4)")
    if out.exists():
        raise SpecUndefinedError(f"{out} exists; a manifest is frozen once")
    manifest = {"selected": selected, "universe": list(universe), "frozen_at": frozen_at,
                "holdout": [HOLDOUT_START.isoformat(), HOLDOUT_END.isoformat()], "warmup_days": WARMUP,
                "criteria": {"net_return_above": 0.0, "t": 1.5, "max_drawdown": "<= 069500 95% hold"},
                "code": {name: _sha(ROOT / name) for name in CODE}}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def verified(path: Path) -> dict:
    if not path.exists():
        raise SpecUndefinedError(f"no frozen manifest at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    changed = [name for name, digest in manifest["code"].items() if _sha(ROOT / name) != digest]
    if changed:
        raise SpecUndefinedError(f"code changed since the freeze: {changed}")
    return manifest


def _bars(root: Path, symbol: str, first: str, last: str) -> dict[str, list[dict]]:
    out = {}
    for path in sorted((root / symbol).glob("*.json")):
        if first <= path.stem <= last:
            bars = json.loads(path.read_text(encoding="utf-8"))["bars"]
            if bars:
                out[path.stem] = bars
    return out


class Gated(candidates.Base):
    """Flat before the holdout; the candidate itself from the first holdout day."""

    def __init__(self, inner: Any):
        self.inner = inner

    def _live(self, view: Any) -> bool:
        return view.day >= HOLDOUT_START.isoformat()

    def open_auction(self, view: Any) -> Any:
        return self.inner.open_auction(view) if self._live(view) else {}

    def on_minute(self, view: Any) -> Any:
        decided = self.inner.on_minute(view)
        return decided if self._live(view) else None

    def close_auction(self, view: Any) -> Any:
        return self.inner.close_auction(view) if self._live(view) else {}


class IntradayOnly(candidates.Base):
    """Report only: the candidate's weights bought at the open auction and sold at the close auction."""

    def __init__(self, weights: dict[str, float]):
        self.weights = weights

    def open_auction(self, view: Any) -> Any:
        return self.weights


class FullDay(IntradayOnly):
    """Report only: the candidate's weights held through the day and the night."""

    def close_auction(self, view: Any) -> Any:
        return self.weights


def comparisons(strategy: Any) -> dict[str, Any]:
    if not isinstance(strategy, candidates.Overnight):
        return {}
    return {"intraday_only": IntradayOnly(strategy.weights), "full_day": FullDay(strategy.weights)}


def _holdout_metrics(days: Any, strategy: Any) -> tuple[dict, list]:
    result = engine.run(days, Gated(strategy))
    result.daily = [d for d in result.daily if d["day"] >= HOLDOUT_START.isoformat()]
    return dev.metrics(result), result.trades


def judge(row: dict, etf_mdd: float) -> dict:
    checks = {"net_positive": row["net_return"] > 0, "t_at_least_1_5": row["t"] >= 1.5,
              "mdd_within_etf": row["max_drawdown"] <= etf_mdd}
    return {"checks": checks, "passed": all(checks.values())}


def run(manifest_path: Path, bars_root: Path, record: Path) -> dict:
    manifest = verified(manifest_path)
    if record.exists():
        raise SpecUndefinedError(f"the holdout already ran ({record})")
    universe = tuple(manifest["universe"])
    dev_days = sorted({d for s in universe for d in history.development_bars(bars_root, s)})
    first = dev_days[-WARMUP] if len(dev_days) >= WARMUP else dev_days[0]
    raw = {s: _bars(bars_root, s, first, HOLDOUT_END.isoformat()) for s in universe}
    days = engine.days_from_kis(raw)
    hold = [d for d in days if d >= HOLDOUT_START.isoformat()]
    if not hold:
        raise SpecUndefinedError("no holdout days in the input")
    held_days = {d: days[d] for d in hold}
    etf = dev.buy_and_hold(held_days, candidates.KOSPI)
    configs = {c.name: c for c in candidates.configs(universe)}
    rows, trades = [], {}
    for name in manifest["selected"]:
        strategy = configs[name].make()
        measured, trades[name] = _holdout_metrics(days, strategy)
        row = {"name": name, **measured}
        row.update(judge(row, etf["max_drawdown"]))
        compare = {label: _holdout_metrics(days, other)[0] for label, other in comparisons(strategy).items()}
        if compare:
            row["report_only"] = compare
        rows.append(row)
    output = {"manifest_sha256": _sha(manifest_path), "days": [hold[0], hold[-1]], "rows": rows,
              "benchmark_069500_95": etf, "trades": trades}
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({k: v for k, v in output.items() if k != "trades"}, default=str, indent=1) + "\n",
                      encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--select", nargs="+", required=True)
    f.add_argument("--universe", type=Path, required=True)
    f.add_argument("--out", type=Path, required=True)
    f.add_argument("--frozen-at", required=True)
    r = sub.add_parser("run")
    r.add_argument("--manifest", type=Path, required=True)
    r.add_argument("--bars", type=Path, default=Path("data/kis/minute-history"))
    r.add_argument("--record", type=Path, default=Path("data/kis/intraday-holdout-run.json"))
    args = parser.parse_args(argv)
    try:
        if args.action == "freeze":
            universe = tuple(r["symbol"] for r in json.loads(args.universe.read_text(encoding="utf-8"))["symbols"])
            print(json.dumps(freeze(args.select, universe, args.out, args.frozen_at), ensure_ascii=False))
            return 0
        result = run(args.manifest, args.bars, args.record)
    except SpecUndefinedError as exc:
        print(f"intraday-holdout: STOP: {exc}")
        return 1
    bench = result["benchmark_069500_95"]
    print(f"holdout {result['days'][0]}~{result['days'][1]}; 069500 95%: net {bench['net_return']:+.2%} "
          f"MDD {bench['max_drawdown']:.2%}")
    for row in result["rows"]:
        print(f"{row['name']:<28} net {row['net_return']:+.2%} t {row['t']:.2f} MDD {row['max_drawdown']:.2%} "
              f"days {row['days']} -> {'PASS' if row['passed'] else 'FAIL'} {row['checks']}")
        for label, c in row.get("report_only", {}).items():
            print(f"  report only {label:<14} net {c['net_return']:+.2%} t {c['t']:.2f} MDD {c['max_drawdown']:.2%}")
    print(json.dumps({k: v for k, v in result.items() if k != "trades"}, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
