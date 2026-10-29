"""Pattern lab: do the last five one-minute bars and their volume tell the next five minutes' direction?

Statistics only, no trading (docs/blueprint/pattern-lab-v19.md, fixed before any bar was read):

- One symbol, 069500. Close and volume per minute come from the v12 grid
  (``direction_lab.grid``); a minute without trades is a zero-range bar at the
  last close with volume 0.
- Samples at every minute 09:30..15:14. Features, for each of the bars i-4..i:
  return, body, upper and lower wick in bp (clipped at +-100) and log volume
  against the mean of minutes i-24..i-5 (clipped at +-5); 25 in all, minutes
  <= i only. Target: sign of close[i+5] / close[i] - 1.
- Models: BASE, PATTERN (five return signs + a volume flag, Laplace up share,
  the v12 MARKOV fit), LOGIT (the v12 fit), KNN (200 nearest standardised
  training samples that moved, smoothed up share).
- ``eval`` walks forward over development days (40 to start, refit every 20)
  and scores with ``direction_lab.score``. ``freeze`` fits on every
  development day and writes once (keep it in data/, it holds training
  samples). ``score`` grades the frozen models on days from 2026-10-26.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

TOOLS = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


direction_lab = _load("direction_lab")
GRID, OPEN = direction_lab.GRID, direction_lab.OPEN
SYMBOL = "069500"
START, H, BARS, BASELINE = 30, 5, 5, 20          # 09:30, five minutes ahead, five bars, 20-minute volume base
CLIP_BP, CLIP_VOL, K = 100.0, 5.0, 200
MODELS = ("BASE", "PATTERN", "LOGIT", "KNN")
CHUNK = 200
ONLINE_MIN = 20                                  # v19b: a pattern speaks once seen this many times


def ohlcv(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    close, volume = direction_lab.grid(rows)
    o, h, low = close.copy(), close.copy(), close.copy()
    for r in rows:
        t = r["stck_cntg_hour"]
        i = int(t[:2]) * 60 + int(t[2:4]) - OPEN
        if 0 <= i < GRID:
            o[i], h[i], low[i] = float(r["stck_oprc"]), float(r["stck_hgpr"]), float(r["stck_lwpr"])
    return o, h, low, close, volume


def day_samples(o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, v: np.ndarray) -> dict[str, np.ndarray]:
    rows, targets, moves, minutes, mags = [], [], [], [], []
    for i in range(START, GRID - H):
        if np.isnan(c[i - BARS]) or np.isnan(c[i - BASELINE - BARS + 1:i + 1]).any():
            continue
        base = v[i - BASELINE - BARS + 1:i - BARS + 1].mean()
        row = []
        for j in range(i - BARS + 1, i + 1):
            top, bottom = max(o[j], c[j]), min(o[j], c[j])
            row += [np.clip((c[j] / c[j - 1] - 1) * 1e4, -CLIP_BP, CLIP_BP),
                    np.clip((c[j] / o[j] - 1) * 1e4, -CLIP_BP, CLIP_BP),
                    np.clip((h[j] - top) / o[j] * 1e4, -CLIP_BP, CLIP_BP),
                    np.clip((bottom - low[j]) / o[j] * 1e4, -CLIP_BP, CLIP_BP),
                    np.clip(np.log((v[j] + 1) / (base + 1)), -CLIP_VOL, CLIP_VOL)]
        rows.append(row)
        window = (c[i - BASELINE - BARS + 2:i - BARS + 1] / c[i - BASELINE - BARS + 1:i - BARS] - 1) * 1e4  # i-23..i-5
        nonzero = np.abs(window[window != 0])
        mags.append(float(np.median(nonzero)) if len(nonzero) else 0.0)
        minutes.append(i)
        move = c[i + H] / c[i] - 1
        targets.append(np.sign(move))
        moves.append(move)
    return {"X": np.array(rows, dtype=float).reshape(-1, 5 * BARS),
            f"y{H}": np.array(targets, dtype=float), f"r{H}": np.array(moves, dtype=float),
            "i": np.array(minutes, dtype=int), "mag": np.array(mags, dtype=float)}


def states(X: np.ndarray) -> np.ndarray:
    """Five return signs, then 1 if the five bars' mean log volume is above the baseline."""
    return np.c_[np.sign(X[:, 0::5]), (X[:, 4::5].mean(axis=1) > 0).astype(float)].astype(int)


