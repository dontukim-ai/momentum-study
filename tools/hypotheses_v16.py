"""v16 theory-based hypotheses D1-D3 (docs/blueprint/theory-hypotheses-v16.md), fixed before any result.

Mac, read-only, no network:

    uv run python tools/hypotheses_v16.py --phase dev
    uv run python tools/hypotheses_v16.py --phase validation   # only after dev, once

D1  Korean MAX (lottery) effect within the highest turnover tercile.
D2  Ledoit-Wolf shrunk vs sample covariance long-only minimum variance
    (top 100 stocks, 250-session window): realised variance difference.
D3  Report only: KOSPI200 index return against exposure-weighted KOSPI200 ETF
    net creations / KOSPI market cap, at lags -3..+5 sessions.

Daily stock returns come from raw closes; a day whose exchange base price
(close - change) differs from the previous close carries a corporate action
and is quarantined, not repaired. Records go to --record-dir (data/, not
committed).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
from scipy import optimize

from momentum.config import SpecUndefinedError

TOOLS = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


premium, v15 = _load("etf_premium_stats"), _load("hypotheses_v15")
MONTHS = {"D1": {"dev": ("2020-02", "2023-12"), "validation": ("2024-01", "2026-09")},
          "D2": {"dev": ("2021-01", "2023-12"), "validation": ("2024-01", "2026-09")},
          "D3": {"dev": ("2020-01", "2023-12"), "validation": ("2024-01", "2026-10")}}
MIN_CAP, MIN_PRICE, MIN_DAYS = 5e10, 1000.0, 15
WINDOW, TOP_N, MAX_WEIGHT = 250, 100, 0.10
LEVERED = {"122630": 2, "123320": 2, "114800": -1, "123310": -1, "252670": -2, "252710": -2}
MARKET_ETF, INDEX_TOL, LAGS = "069500", 0.005, range(-3, 6)
DEV_PASS = {"D1": -2.0, "D2": -2.0}
VALIDATION_PASS = {"D1": -1.5, "D2": -1.5}


class Stock(NamedTuple):
    close: float
    base: float
    volume: float
    shares: float
    cap: float
    market: str


def common_share(code: str, name: str, market: str) -> bool:
    return market in ("KOSPI", "KOSDAQ") and code.endswith("0") and "스팩" not in name


def load_stocks(root: Path) -> dict[str, dict[str, Stock]]:
    out: dict[str, dict[str, Stock]] = {}
    for folder in sorted(p for p in (root / "stock").iterdir() if p.is_dir() and not p.name.startswith(".")):
        rows = {}
        for page in sorted(folder.glob("page-*.json")):
            for it in premium._items(json.loads(page.read_text(encoding="utf-8"))) or []:
                code, market = it.get("srtnCd", ""), it.get("mrktCtg", "")
                if not common_share(code, it.get("itmsNm", ""), market):
                    continue
                close = premium._num(it.get("clpr"))
                if close > 0:
                    rows[code] = Stock(close, close - premium._num(it.get("vs")), premium._num(it.get("trqu")),
                                       premium._num(it.get("lstgStCnt")), premium._num(it.get("mrktTotAmt")), market)
        if rows:
            out[folder.name] = rows
    return out


def daily_returns(data: dict[str, dict[str, Stock]]) -> tuple[list[str], dict[str, dict[int, float]], int]:
    """Valid close-to-close returns by session index; corporate-action days are quarantined and counted."""
    sessions = sorted(data)
    returns: dict[str, dict[int, float]] = {}
    quarantined = 0
    for k, (a, b) in enumerate(pairwise(sessions), start=1):
        for code, row in data[b].items():
            prev = data[a].get(code)
            if prev is None:
                continue
            if abs(row.base - prev.close) > 0.5:
                quarantined += 1
                continue
            returns.setdefault(code, {})[k] = row.close / prev.close - 1
    return sessions, returns, quarantined


def month_bounds(sessions: list[str], first: str, last: str) -> list[tuple[int, int, int]]:
    """(month start, month end, next month end) session indices for months first..last."""
    ends = [k for k, (a, b) in enumerate(pairwise(sessions)) if a[:7] != b[:7]]
    out = []
    for prev_end, end, nxt in zip(ends, ends[1:], ends[2:], strict=False):
        if first <= sessions[end][:7] <= last and nxt + 1 < len(sessions):
            out.append((prev_end + 1, end, nxt))
    return out


def _hold(rets: dict[int, float], lo: int, hi: int) -> float | None:
    valid = [rets[i] for i in range(lo, hi + 1) if i in rets]
    return math.prod(1 + r for r in valid) - 1 if valid else None


def _spread(rows: list[tuple[float, float, float]]) -> float | None:
    """Cap-weighted top-minus-bottom decile of (max, cap, return) rows."""
    if len(rows) < 20:
        return None
    rows = sorted(rows)
    n = len(rows) // 10

    def vw(part: list[tuple[float, float, float]]) -> float:
        return sum(c * r for _, c, r in part) / sum(c for _, c, _ in part)
    return vw(rows[-n:]) - vw(rows[:n])


def d1(data: dict, sessions: list[str], returns: dict, first: str, last: str) -> dict[str, Any]:
    out: dict[str, list[float]] = {"all": [], "high_turnover": [], "low_turnover": []}
    dropped = 0
    for start, end, nxt in month_bounds(sessions, first, last):
        rows = []
        for code, row in data[sessions[end]].items():
            rets = returns.get(code, {})
            month = [rets[i] for i in range(start, end + 1) if i in rets]
            if row.cap < MIN_CAP or row.close < MIN_PRICE or len(month) < MIN_DAYS:
                continue
            turns = [data[sessions[i]][code].volume / data[sessions[i]][code].shares
                     for i in range(start, end + 1) if code in data[sessions[i]] and data[sessions[i]][code].shares > 0]
            held = _hold(rets, end + 2, nxt + 1)
            if held is None:
                dropped += 1
                continue
            rows.append((max(month), statistics.fmean(turns), row.cap, held))
        if len(rows) < 60:
            continue
        cuts = np.quantile([r[1] for r in rows], [1 / 3, 2 / 3])
        for label, part in (("all", rows), ("high_turnover", [r for r in rows if r[1] > cuts[1]]),
                            ("low_turnover", [r for r in rows if r[1] <= cuts[0]])):
            s = _spread([(m, c, h) for m, _, c, h in part])
            if s is not None:
                out[label].append(s)
    return {k: {"months": len(v), "mean": statistics.fmean(v) if v else math.nan, "t": v15.t_mean(v)}
            for k, v in out.items()} | {"dropped_no_return": dropped}


def ledoit_wolf(x: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf (2004) shrinkage toward a scaled identity; returns (covariance, intensity)."""
    t, p = x.shape
    x = x - x.mean(axis=0)
    s = x.T @ x / t
    mu = np.trace(s) / p
    d2 = float(np.sum((s - mu * np.eye(p)) ** 2)) / p
    b2 = sum(float(np.sum((np.outer(row, row) - s) ** 2)) for row in x) / p / t ** 2
    shrink = min(b2, d2) / d2 if d2 > 0 else 1.0
    return shrink * mu * np.eye(p) + (1 - shrink) * s, shrink


