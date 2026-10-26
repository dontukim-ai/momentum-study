"""Direction lab: can minute data predict the sign of the next move? (statistics only, no trading)

Fixed before any bar was read (docs/blueprint/direction-lab-v12.md):

- Grid: one point a minute 09:00..15:19 per symbol-day. The price at a minute
  is the last trade at or before it (a minute without a trade keeps the last
  trade price and volume 0; nothing is invented before the first trade).
- Target at minute i: sign of price[i+h] / price[i] - 1 for h in {1, 5, 30},
  within the day and before 15:20. Up, flat or down; accuracy counts up/down
  outcomes only and the flat share is reported apart.
- Features at i use minutes <= i only (needs i >= 60): signs of the last three
  one-minute returns, the 5-minute return and the gap to VWAP in units of the
  last 60 minutes' volatility, log volume vs the last 20 minutes, the sign of
  069500's last one-minute return, time of day.
- Models: BASE (majority direction of the training data), MARKOV1..3 (up
  frequency after the last m signs, Laplace smoothed), LOGIT (L2 logistic
  regression on the features, pooled over symbols).
- Walk-forward: train on the first 40 days, predict the next 20, refit on all
  days so far, and so on. Scores: accuracy with a Wilson interval, binomial
  p vs 0.5, a day-clustered t (predictions of one day are not independent),
  Brier score, log loss, calibration by predicted-probability decile, and a
  Bonferroni bound over every model x horizon cell.

``eval`` walks forward over development days only (``development_bars``).
``freeze`` fits every model on all development days and writes it once;
``score`` grades that frozen model on days from 2026-10-26 (after the v11
holdout, which stays sealed). Only ``freeze`` writes a file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from scipy import optimize, stats

TOOLS = Path(__file__).resolve().parent
OPEN, LAST = 9 * 60, 15 * 60 + 19          # minutes since midnight, continuous session
GRID = LAST - OPEN + 1                       # 380 points
WARM, HORIZONS = 60, (1, 5, 30)
INITIAL, REFIT = 40, 20
MARKET = "069500"
FEATURES = ("s1", "s2", "s3", "r5z", "vwapz", "volz", "mkt1", "tod")
MODELS = ("BASE", "MARKOV1", "MARKOV2", "MARKOV3", "LOGIT")


def grid(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Last-trade price and volume per minute 09:00..15:19 (NaN before the first trade)."""
    price, volume = np.full(GRID, np.nan), np.zeros(GRID)
    for r in sorted(rows, key=lambda r: r["stck_cntg_hour"]):
        t = r["stck_cntg_hour"]
        i = int(t[:2]) * 60 + int(t[2:4]) - OPEN
        if 0 <= i < GRID:
            price[i], volume[i] = float(r["stck_prpr"]), float(r["cntg_vol"])
    last = np.nan
    for i in range(GRID):
        if np.isnan(price[i]):
            price[i] = last
        last = price[i]
    return price, volume


def day_samples(price: np.ndarray, volume: np.ndarray, market: np.ndarray | None) -> dict[str, np.ndarray]:
    """Feature rows and targets (+1/0/-1 per horizon) for one symbol-day."""
    with np.errstate(invalid="ignore", divide="ignore"):
        r1 = np.r_[np.nan, price[1:] / price[:-1] - 1]
        mr1 = np.r_[np.nan, market[1:] / market[:-1] - 1] if market is not None else np.full(GRID, np.nan)
    rows, targets = [], {h: [] for h in HORIZONS}
    cum_pv, cum_v = np.nancumsum(np.nan_to_num(price) * volume), np.cumsum(volume)
    for i in range(WARM, GRID - 1):
        window = r1[i - 59:i + 1]
        if np.isnan(price[i - WARM]) or np.isnan(window).any():
            continue
        sd = float(np.std(window))
        scale = sd if sd > 0 else 1.0
        vwap = cum_pv[i] / cum_v[i] if cum_v[i] > 0 else price[i]
        past = volume[i - 20:i].mean()
        row = [np.sign(r1[i]), np.sign(r1[i - 1]), np.sign(r1[i - 2]),
               np.clip((price[i] / price[i - 5] - 1) / (scale * math.sqrt(5)), -5, 5),
               np.clip((price[i] / vwap - 1) / scale, -5, 5),
               math.log((volume[i] + 1) / (past + 1)),
               0.0 if np.isnan(mr1[i]) else np.sign(mr1[i]),
               i / GRID]
        rows.append(row)
        for h in HORIZONS:
            targets[h].append(np.sign(price[i + h] / price[i] - 1) if i + h < GRID else np.nan)
    out = {"X": np.array(rows, dtype=float).reshape(-1, len(FEATURES))}
    for h in HORIZONS:
        out[f"y{h}"] = np.array(targets[h], dtype=float)
    return out