def bar_types(X: np.ndarray, mag: np.ndarray) -> np.ndarray:
    """v19b §10.1: each of the five bars as one of 10 types, (move level + 2) * 2 + high volume."""
    ret, vol = X[:, 0::5], X[:, 4::5]
    level = np.sign(ret) * (1 + (np.abs(ret) > mag[:, None]))
    return ((level + 2) * 2 + (vol > 0)).astype(int)


class OnlineRecord:
    """Up/down counts per pattern of the last 1..5 bar types; the longest pattern seen ``minimum`` times speaks."""

    def __init__(self, minimum: int = ONLINE_MIN) -> None:
        self.minimum = minimum
        self.counts: list[dict[tuple, list[int]]] = [{} for _ in range(BARS)]

    def add(self, types: tuple, outcome: float) -> None:
        if outcome == 0:
            return
        for k in range(1, BARS + 1):
            c = self.counts[k - 1].setdefault(tuple(types[-k:]), [0, 0])
            c[0 if outcome > 0 else 1] += 1

    def prob(self, types: tuple) -> float:
        for k in range(BARS, 0, -1):
            c = self.counts[k - 1].get(tuple(types[-k:]))
            if c and sum(c) >= self.minimum:
                return (c[0] + 1) / (sum(c) + 2)
        return 0.5

    def digest(self) -> str:
        text = json.dumps([sorted((",".join(map(str, key)), c) for key, c in level.items()) for level in self.counts])
        return hashlib.sha256(text.encode()).hexdigest()


def online(data: dict, initial: int, minimum: int = ONLINE_MIN) -> dict[str, Any]:
    """v19b §10.2: seed the record with the first ``initial`` days, then predict each minute and add each
    outcome once it is known (sample at minute i at minute i + 5; a day's remaining outcomes at its close)."""
    days = list(data)
    if len(days) <= initial:
        raise SystemExit(f"need more than {initial} days, have {len(days)}")
    rec = OnlineRecord(minimum)
    for day in days[:initial]:
        s = data[day]
        for pattern, y in zip(map(tuple, bar_types(s["X"], s["mag"])), s[f"y{H}"], strict=True):
            rec.add(pattern, y)
    seed = rec.digest()
    ps, ys, rs, ds = [], [], [], []
    for n, day in enumerate(days[initial:], start=initial):
        s = data[day]
        types = list(map(tuple, bar_types(s["X"], s["mag"])))
        pending = 0
        for k, minute in enumerate(s["i"]):
            while pending < k and s["i"][pending] + H <= minute:
                rec.add(types[pending], s[f"y{H}"][pending])
                pending += 1
            ps.append(rec.prob(types[k]))
        for k in range(pending, len(types)):
            rec.add(types[k], s[f"y{H}"][k])
        ys.append(s[f"y{H}"])
        rs.append(s[f"r{H}"])
        ds.append(np.full(len(types), n))
    p, y, r, d = np.array(ps), np.concatenate(ys), np.concatenate(rs), np.concatenate(ds)
    return {"days": [days[initial], days[-1]], "seed_days": [days[0], days[initial - 1]], "seed_sha256": seed,
            "p": p, "cell": direction_lab.score(p, y, d, r)}


def fit(model: str, X: np.ndarray, y: np.ndarray, k: int = K) -> Any:
    if model in ("BASE", "LOGIT"):
        return direction_lab.fit(model, X, y)
    if model == "PATTERN":
        return direction_lab.fit("MARKOV6", states(X).astype(float), y)
    moving = y != 0
    mean, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    return {"mean": mean, "sd": sd, "Z": (X[moving] - mean) / sd, "up": (y[moving] > 0).astype(float)}


