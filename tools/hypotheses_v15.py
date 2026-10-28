"""v15 original hypotheses H1-H3 (docs/blueprint/original-hypotheses-v15.md), fixed before any result.

Mac, read-only, no network:

    uv run python tools/hypotheses_v15.py --phase dev
    uv run python tools/hypotheses_v15.py --phase validation   # only after dev, once

H1  ETF creations/redemptions (FSC listed shares x NAV): (a) ETFs with the
    largest 20-session inflow underperform the smallest over the next 20
    sessions; (b) inflow into KOSPI200 inverse ETFs precedes a rise of 069500.
    Signals on each month's last session; entry at the next session's close.
H2  The closing-auction move (15:30 close vs last trade at or before 15:19)
    reverses at the next open. Day-clustered slope; plus the mean gap after
    a <= -20bp less 3bp of commission (buy at the close after hours, sell at
    the next open auction).
H3  Report only: a KOSPI200 ETF's deviation from its siblings' median price
    (less its last-60-minute mean) against its next-5-minute relative return.

The validation phase needs the dev record, runs only what passed dev, and
runs once. Records go to --record-dir (data/, not committed).
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

from momentum.config import SpecUndefinedError

TOOLS = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


premium, dl = _load("etf_premium_stats"), _load("direction_lab")
H1_MONTHS = {"dev": ("2020-02", "2023-12"), "validation": ("2024-01", "2026-09")}
MINUTE_DAYS = {"dev": ("2025-10-27", "2026-06-30"), "validation": ("2026-07-01", "2026-10-23")}
WINDOW, LISTED, MIN_CAP = 20, 60, 1e10
MARKET = "069500"
SIBLINGS = ("069500", "102110", "148020", "105190")
AUX_LINE, AUX_COST = -0.002, 0.0003
DEV_PASS = {"H1a": -2.0, "H1b": 2.0, "H2": -2.0}
VALIDATION_PASS = {"H1a": -1.5, "H1b": 1.5, "H2": -1.5}


# ---- statistics ---------------------------------------------------------
def t_mean(values: list[float]) -> float:
    if len(values) < 2 or statistics.stdev(values) == 0:
        return math.nan
    return statistics.fmean(values) / (statistics.stdev(values) / math.sqrt(len(values)))


def ols(x: list[float], y: list[float]) -> tuple[float, float]:
    """Slope and its plain t (iid errors)."""
    n = len(x)
    if n < 3:
        return math.nan, math.nan
    xm, ym = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - xm) ** 2 for a in x)
    beta = sum((a - xm) * (b - ym) for a, b in zip(x, y, strict=True)) / sxx
    resid = [b - ym - beta * (a - xm) for a, b in zip(x, y, strict=True)]
    se = math.sqrt(sum(e * e for e in resid) / (n - 2) / sxx)
    return beta, beta / se if se > 0 else math.nan


def clustered_slope(x: np.ndarray, y: np.ndarray, cluster: np.ndarray) -> tuple[float, float]:
    """Slope of y on x with standard errors clustered by ``cluster`` (days)."""
    if len(x) < 3:
        return math.nan, math.nan
    xd, yd = x - x.mean(), y - y.mean()
    sxx = float(xd @ xd)
    beta = float(xd @ yd) / sxx
    score = xd * (yd - beta * xd)
    var = sum(float(score[cluster == c].sum()) ** 2 for c in np.unique(cluster)) / sxx ** 2
    return beta, beta / math.sqrt(var) if var > 0 else math.nan


# ---- H1 -----------------------------------------------------------------
class EtfDay(NamedTuple):
    close: float
    nav: float
    shares: float
    cap: float
    name: str
    index: str


def load_etf(root: Path) -> dict[str, dict[str, EtfDay]]:
    out: dict[str, dict[str, EtfDay]] = {}
    for folder in sorted(p for p in (root / "etf").iterdir() if p.is_dir() and not p.name.startswith(".")):
        rows = {}
        for page in sorted(folder.glob("page-*.json")):
            for it in premium._items(json.loads(page.read_text(encoding="utf-8"))) or []:
                row = EtfDay(premium._num(it.get("clpr")), premium._num(it.get("nav")),
                             premium._num(it.get("stLstgCnt")), premium._num(it.get("mrktTotAmt")),
                             it.get("itmsNm", ""), it.get("bssIdxIdxNm", ""))
                if row.close > 0 and row.nav > 0 and row.shares > 0:
                    rows[it["srtnCd"]] = row
        if rows:
            out[folder.name] = rows
    return out


def _flow(data: dict, sessions: list[str], codes: list[str], k: int) -> float | None:
    """Sum over codes of (shares change x NAV) over the last WINDOW sessions / value WINDOW sessions ago."""
    span = sessions[k - WINDOW:k + 1]
    if any(c not in data[d] for d in span for c in codes):
        return None
    base = sum(data[span[0]][c].shares * data[span[0]][c].nav for c in codes)
    moved = sum((data[b][c].shares - data[a][c].shares) * data[b][c].nav
                for a, b in pairwise(span) for c in codes)
    return moved / base if base > 0 else None


def month_ends(sessions: list[str], first: str, last: str) -> list[int]:
    out = []
    for k, (a, b) in enumerate(pairwise(sessions)):
        if a[:7] != b[:7] and first <= a[:7] <= last and k >= WINDOW and k + 1 + WINDOW < len(sessions):
            out.append(k)
    return out


def h1(data: dict[str, dict[str, EtfDay]], first: str, last: str) -> dict[str, Any]:
    sessions = sorted(data)
    seen: dict[str, int] = {}
    for k, d in enumerate(sessions):
        for c in data[d]:
            seen.setdefault(c, k)
    spreads, inverse_x, market_y = [], [], []
    for k in month_ends(sessions, first, last):
        entry, exit_ = sessions[k + 1], sessions[k + 1 + WINDOW]
        ranked = []
        for c, row in data[sessions[k]].items():
            if (premium.group(row.name) != "domestic_equity" or row.cap < MIN_CAP or k - seen[c] < LISTED
                    or c not in data[entry] or c not in data[exit_]):
                continue
            f = _flow(data, sessions, [c], k)
            if f is not None:
                ranked.append((f, data[exit_][c].close / data[entry][c].close - 1))
        if len(ranked) >= 10:
            ranked.sort()
            n = len(ranked) // 10
            spreads.append(statistics.fmean(r for _, r in ranked[-n:]) - statistics.fmean(r for _, r in ranked[:n]))
        inverse = sorted(c for c, row in data[sessions[k]].items() if "인버스" in row.name and "200" in row.index)
        f = _flow(data, sessions, inverse, k) if inverse else None
        if f is not None and MARKET in data[entry] and MARKET in data[exit_]:
            inverse_x.append(f)
            market_y.append(data[exit_][MARKET].close / data[entry][MARKET].close - 1)
    beta, t = ols(inverse_x, market_y)
    return {"H1a": {"months": len(spreads), "mean_spread": statistics.fmean(spreads) if spreads else math.nan,
                    "t": t_mean(spreads)},
            "H1b": {"months": len(inverse_x), "slope": beta, "t": t}}


# ---- H2 / H3 --------------------------------------------------------------
def _bars(root: Path, symbol: str) -> dict[str, list[dict]]:
    out = {}
    for path in sorted((root / symbol).glob("*.json")):
        bars = json.loads(path.read_text(encoding="utf-8"))["bars"]
        if bars:
            out[path.stem] = sorted(bars, key=lambda r: r["stck_cntg_hour"])
    return out


def auction_points(rows: list[dict]) -> tuple[float | None, float | None, float | None]:
    """(last trade at or before 15:19, 15:30 close, first open at or after 09:00)."""
    cont = [float(r["stck_prpr"]) for r in rows if r["stck_cntg_hour"] < "152000"]
    close = [float(r["stck_prpr"]) for r in rows if r["stck_cntg_hour"] == "153000"]
    opens = [float(r["stck_oprc"]) for r in rows if r["stck_cntg_hour"] >= "090000"]
    return (cont[-1] if cont else None, close[0] if close else None, opens[0] if opens else None)


def h2(root: Path, symbols: list[str], first: str, last: str) -> dict[str, Any]:
    bars = {s: _bars(root, s) for s in symbols}
    sessions = sorted({d for b in bars.values() for d in b})
    following = dict(pairwise(sessions))
    xs, ys, ds = [], [], []
    for s in symbols:
        for d, rows in bars[s].items():
            nxt = following.get(d)
            if not first <= d <= last or nxt is None or nxt not in bars[s]:
                continue
            cont, close, _ = auction_points(rows)
            nxt_open = auction_points(bars[s][nxt])[2]
            if cont and close and nxt_open:
                xs.append(close / cont - 1)
                ys.append(nxt_open / close - 1)
                ds.append(sessions.index(d))
    x, y, day = np.array(xs), np.array(ys), np.array(ds)
    beta, t = clustered_slope(x, y, day)
    hit = y[x <= AUX_LINE]
    return {"pairs": len(xs), "days": len(set(ds)), "slope": beta, "t": t,
            "aux_trades": len(hit), "aux_mean_after_cost": float(hit.mean() - AUX_COST) if len(hit) else math.nan}


def h3(root: Path, first: str, last: str, symbols: tuple[str, ...] = SIBLINGS) -> dict[str, Any]:
    bars = {s: _bars(root, s) for s in symbols}
    days = sorted(set.intersection(*(set(b) for b in bars.values())))
    xs, ys, ds = [], [], []
    lo, hi = 30, 15 * 60 + 10 - dl.OPEN
    for k, d in enumerate(dd for dd in days if first <= dd <= last):
        lp = np.array([np.log(dl.grid([{**r, "cntg_vol": r.get("cntg_vol", "0")} for r in bars[s][d]])[0])
                       for s in symbols])
        rel = lp - np.median(lp, axis=0)
        for t in range(lo, hi + 1):
            past = rel[:, max(0, t - 60):t]
            if np.isnan(past).any() or np.isnan(rel[:, t + 5]).any() or np.isnan(rel[:, t]).any():
                continue
            dev = rel[:, t] - past.mean(axis=1)
            xs.extend(dev)
            ys.extend(rel[:, t + 5] - rel[:, t])
            ds.extend([k] * len(symbols))
    x, y = np.array(xs), np.array(ys)
    beta, t = clustered_slope(x, y, np.array(ds))
    return {"points": len(xs), "slope": beta, "t": t,
            "share_abs_dev_over_5bp": float(np.mean(np.abs(x) > 5e-4)) if len(x) else math.nan}


# ---- phases -------------------------------------------------------------
def dev_passed(record: dict) -> list[str]:
    t = {"H1a": record["H1"]["H1a"]["t"], "H1b": record["H1"]["H1b"]["t"], "H2": record["H2"]["t"]}
    return [h for h, line in DEV_PASS.items() if not math.isnan(t[h]) and (t[h] <= line if line < 0 else t[h] >= line)]


def run(phase: str, fsc: Path, bars: Path, symbols: list[str], record_dir: Path) -> dict[str, Any]:
    dev_path, val_path = record_dir / "dev.json", record_dir / "validation.json"
    if phase == "dev":
        if dev_path.exists():
            raise SpecUndefinedError(f"{dev_path} exists; the development run is recorded once")
        months, days = H1_MONTHS["dev"], MINUTE_DAYS["dev"]
        out = {"phase": "dev", "H1": h1(load_etf(fsc), *months), "H2": h2(bars, symbols, *days),
               "H3": h3(bars, *days)}
        out["passed_dev"] = dev_passed(out)
        path = dev_path
    else:
        if not dev_path.exists():
            raise SpecUndefinedError("validation needs the development record first")
        if val_path.exists():
            raise SpecUndefinedError("the validation phase already ran")
        todo = json.loads(dev_path.read_text(encoding="utf-8"))["passed_dev"]
        out = {"phase": "validation", "ran": todo}
        if {"H1a", "H1b"} & set(todo):
            out["H1"] = h1(load_etf(fsc), *H1_MONTHS["validation"])
        if "H2" in todo:
            out["H2"] = h2(bars, symbols, *MINUTE_DAYS["validation"])
        verdict = {}
        for h in todo:
            t = out["H2"]["t"] if h == "H2" else out["H1"][h]["t"]
            line = VALIDATION_PASS[h]
            ok = not math.isnan(t) and (t <= line if line < 0 else t >= line)
            if h == "H2":
                ok = ok and out["H2"]["aux_mean_after_cost"] > 0
            verdict[h] = ok
        out["passed_validation"] = verdict
        path = val_path
    record_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, default=float) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=("dev", "validation"), required=True)
    parser.add_argument("--fsc", type=Path, default=Path("data/krx/raw/fsc"))
    parser.add_argument("--bars", type=Path, default=Path("data/kis/minute-history"))
    parser.add_argument("--universe", type=Path, default=Path("config/intraday-v11/manifest.json"))
    parser.add_argument("--record-dir", type=Path, default=Path("data/kis/hypotheses-v15"))
    args = parser.parse_args(argv)
    symbols = json.loads(args.universe.read_text(encoding="utf-8"))["universe"]
    try:
        out = run(args.phase, args.fsc, args.bars, symbols, args.record_dir)
    except SpecUndefinedError as exc:
        print(f"hypotheses-v15: STOP: {exc}")
        return 1
    print(json.dumps(out, indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