def min_variance(cov: np.ndarray, cap: float = MAX_WEIGHT) -> np.ndarray:
    n = len(cov)
    res = optimize.minimize(lambda w: float(w @ cov @ w), np.full(n, 1 / n), jac=lambda w: 2 * cov @ w,
                            bounds=[(0.0, cap)] * n, constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1}],
                            method="SLSQP", options={"maxiter": 500, "ftol": 1e-12})
    if not res.success:
        raise SpecUndefinedError(f"minimum variance did not converge: {res.message}")
    return np.clip(res.x, 0, cap)


def d2(data: dict, sessions: list[str], returns: dict, first: str, last: str) -> dict[str, Any]:
    diffs, squares = [], {"sample": [], "shrunk": [], "equal": []}
    renormalized, shrinks = 0, []
    for _, end, nxt in month_bounds(sessions, first, last):
        span = range(end - WINDOW + 1, end + 1)
        ranked = sorted(((row.cap, code) for code, row in data[sessions[end]].items() if row.cap >= MIN_CAP),
                        reverse=True)
        chosen = [c for _, c in ranked if all(i in returns.get(c, {}) for i in span)][:TOP_N]
        if len(chosen) < TOP_N:
            continue
        hist = np.array([[returns[c][i] for c in chosen] for i in span])
        shrunk, intensity = ledoit_wolf(hist)
        shrinks.append(intensity)
        weights = {"sample": min_variance(np.cov(hist, rowvar=False, bias=True)), "shrunk": min_variance(shrunk),
                   "equal": np.full(len(chosen), 1 / len(chosen))}
        month_sq: dict[str, list[float]] = {k: [] for k in weights}
        for i in range(end + 2, nxt + 2):
            have = np.array([i in returns[c] for c in chosen])
            if not have.any():
                continue
            r = np.array([returns[c].get(i, 0.0) for c in chosen])
            renormalized += int(not have.all())
            for k, w in weights.items():
                ww = w * have
                month_sq[k].append(float((ww @ r) / ww.sum()) ** 2)
        if month_sq["sample"]:
            diffs.append(statistics.fmean(month_sq["shrunk"]) - statistics.fmean(month_sq["sample"]))
            for k, values in month_sq.items():
                squares[k].extend(values)
    vol = {k: math.sqrt(statistics.fmean(v) * 252) if v else math.nan for k, v in squares.items()}
    return {"months": len(diffs), "mean_diff": statistics.fmean(diffs) if diffs else math.nan,
            "t": v15.t_mean(diffs), "annual_vol": vol, "renormalized_days": renormalized,
            "mean_shrinkage": statistics.fmean(shrinks) if shrinks else math.nan}