def predict(model: str, params: Any, X: np.ndarray, k: int = K) -> np.ndarray:
    if model in ("BASE", "LOGIT"):
        return direction_lab.predict(model, params, X)
    if model == "PATTERN":
        return direction_lab.predict("MARKOV6", params, states(X).astype(float))
    Z, up = params["Z"], params["up"]
    k = min(k, len(Z))
    out = np.empty(len(X))
    norms = (Z ** 2).sum(axis=1)
    for lo in range(0, len(X), CHUNK):
        q = (X[lo:lo + CHUNK] - params["mean"]) / params["sd"]
        dist = norms[None, :] - 2 * q @ Z.T
        nearest = np.argpartition(dist, k - 1, axis=1)[:, :k]
        out[lo:lo + CHUNK] = (up[nearest].sum(axis=1) + 1) / (k + 2)
    return out


def stack(data: dict, days: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    xs, ys, rs, ds = [], [], [], []
    for n, day in enumerate(days):
        s = data[day]
        keep = ~np.isnan(s[f"y{H}"])
        xs.append(s["X"][keep])
        ys.append(s[f"y{H}"][keep])
        rs.append(s[f"r{H}"][keep])
        ds.append(np.full(int(keep.sum()), n))
    if not xs:
        return np.empty((0, 5 * BARS)), np.empty(0), np.empty(0), np.empty(0)
    return np.vstack(xs), np.concatenate(ys), np.concatenate(rs), np.concatenate(ds)


def _bonferroni(cells: dict) -> float:
    bound = 0.05 / len(cells)
    for cell in cells.values():
        cell["bonferroni_significant"] = bool(cell["p_binomial"] < bound)
    return bound


def walk_forward(data: dict, initial: int = direction_lab.INITIAL, refit: int = direction_lab.REFIT,
                 k: int = K) -> dict[str, Any]:
    days = list(data)
    if len(days) <= initial:
        raise SystemExit(f"need more than {initial} days, have {len(days)}")
    preds: dict[str, list[np.ndarray]] = {m: [] for m in MODELS}
    ys, rs, ds = [], [], []
    for start in range(initial, len(days), refit):
        X, y, _, _ = stack(data, days[:start])
        Xt, yt, rt, dt = stack(data, days[start:start + refit])
        ys.append(yt)
        rs.append(rt)
        ds.append(dt + start)
        for m in MODELS:
            preds[m].append(predict(m, fit(m, X, y, k), Xt, k))
    y_all, r_all, d_all = np.concatenate(ys), np.concatenate(rs), np.concatenate(ds)
    cells = {f"{m} h{H}": direction_lab.score(np.concatenate(preds[m]), y_all, d_all, r_all) for m in MODELS}
    return {"days": [days[0], days[-1]], "train_initial": initial, "refit_every": refit,
            "test_days": len(days) - initial, "cells": cells, "bonferroni_alpha": _bonferroni(cells)}


def to_json(model: str, params: Any) -> Any:
    if model == "PATTERN":
        return direction_lab.to_json("MARKOV6", params)
    if model == "KNN":
        return {key: value.tolist() for key, value in params.items()}
    return direction_lab.to_json(model, params)


def from_json(model: str, raw: Any) -> Any:
    if model == "PATTERN":
        return direction_lab.from_json("MARKOV6", raw)
    if model == "KNN":
        return {key: np.array(value) for key, value in raw.items()}
    return direction_lab.from_json(model, raw)


def freeze(data: dict, k: int = K) -> dict:
    X, y, _, _ = stack(data, list(data))
    return {"symbol": SYMBOL, "trained": [min(data), max(data)], "k": k,
            "models": {f"{m} h{H}": to_json(m, fit(m, X, y, k)) for m in MODELS}}


def score_frozen(frozen: dict, data: dict, k: int = K) -> dict:
    days = list(data)
    X, y, r, d = stack(data, days)
    cells = {f"{m} h{H}": direction_lab.score(predict(m, from_json(m, frozen["models"][f"{m} h{H}"]), X, k), y, d, r)
             for m in MODELS}
    return {"days": [days[0], days[-1]], "cells": cells, "bonferroni_alpha": _bonferroni(cells)}


def build(bars: dict[str, list[dict]]) -> dict[str, dict[str, np.ndarray]]:
    out = {}
    for day, rows in sorted(bars.items()):
        if rows:
            samples = day_samples(*ohlcv(rows))
            if len(samples["X"]):
                out[day] = samples
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("eval", "freeze", "score", "online-eval", "online-score"))
    parser.add_argument("--bars", type=Path, default=Path("data/kis/minute-history"))
    parser.add_argument("--model", type=Path, help="freeze: output; score: the frozen model")
    parser.add_argument("--from", dest="first", default=direction_lab.FORWARD_FROM, help="score: first forward day")
    args = parser.parse_args(argv)
    if args.action.startswith("online"):
        return _online(args)
    if args.action == "score":
        if args.model is None:
            raise SystemExit("score needs --model")
        frozen = json.loads(args.model.read_text(encoding="utf-8"))
        result = score_frozen(frozen, build(direction_lab.forward_bars(args.bars, SYMBOL, args.first)), frozen["k"])
        print(f"forward {result['days'][0]}~{result['days'][1]}, {SYMBOL}, frozen model {args.model}")
    else:
        history = _load("kis_minute_history")
        data = build(history.development_bars(args.bars, SYMBOL))
        if not data or max(data) > history.DEVELOPMENT_END.isoformat():
            raise SystemExit("no development days, or a holdout day in development input")
        if args.action == "freeze":
            if args.model is None or args.model.exists():
                raise SystemExit("freeze needs a new --model path (a frozen model is written once)")
            args.model.parent.mkdir(parents=True, exist_ok=True)
            args.model.write_text(json.dumps(freeze(data)) + "\n", encoding="utf-8")
            print(f"frozen {args.model}")
            return 0
        result = walk_forward(data)
        print(f"development {result['days'][0]}~{result['days'][1]}, {SYMBOL}, test days {result['test_days']} "
              f"(train first {direction_lab.INITIAL}, refit every {direction_lab.REFIT})")
    direction_lab._table(result["cells"], result["bonferroni_alpha"])
    print(json.dumps(result, sort_keys=True, default=float))
    return 0


