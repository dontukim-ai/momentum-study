"""Return lab: forecast the size and sign of the next move together (docs/blueprint/return-lab-v12b.md).

Fixed before any bar was read. Same grid, features, walk-forward and sealed
v11 holdout as the direction lab (``direction_lab.build``); the target is the
h-minute return in bp instead of its sign, and apart from it its absolute size.

- Models: ZERO (always 0), MEAN (training mean), RIDGE (L2 linear regression,
  alpha 1, on the eight standardised direction-lab features, trained on
  returns clipped at the training 1%/99% quantiles), SIZE (the same regression
  on |return| with size features: last 60 minutes' volatility, absolute signs
  and z-scores, log volume ratio, time of day).
- Scores per model x horizon: Spearman IC pooled and by day (mean, day t,
  one-sided p), top-minus-bottom decile of realised bp by day, out-of-sample
  R^2 against ZERO (SIZE: against the training mean size). Bonferroni over
  the RIDGE cells.

``eval`` walks forward over development days only, ``freeze`` fits on all of
them and writes once, ``score`` grades the frozen models on days from
2026-10-26. Only ``freeze`` writes a file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

TOOLS = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dl = _load("direction_lab")
MODELS = ("ZERO", "MEAN", "RIDGE", "SIZE")
ALPHA, CLIP, MIN_DAY = 1.0, (0.01, 0.99), 30


def collect(data: dict, days: list[str], h: int) -> dict[str, np.ndarray]:
    """Features, 60-minute volatility, return (bp) and day index, in ``direction_lab.stack`` order."""
    xs, vs, rs, ds = [], [], [], []
    for k, day in enumerate(days):
        for s in data[day].values():
            keep = ~np.isnan(s[f"y{h}"])
            xs.append(s["X"][keep])
            vs.append(s["v"][keep])
            rs.append(s[f"r{h}"][keep] * 1e4)
            ds.append(np.full(int(keep.sum()), k))
    if not xs:
        return {"X": np.empty((0, len(dl.FEATURES))), "v": np.empty(0), "r": np.empty(0), "d": np.empty(0)}
    return {"X": np.vstack(xs), "v": np.concatenate(vs), "r": np.concatenate(rs), "d": np.concatenate(ds)}


def size_features(X: np.ndarray, v: np.ndarray) -> np.ndarray:
    return np.c_[v, np.abs(X[:, 0:3]), np.abs(X[:, 3:5]), X[:, 5], X[:, 7]]


def _ridge(F: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    mean, sd = F.mean(axis=0), F.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (F - mean) / sd
    centre = float(y.mean())
    w = np.linalg.solve(Z.T @ Z + ALPHA * np.eye(Z.shape[1]), Z.T @ (y - centre))
    return {"mean": mean, "sd": sd, "w": w, "b": centre}


def _apply(params: dict[str, Any], F: np.ndarray) -> np.ndarray:
    return ((F - params["mean"]) / params["sd"]) @ params["w"] + params["b"]


def fit(model: str, s: dict[str, np.ndarray]) -> Any:
    r = s["r"]
    if model == "ZERO":
        return 0.0
    if model == "MEAN":
        return float(r.mean()) if len(r) else 0.0
    if model == "RIDGE":
        lo, hi = np.quantile(r, CLIP)
        return _ridge(s["X"], np.clip(r, lo, hi))
    params = _ridge(size_features(s["X"], s["v"]), np.abs(r))
    params["base"] = float(np.abs(r).mean())
    return params


def predict(model: str, params: Any, s: dict[str, np.ndarray]) -> np.ndarray:
    if model in ("ZERO", "MEAN"):
        return np.full(len(s["r"]), float(params))
    if model == "RIDGE":
        return _apply(params, s["X"])
    return _apply(params, size_features(s["X"], s["v"]))


def target(model: str, s: dict[str, np.ndarray]) -> np.ndarray:
    return np.abs(s["r"]) if model == "SIZE" else s["r"]


def baseline(model: str, params: Any, n: int) -> np.ndarray:
    return np.full(n, params["base"] if model == "SIZE" else 0.0)


def _t(values: list[float]) -> float:
    sd = np.std(values, ddof=1) if len(values) > 1 else 0.0
    return float(np.mean(values) / (sd / math.sqrt(len(values)))) if sd > 0 else math.nan


def score_cell(pred: np.ndarray, actual: np.ndarray, day: np.ndarray, baseline: np.ndarray) -> dict[str, Any]:
    varied = len(pred) > 1 and np.ptp(pred) > 0 and np.ptp(actual) > 0
    ics, spreads = [], []
    for d in np.unique(day):
        p, a = pred[day == d], actual[day == d]
        if len(p) < MIN_DAY or np.ptp(p) == 0 or np.ptp(a) == 0:
            continue
        ics.append(float(stats.spearmanr(p, a).statistic))
        order = np.argsort(p, kind="stable")
        k = max(1, len(p) // 10)
        spreads.append(float(a[order[-k:]].mean() - a[order[:k]].mean()))
    day_t = _t(ics)
    sse_base = float(np.sum((actual - baseline) ** 2))
    return {"samples": len(pred), "ic": float(stats.spearmanr(pred, actual).statistic) if varied else math.nan,
            "days": len(ics), "day_ic": float(np.mean(ics)) if ics else math.nan, "day_ic_t": day_t,
            "p_day_ic": float(stats.t.sf(day_t, len(ics) - 1)) if not math.isnan(day_t) else math.nan,
            "decile_spread_bp": float(np.mean(spreads)) if spreads else math.nan,
            "decile_spread_t": _t(spreads),
            "r2": 1 - float(np.sum((actual - pred) ** 2)) / sse_base if sse_base > 0 else math.nan}


def _bonferroni(cells: dict) -> float:
    ridge = [c for name, c in cells.items() if name.startswith("RIDGE")]
    bound = 0.05 / len(ridge)
    for name, cell in cells.items():
        cell["bonferroni_significant"] = bool(name.startswith("RIDGE") and cell["p_day_ic"] < bound)
    return bound


def walk_forward(data: dict, initial: int = dl.INITIAL, refit: int = dl.REFIT) -> dict[str, Any]:
    days = list(data)
    if len(days) <= initial:
        raise SystemExit(f"need more than {initial} days, have {len(days)}")
    cells = {}
    for h in dl.HORIZONS:
        parts: dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray]]] = {m: [] for m in MODELS}
        days_all = []
        for start in range(initial, len(days), refit):
            train, test = collect(data, days[:start], h), collect(data, days[start:start + refit], h)
            days_all.append(test["d"] + start)
            for m in MODELS:
                params = fit(m, train)
                parts[m].append((predict(m, params, test), target(m, test), baseline(m, params, len(test["r"]))))
        d = np.concatenate(days_all)
        for m in MODELS:
            pred, actual, base = (np.concatenate([p[j] for p in parts[m]]) for j in range(3))
            cells[f"{m} h{h}"] = score_cell(pred, actual, d, base)
    bound = _bonferroni(cells)
    return {"days": [days[0], days[-1]], "train_initial": initial, "refit_every": refit,
            "test_days": len(days) - initial, "cells": cells, "bonferroni_alpha": bound}


def _to_json(params: Any) -> Any:
    return {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in params.items()} \
        if isinstance(params, dict) else params


def _from_json(raw: Any) -> Any:
    return {k: (np.array(v) if isinstance(v, list) else v) for k, v in raw.items()} if isinstance(raw, dict) else raw


def freeze(data: dict) -> dict:
    out: dict[str, Any] = {"trained": [min(data), max(data)], "models": {}}
    for h in dl.HORIZONS:
        train = collect(data, list(data), h)
        for m in MODELS:
            out["models"][f"{m} h{h}"] = _to_json(fit(m, train))
    return out


def score_frozen(frozen: dict, data: dict) -> dict:
    days = list(data)
    cells = {}
    for h in dl.HORIZONS:
        test = collect(data, days, h)
        for m in MODELS:
            params = _from_json(frozen["models"][f"{m} h{h}"])
            cells[f"{m} h{h}"] = score_cell(predict(m, params, test), target(m, test), test["d"],
                                            baseline(m, params, len(test["r"])))
    bound = _bonferroni(cells)
    return {"days": [days[0], days[-1]], "cells": cells, "bonferroni_alpha": bound}


def _table(cells: dict, alpha: float) -> None:
    print(f"{'cell':<9} {'n':>9} {'IC':>7} {'dayIC':>7} {'t':>6} {'days':>5} {'d10bp':>7} {'t':>6} {'R2':>8}")
    for name, c in cells.items():
        print(f"{name:<9} {c['samples']:>9} {c['ic']:>7.4f} {c['day_ic']:>7.4f} {c['day_ic_t']:>6.2f} {c['days']:>5} "
              f"{c['decile_spread_bp']:>7.2f} {c['decile_spread_t']:>6.2f} {c['r2']:>8.5f}"
              + (" *" if c["bonferroni_significant"] else ""))
    print(f"* RIDGE day-IC p < {alpha:.4f} (Bonferroni over the RIDGE cells). SIZE forecasts |return| and is "
          "reported apart; d10bp is top minus bottom decile of realised bp by day, before costs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("eval", "freeze", "score"))
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--bars", type=Path, default=Path("data/kis/minute-history"))
    parser.add_argument("--model", type=Path, help="freeze: output; score: the frozen model")
    parser.add_argument("--from", dest="first", default=dl.FORWARD_FROM, help="score: first forward day")
    args = parser.parse_args(argv)
    history = dl._history()
    symbols = [r["symbol"] for r in json.loads(args.universe.read_text(encoding="utf-8"))["symbols"]]
    if args.action == "score":
        if args.model is None:
            raise SystemExit("score needs --model")
        result = score_frozen(json.loads(args.model.read_text(encoding="utf-8")),
                              dl.build({s: dl.forward_bars(args.bars, s, args.first) for s in symbols}))
        print(f"forward {result['days'][0]}~{result['days'][1]}, frozen model {args.model}")
        _table(result["cells"], result["bonferroni_alpha"])
        print(json.dumps(result, sort_keys=True, default=float))
        return 0
    data = dl.build({s: history.development_bars(args.bars, s) for s in symbols})
    if max(data) > history.DEVELOPMENT_END.isoformat():
        raise SystemExit("holdout day in development input")
    if args.action == "freeze":
        if args.model is None or args.model.exists():
            raise SystemExit("freeze needs a new --model path (a frozen model is written once)")
        args.model.parent.mkdir(parents=True, exist_ok=True)
        args.model.write_text(json.dumps(freeze(data)) + "\n", encoding="utf-8")
        print(f"frozen {args.model}")
        return 0
    result = walk_forward(data)
    print(f"development {result['days'][0]}~{result['days'][1]}, {len(symbols)} symbols, "
          f"test days {result['test_days']} (train first {dl.INITIAL}, refit every {dl.REFIT})")
    _table(result["cells"], result["bonferroni_alpha"])
    print(json.dumps(result, sort_keys=True, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