def build(bars: dict[str, dict[str, list[dict]]]) -> dict[str, dict[str, dict[str, np.ndarray]]]:
    """{day: {symbol: samples}} for every symbol-day with trades."""
    grids = {s: {d: grid(rows) for d, rows in days.items() if rows} for s, days in bars.items()}
    out: dict[str, dict[str, dict[str, np.ndarray]]] = defaultdict(dict)
    for symbol, days in grids.items():
        for day, (price, volume) in days.items():
            market = grids.get(MARKET, {}).get(day, (None, None))[0]
            samples = day_samples(price, volume, market)
            if len(samples["X"]):
                out[day][symbol] = samples
    return dict(sorted(out.items()))


def stack(data: dict, days: list[str], h: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xs, ys, ds = [], [], []
    for k, day in enumerate(days):
        for samples in data[day].values():
            keep = ~np.isnan(samples[f"y{h}"])
            xs.append(samples["X"][keep])
            ys.append(samples[f"y{h}"][keep])
            ds.append(np.full(int(keep.sum()), k))
    if not xs:
        return np.empty((0, len(FEATURES))), np.empty(0), np.empty(0)
    return np.vstack(xs), np.concatenate(ys), np.concatenate(ds)


def fit(model: str, X: np.ndarray, y: np.ndarray) -> Any:
    moving = y != 0
    X, up = X[moving], (y[moving] > 0).astype(float)
    if model == "BASE":
        return float(up.mean()) if len(up) else 0.5
    if model.startswith("MARKOV"):
        m = int(model[-1])
        counts: dict[tuple, list[float]] = defaultdict(lambda: [1.0, 1.0])  # Laplace: [ups+1, downs+1]
        for state, u in zip(map(tuple, X[:, :m]), up, strict=True):
            counts[state][0 if u else 1] += 1
        return {k: v[0] / (v[0] + v[1]) for k, v in counts.items()}
    mean, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = np.c_[np.ones(len(X)), (X - mean) / sd]

    def loss(w: np.ndarray) -> tuple[float, np.ndarray]:
        z = Z @ w
        p = 1 / (1 + np.exp(-z))
        eps = 1e-12
        value = -np.mean(up * np.log(p + eps) + (1 - up) * np.log(1 - p + eps)) + 1e-3 * np.sum(w[1:] ** 2)
        grad = Z.T @ (p - up) / len(up) + 2e-3 * np.r_[0.0, w[1:]]
        return float(value), grad

    w = optimize.minimize(loss, np.zeros(Z.shape[1]), jac=True, method="L-BFGS-B").x
    return {"mean": mean, "sd": sd, "w": w}


def predict(model: str, params: Any, X: np.ndarray) -> np.ndarray:
    if model == "BASE":
        return np.full(len(X), params)
    if model.startswith("MARKOV"):
        m = int(model[-1])
        return np.array([params.get(tuple(row), 0.5) for row in X[:, :m]])
    Z = np.c_[np.ones(len(X)), (X - params["mean"]) / params["sd"]]
    return 1 / (1 + np.exp(-(Z @ params["w"])))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (math.nan, math.nan)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (centre - half, centre + half)


def score(p: np.ndarray, y: np.ndarray, day: np.ndarray) -> dict[str, Any]:
    """Direction calls on up/down outcomes; abstain when p == 0.5."""
    flat_share = float(np.mean(y == 0)) if len(y) else math.nan
    moving = y != 0
    p, up, day = p[moving], (y[moving] > 0), day[moving]
    called = p != 0.5
    hit = (p[called] > 0.5) == up[called]
    k, n = int(hit.sum()), int(called.sum())
    per_day = [float(hit[day[called] == d].mean()) for d in np.unique(day[called])]
    day_t = (float((np.mean(per_day) - 0.5) / (np.std(per_day, ddof=1) / math.sqrt(len(per_day))))
             if len(per_day) > 1 and np.std(per_day, ddof=1) > 0 else math.nan)
    eps = 1e-12
    deciles = []
    for lo in np.arange(0, 1, 0.1):
        sel = (p >= lo) & (p < lo + 0.1)
        if sel.any():
            deciles.append([round(float(lo), 1), int(sel.sum()), float(p[sel].mean()), float(up[sel].mean())])
    return {
        "samples": int(moving.size), "flat_share": flat_share, "moving": len(up), "called": n,
        "coverage": n / len(up) if len(up) else math.nan,
        "accuracy": k / n if n else math.nan, "ci95": wilson(k, n),
        "p_binomial": float(stats.binomtest(k, n, 0.5, alternative="greater").pvalue) if n else math.nan,
        "days": len(per_day), "day_t": day_t,
        "brier": float(np.mean((p - up) ** 2)) if len(up) else math.nan,
        "log_loss": float(-np.mean(up * np.log(p + eps) + (~up) * np.log(1 - p + eps))) if len(up) else math.nan,
        "calibration": deciles,
    }


def walk_forward(data: dict, initial: int = INITIAL, refit: int = REFIT) -> dict[str, Any]:
    days = list(data)
    if len(days) <= initial:
        raise SystemExit(f"need more than {initial} days, have {len(days)}")
    cells = {}
    for h in HORIZONS:
        preds: dict[str, list[np.ndarray]] = {m: [] for m in MODELS}
        ys, ds = [], []
        for start in range(initial, len(days), refit):
            X, y, _ = stack(data, days[:start], h)
            Xt, yt, dt = stack(data, days[start:start + refit], h)
            ys.append(yt)
            ds.append(dt + start)
            for m in MODELS:
                preds[m].append(predict(m, fit(m, X, y), Xt))
        y_all, d_all = np.concatenate(ys), np.concatenate(ds)
        for m in MODELS:
            cells[f"{m} h{h}"] = score(np.concatenate(preds[m]), y_all, d_all)
    bound = 0.05 / len(cells)
    for cell in cells.values():
        cell["bonferroni_significant"] = bool(cell["p_binomial"] < bound)
    return {"days": [days[0], days[-1]], "train_initial": initial, "refit_every": refit,
            "test_days": len(days) - initial, "cells": cells, "bonferroni_alpha": bound}


FORWARD_FROM = "2026-10-26"  # after the v11 holdout (2026-07-01..10-23), which stays sealed


def to_json(model: str, params: Any) -> Any:
    if model == "BASE":
        return params
    if model.startswith("MARKOV"):
        return {",".join(str(int(v)) for v in k): p for k, p in params.items()}
    return {k: v.tolist() for k, v in params.items()}


def from_json(model: str, raw: Any) -> Any:
    if model == "BASE":
        return raw
    if model.startswith("MARKOV"):
        return {tuple(float(v) for v in k.split(",")): p for k, p in raw.items()}
    return {k: np.array(v) for k, v in raw.items()}


def freeze(data: dict) -> dict:
    """Every model fitted on all development days, per horizon."""
    out: dict[str, Any] = {"trained": [min(data), max(data)], "models": {}}
    for h in HORIZONS:
        X, y, _ = stack(data, list(data), h)
        for m in MODELS:
            out["models"][f"{m} h{h}"] = to_json(m, fit(m, X, y))
    return out


def score_frozen(frozen: dict, data: dict) -> dict:
    days = list(data)
    cells = {}
    for h in HORIZONS:
        X, y, d = stack(data, days, h)
        for m in MODELS:
            cells[f"{m} h{h}"] = score(predict(m, from_json(m, frozen["models"][f"{m} h{h}"]), X), y, d)
    bound = 0.05 / len(cells)
    for cell in cells.values():
        cell["bonferroni_significant"] = bool(cell["p_binomial"] < bound)
    return {"days": [days[0], days[-1]], "cells": cells, "bonferroni_alpha": bound}


def forward_bars(root: Path, symbol: str, first: str) -> dict[str, list[dict]]:
    if first < FORWARD_FROM:
        raise SystemExit(f"forward scoring starts {FORWARD_FROM}; the v11 holdout stays sealed")
    out = {}
    for path in sorted((root / symbol).glob("*.json")):
        if path.stem >= first:
            bars = json.loads(path.read_text(encoding="utf-8"))["bars"]
            if bars:
                out[path.stem] = bars
    return out


def _history() -> Any:
    spec = importlib.util.spec_from_file_location("kis_minute_history", TOOLS / "kis_minute_history.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules["kis_minute_history"] = module
    spec.loader.exec_module(module)
    return module


def _table(cells: dict, alpha: float) -> None:
    print(f"{'cell':<12} {'flat':>5} {'n':>9} {'acc':>7} {'95% CI':>17} {'p':>8} {'day_t':>6} {'brier':>6}")
    for name, c in cells.items():
        lo, hi = c["ci95"]
        print(f"{name:<12} {c['flat_share']:>5.0%} {c['called']:>9} {c['accuracy']:>7.2%} "
              f"[{lo:.2%},{hi:.2%}] {c['p_binomial']:>8.1e} {c['day_t']:>6.2f} {c['brier']:>6.4f}"
              + (" *" if c["bonferroni_significant"] else ""))
    print(f"* p < {alpha:.4f} (Bonferroni over {len(cells)} cells); day_t treats each test day as one observation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("eval", "freeze", "score"))
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--bars", type=Path, default=Path("data/kis/minute-history"))
    parser.add_argument("--model", type=Path, help="freeze: output; score: the frozen model")
    parser.add_argument("--from", dest="first", default=FORWARD_FROM, help="score: first forward day")
    args = parser.parse_args(argv)
    history = _history()
    symbols = [r["symbol"] for r in json.loads(args.universe.read_text(encoding="utf-8"))["symbols"]]
    if args.action == "score":
        if args.model is None:
            raise SystemExit("score needs --model")
        result = score_frozen(json.loads(args.model.read_text(encoding="utf-8")),
                              build({s: forward_bars(args.bars, s, args.first) for s in symbols}))
        print(f"forward {result['days'][0]}~{result['days'][1]}, frozen model {args.model}")
        _table(result["cells"], result["bonferroni_alpha"])
        print(json.dumps(result, sort_keys=True, default=float))
        return 0
    data = build({s: history.development_bars(args.bars, s) for s in symbols})
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
          f"test days {result['test_days']} (train first {INITIAL}, refit every {REFIT})")
    _table(result["cells"], result["bonferroni_alpha"])
    print(json.dumps(result, sort_keys=True, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