def _development(root: Path) -> dict:
    history = _load("kis_minute_history")
    data = build(history.development_bars(root, SYMBOL))
    if not data or max(data) > history.DEVELOPMENT_END.isoformat():
        raise SystemExit("no development days, or a holdout day in development input")
    return data


def _online(args: argparse.Namespace) -> int:
    """v19b: online-eval over development days; online-score continues from every development day."""
    dev = _development(args.bars)
    full = OnlineRecord()
    for s in dev.values():
        for pattern, y in zip(map(tuple, bar_types(s["X"], s["mag"])), s[f"y{H}"], strict=True):
            full.add(pattern, y)
    if args.action == "online-eval":
        result = online(dev, direction_lab.INITIAL)
        label = f"development {result['days'][0]}~{result['days'][1]} (record seeded by the first {direction_lab.INITIAL} days)"
    else:
        forward = build(direction_lab.forward_bars(args.bars, SYMBOL, args.first))
        if not forward:
            raise SystemExit("no forward day yet")
        result = online({**dev, **forward}, len(dev))
        label = f"forward {result['days'][0]}~{result['days'][1]} (record seeded by every development day)"
    print(f"v19b ONLINE, {SYMBOL}, {label}")
    print(f"forward seed sha256 {full.digest()}")
    cells = {f"ONLINE h{H}": result["cell"]}
    direction_lab._table(cells, _bonferroni(cells))
    print(json.dumps({key: v for key, v in result.items() if key != "p"} | {"forward_seed_sha256": full.digest()},
                     sort_keys=True, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