class Etf(NamedTuple):
    nav: float
    shares: float
    index: float


def load_etf_index(root: Path) -> dict[str, dict[str, Etf]]:
    out: dict[str, dict[str, Etf]] = {}
    for folder in sorted(p for p in (root / "etf").iterdir() if p.is_dir() and not p.name.startswith(".")):
        rows = {}
        for page in sorted(folder.glob("page-*.json")):
            for it in premium._items(json.loads(page.read_text(encoding="utf-8"))) or []:
                row = Etf(premium._num(it.get("nav")), premium._num(it.get("stLstgCnt")),
                          premium._num(it.get("bssIdxClpr")))
                if row.nav > 0 and row.shares > 0:
                    rows[it["srtnCd"]] = row
        if rows:
            out[folder.name] = rows
    return out


def d3(etfs: dict[str, dict[str, Etf]], stocks: dict[str, dict[str, Stock]], first: str, last: str) -> dict[str, Any]:
    days = [d for d in sorted(etfs) if d in stocks]
    xs: dict[int, float] = {}
    ys: dict[int, float] = {}
    for k in range(1, len(days)):
        a, b = days[k - 1], days[k]
        idx_a, idx_b = etfs[a].get(MARKET_ETF), etfs[b].get(MARKET_ETF)
        if not idx_a or not idx_b or idx_a.index <= 0 or idx_b.index <= 0:
            continue
        ys[k] = idx_b.index / idx_a.index - 1
        if not first <= b[:7] <= last:
            continue
        flow = 0.0
        for code, row in etfs[b].items():
            prev = etfs[a].get(code)
            if prev is None:
                continue
            exposure = LEVERED.get(code)
            if exposure is None and row.index > 0 and abs(row.index / idx_b.index - 1) <= INDEX_TOL:
                exposure = 1
            if exposure:
                flow += exposure * (row.shares - prev.shares) * row.nav
        cap = sum(s.cap for s in stocks[a].values() if s.market == "KOSPI")
        if cap > 0:
            xs[k] = flow / cap
    table = {}
    for lag in LAGS:
        pairs = [(x, ys[k + lag]) for k, x in xs.items() if k + lag in ys]
        beta, t = v15.ols([p[0] for p in pairs], [p[1] for p in pairs])
        table[str(lag)] = {"n": len(pairs), "multiplier": beta, "t": t}
    return {"days": len(xs), "by_lag": table}


def dev_passed(record: dict) -> list[str]:
    t = {"D1": record["D1"]["high_turnover"]["t"], "D2": record["D2"]["t"]}
    return [h for h, line in DEV_PASS.items() if not math.isnan(t[h]) and t[h] <= line]


def run(phase: str, fsc: Path, record_dir: Path) -> dict[str, Any]:
    dev_path, val_path = record_dir / "dev.json", record_dir / "validation.json"
    if phase == "dev" and dev_path.exists():
        raise SpecUndefinedError(f"{dev_path} exists; the development run is recorded once")
    if phase == "validation":
        if not dev_path.exists():
            raise SpecUndefinedError("validation needs the development record first")
        if val_path.exists():
            raise SpecUndefinedError("the validation phase already ran")
    todo = (["D1", "D2"] if phase == "dev"
            else json.loads(dev_path.read_text(encoding="utf-8"))["passed_dev"])
    stocks = load_stocks(fsc)
    sessions, returns, quarantined = daily_returns(stocks)
    out: dict[str, Any] = {"phase": phase, "quarantined_stock_days": quarantined}
    if "D1" in todo:
        out["D1"] = d1(stocks, sessions, returns, *MONTHS["D1"][phase])
    if "D2" in todo:
        out["D2"] = d2(stocks, sessions, returns, *MONTHS["D2"][phase])
    out["D3"] = d3(load_etf_index(fsc), stocks, *MONTHS["D3"][phase])
    if phase == "dev":
        out["passed_dev"] = dev_passed(out)
    else:
        out["ran"] = todo
        t = {"D1": out.get("D1", {}).get("high_turnover", {}).get("t"), "D2": out.get("D2", {}).get("t")}
        out["passed_validation"] = {h: t[h] is not None and not math.isnan(t[h]) and t[h] <= VALIDATION_PASS[h]
                                    for h in todo}
    record_dir.mkdir(parents=True, exist_ok=True)
    (dev_path if phase == "dev" else val_path).write_text(json.dumps(out, indent=1, default=float) + "\n",
                                                          encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=("dev", "validation"), required=True)
    parser.add_argument("--fsc", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--record-dir", type=Path, default=Path("data/krx/hypotheses-v16"))
    args = parser.parse_args(argv)
    try:
        out = run(args.phase, args.fsc, args.record_dir)
    except SpecUndefinedError as exc:
        print(f"hypotheses-v16: STOP: {exc}")
        return 1
    print(json.dumps(out, indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
