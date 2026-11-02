"""v26: TimesFM 2.5 and Kronos-small against a 10-session reversal, one frame (docs/blueprint/foundation-models-v26.md).

Mac, read-only, run once (a record file stops a second run). The models need the optional extra:

    uv sync --extra agent --extra fm
    git clone https://github.com/shiyu-coder/Kronos.git data/models/Kronos && \
        git -C data/models/Kronos checkout 67b630e67f6a18c9e9be918d9b4337c960db1e9a
    uv run --extra agent --extra fm python tools/fm_rank.py --input data/krx/v2-2026-11-01d/research \
        --events data/krx/v2-2026-11-01/market-events.json --kronos-code data/models/Kronos \
        --record data/krx/fm-v26/run.json

Every 10th session from 2025-10-01 (both models public by then) is a decision day. On it, each eligible common
stock in the top 300 by 20-session trading value that no restructuring filing quarantines and that has 90
consecutive bars is scored three ways from its 90 bars up to that close (prices divided back through the share
actions published by the decision):
- ``kronos_small``: Kronos-small OHLCV, 5 independent paths (T 0.6, top_p 0.9, seed 42), mean close per step;
- ``timesfm25``: TimesFM 2.5 200M point forecast of the closes;
- ``reversal10``: minus the 10-session return (the baseline).
A model's score is (mean predicted close over 10 sessions - last close) / standard deviation of the 90 closes.
Outcomes are tools/ma_event_study.py's: bought at the next open, valued at the close 10 sessions on, against
069500 over the same open and close, round trip 0.73%; missing bars, unexplained jumps (a filed liquidation
is a real price) and the end of the input are left out and counted. Each signal is judged on its RankIC per
decision day, its top-30 net excess and its paired RankIC difference to the baseline. Model weights are pinned
revisions committed on or before 2025-09-30 (checked, else the run stops). Hypotheses return scores only;
nothing is traded. Prints the table and one JSON line; writes only the record file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from momentum.config import SpecUndefinedError

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


ma = _load("ma_cross")
es = _load("ma_event_study")
START, END = date(2025, 10, 1), date(2026, 10, 20)
LOOKBACK, HORIZON, STEP, TOP_N = 90, 10, 10, 30
SIGNALS = ("kronos_small", "timesfm25", "reversal10")
BASELINE = "reversal10"
THRESHOLD = 3.29                                   # learning ledger: Bonferroni over 100 trials
WEIGHT_CUTOFF = date(2025, 9, 30)                  # weights committed after it may have seen the test window
KRONOS = {"code": "67b630e67f6a18c9e9be918d9b4337c960db1e9a",
          "model": ("NeoQuasar/Kronos-small", "901c26c1332695a2a8f243eb2f37243a37bea320"),
          "tokenizer": ("NeoQuasar/Kronos-Tokenizer-base", "0e0117387f39004a9016484a186a908917e22426"),
          "samples": 5, "T": 0.6, "top_p": 0.9, "seed": 42, "context": 512, "chunk": 64}
TIMESFM_REPO = "google/timesfm-2.5-200m-pytorch"


@dataclass(frozen=True)
class Window:
    symbol: str
    days: tuple[date, ...]
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    volume: tuple[float, ...]


Predictor = Callable[[list[Window], list[date]], list[list[float]]]


def origins(sessions: Sequence[date]) -> list[int]:
    """Every STEP-th session from START whose HORIZON-th next session is still in the input and on or before END."""
    first = next((k for k, d in enumerate(sessions) if d >= START), None)
    if first is None:
        return []
    return [k for k in range(first, len(sessions), STEP) if k + HORIZON < len(sessions) and sessions[k] <= END]


def model_score(window: Window, predicted: Sequence[float]) -> float:
    closes = window.close
    return (statistics.fmean(predicted) - closes[-1]) / (statistics.pstdev(closes) + 1e-5)


def reversal_score(window: Window) -> float:
    return -(window.close[-1] / window.close[-1 - HORIZON] - 1)


def windows_at(data: Any, k: int, events: Any = None) -> tuple[list[Window], dict[str, int]]:
    """The scored universe on decision day ``k`` and the counts left out of it."""
    from momentum.krx.market_events import MarketEvents

    events = events or MarketEvents([])
    sessions = list(data.sessions)
    day = sessions[k]
    bars: dict[date, dict[str, Any]] = defaultdict(dict)
    for bar in data.snapshot.bars:
        if sessions[max(0, k - LOOKBACK - ma.ADV_DAYS)] <= bar.session <= day:
            bars[bar.session][bar.symbol] = bar
    states = data.securities.states(ma._decision(day))
    values = ma._universe(bars, sessions, k, states)
    ranked = sorted((s for s in values if not events.quarantined(s, ma._decision(day))),
                    key=values.__getitem__, reverse=True)[:ma.TOP]
    left = {"quarantined": sum(events.quarantined(s, ma._decision(day)) for s in values), "short_history": 0}
    factors: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for announced in data.actions:
        f = ma.action_factor(announced.action)
        if f is not None and announced.available_at <= ma._decision(day) and announced.action.ex_date <= day:
            factors[announced.action.security_id].append((announced.action.ex_date, f))
    span = sessions[k - LOOKBACK + 1:k + 1] if k + 1 >= LOOKBACK else []
    out = []
    for s in ranked:
        rows = [bars[d].get(s) for d in span]
        if len(span) < LOOKBACK or any(b is None or b.close <= 0 or b.open <= 0 for b in rows):
            left["short_history"] += 1
            continue
        per_day = [math.prod(f for ex, f in factors.get(s, []) if ex == d) for d in span]
        adjust = ma.adjust([1.0] * LOOKBACK, per_day)          # 1 / cumulative factor after each day
        scaled = {name: tuple(float(getattr(b, name)) * a for b, a in zip(rows, adjust, strict=True))
                  for name in ("open", "high", "low", "close")}
        volume = tuple(float(b.volume) / a for b, a in zip(rows, adjust, strict=True))
        out.append(Window(s, tuple(span), scaled["open"], scaled["high"], scaled["low"], scaled["close"], volume))
    return out, left


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    def ranks(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=v.__getitem__)
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for m in range(i, j + 1):
                r[order[m]] = (i + j) / 2 + 1
            i = j + 1
        return r
    if len(xs) < 3:
        return math.nan
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den > 0 else math.nan


def _t(xs: Sequence[float]) -> float:
    xs = [x for x in xs if not math.isnan(x)]
    if len(xs) < 2 or statistics.stdev(xs) == 0:
        return math.nan
    return statistics.fmean(xs) / (statistics.stdev(xs) / math.sqrt(len(xs)))


def run(data: Any, predictors: dict[str, Predictor], events: Any = None) -> dict[str, Any]:
    """Scores per signal per decision day, the realized outcomes, and what was left out."""
    sessions = list(data.sessions)
    scores: dict[str, dict[int, dict[str, float]]] = {s: {} for s in SIGNALS}
    left_universe: dict[str, int] = defaultdict(int)
    found = []
    for k in origins(sessions):
        windows, left = windows_at(data, k, events)
        for key, n in left.items():
            left_universe[key] += n
        if not windows:
            continue
        future = sessions[k + 1:k + 1 + HORIZON]
        for name, predict in predictors.items():
            predicted = predict(windows, future)
            if len(predicted) != len(windows) or any(len(p) != HORIZON for p in predicted):
                raise SpecUndefinedError(f"{name} returned {len(predicted)} forecasts for {len(windows)} windows")
            scores[name][k] = {w.symbol: model_score(w, p) for w, p in zip(windows, predicted, strict=True)}
        scores[BASELINE][k] = {w.symbol: reversal_score(w) for w in windows}
        found += [{"k": k, "day": sessions[k], "symbol": w.symbol} for w in windows]
    rows, left_outcome = es.outcomes(data, found, HORIZON, events)
    return {"scores": scores, "outcomes": rows, "left_out": {**dict(left_universe), **left_outcome},
            "days": {k: sessions[k] for k in origins(sessions)}}


def evaluate(result: dict[str, Any]) -> dict[str, Any]:
    realized: dict[int, dict[str, dict[str, float]]] = defaultdict(dict)
    for r in result["outcomes"]:
        realized[r["k"]][r["symbol"]] = r
    per: dict[str, dict[int, dict[str, float]]] = {}
    for name in SIGNALS:
        per[name] = {}
        for k, score in sorted(result["scores"][name].items()):
            names = [s for s in score if s in realized.get(k, {})]
            if len(names) < 3:
                continue
            xs = [score[s] for s in names]
            rets = [realized[k][s]["ret"] for s in names]
            ordered = sorted(names, key=score.__getitem__, reverse=True)
            third = max(1, len(names) // 3)
            per[name][k] = {
                "rank_ic": spearman(xs, rets),
                "top_net_excess": statistics.fmean(realized[k][s]["net_excess"] for s in ordered[:TOP_N]),
                "tercile_spread": statistics.fmean(realized[k][s]["ret"] for s in ordered[:third])
                - statistics.fmean(realized[k][s]["ret"] for s in ordered[-third:]),
                "n": len(names)}
    out: dict[str, Any] = {"signals": {}, "left_out": result["left_out"], "threshold": THRESHOLD}
    for name in SIGNALS:
        days = per[name]
        ics = [d["rank_ic"] for d in days.values() if not math.isnan(d["rank_ic"])]
        tops = [d["top_net_excess"] for d in days.values()]
        paired = [x for k in days if k in per[BASELINE]
                  if not math.isnan(x := days[k]["rank_ic"] - per[BASELINE][k]["rank_ic"])]
        mean_ic = statistics.fmean(ics) if ics else math.nan
        t_ic, mean_top = _t(ics), statistics.fmean(tops) if tops else math.nan
        out["signals"][name] = {
            "days": len(days), "mean_rank_ic": mean_ic, "sd_rank_ic": statistics.stdev(ics) if len(ics) > 1 else math.nan,
            "t_rank_ic": t_ic, "positive_share": sum(x > 0 for x in ics) / len(ics) if ics else math.nan,
            "mean_top_net_excess": mean_top, "t_top_net_excess": _t(tops),
            "mean_tercile_spread": statistics.fmean(d["tercile_spread"] for d in days.values()) if days else math.nan,
            "vs_baseline_mean": statistics.fmean(paired) if paired and name != BASELINE else math.nan,
            "vs_baseline_t": _t(paired) if name != BASELINE else math.nan,
            "pass": bool(ics) and mean_ic > 0 and t_ic >= THRESHOLD and mean_top > 0,
            "per_day": {result["days"][k].isoformat(): v for k, v in days.items()}}
    return out


def text(ev: dict[str, Any]) -> str:
    lines = [f"v26: pass needs mean RankIC > 0, its t >= {ev['threshold']}, top-{TOP_N} net excess > 0",
             f"left out: {json.dumps(ev['left_out'], sort_keys=True)}"]
    for name, s in ev["signals"].items():
        lines.append(f"{name}: {'PASS' if s['pass'] else 'no'} | {s['days']} days, RankIC {s['mean_rank_ic']:+.4f} "
                     f"(sd {s['sd_rank_ic']:.4f}, t {s['t_rank_ic']:+.2f}, >0 {s['positive_share']:.0%}), top {TOP_N} net "
                     f"excess {s['mean_top_net_excess']:+.2%} (t {s['t_top_net_excess']:+.2f}), tercile spread "
                     f"{s['mean_tercile_spread']:+.2%}"
                     + (f", vs {BASELINE} RankIC {s['vs_baseline_mean']:+.4f} (t {s['vs_baseline_t']:+.2f})"
                        if name != BASELINE else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ models (Mac only, optional extra "fm")
def _commit_date(repo: str, revision: str) -> date:
    from huggingface_hub import HfApi

    for c in HfApi().list_repo_commits(repo):
        if c.commit_id == revision:
            return c.created_at.date()
    raise SpecUndefinedError(f"{repo}: revision {revision} not found")


def _checked(repo: str, revision: str) -> str:
    when = _commit_date(repo, revision)
    if when > WEIGHT_CUTOFF:
        raise SpecUndefinedError(f"{repo}@{revision[:7]} committed {when}, after {WEIGHT_CUTOFF}")
    return revision


def timesfm_revision() -> str:
    """The last commit of the TimesFM 2.5 weights on or before WEIGHT_CUTOFF (the preregistered rule)."""
    from huggingface_hub import HfApi

    commits = [c for c in HfApi().list_repo_commits(TIMESFM_REPO) if c.created_at.date() <= WEIGHT_CUTOFF]
    if not commits:
        raise SpecUndefinedError(f"{TIMESFM_REPO} has no commit on or before {WEIGHT_CUTOFF}")
    return max(commits, key=lambda c: c.created_at).commit_id


def kronos_predictor(code_dir: Path, device: str = "cpu") -> tuple[Predictor, dict[str, str]]:
    import subprocess

    import numpy as np
    import pandas as pd
    import torch

    head = subprocess.run(["git", "-C", str(code_dir), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=False).stdout.strip()
    if head != KRONOS["code"]:
        raise SpecUndefinedError(f"Kronos code at {head or 'nothing'}, expected {KRONOS['code']}")
    sys.path.insert(0, str(code_dir))
    from model import (  # type: ignore[import-not-found]
        Kronos,
        KronosPredictor,
        KronosTokenizer,
    )

    (mrepo, mrev), (trepo, trev) = KRONOS["model"], KRONOS["tokenizer"]  # type: ignore[misc]
    tokenizer = KronosTokenizer.from_pretrained(trepo, revision=_checked(trepo, trev))
    model = Kronos.from_pretrained(mrepo, revision=_checked(mrepo, mrev))
    tokenizer.eval()
    model.eval()
    engine = KronosPredictor(model, tokenizer, device=device, max_context=KRONOS["context"])
    samples, chunk = int(KRONOS["samples"]), int(KRONOS["chunk"])  # type: ignore[call-overload]

    def predict(windows: list[Window], future: list[date]) -> list[list[float]]:
        frames, xs, ys = [], [], []
        stamps = pd.Series(pd.DatetimeIndex(future))
        for w in windows:
            df = pd.DataFrame({"open": w.open, "high": w.high, "low": w.low, "close": w.close, "volume": w.volume},
                              index=pd.DatetimeIndex(w.days))
            for _ in range(samples):
                frames.append(df)
                xs.append(pd.Series(df.index))
                ys.append(stamps)
        torch.manual_seed(int(KRONOS["seed"]))  # type: ignore[call-overload]
        np.random.seed(int(KRONOS["seed"]))  # type: ignore[call-overload]
        closes = []
        for s in range(0, len(frames), chunk):
            res = engine.predict_batch(frames[s:s + chunk], xs[s:s + chunk], ys[s:s + chunk], pred_len=HORIZON,
                                       T=KRONOS["T"], top_k=0, top_p=KRONOS["top_p"], sample_count=1, verbose=False)
            closes += [r["close"].to_numpy(dtype=float) for r in res]
        paths = np.array(closes).reshape(len(windows), samples, HORIZON)
        return paths.mean(axis=1).tolist()

    return predict, {"kronos_code": head, "kronos_model": mrev, "kronos_tokenizer": trev}


def timesfm_predictor() -> tuple[Predictor, dict[str, str]]:
    import numpy as np
    import timesfm  # type: ignore[import-not-found]
    import torch

    revision = timesfm_revision()
    torch.manual_seed(0)
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(TIMESFM_REPO, revision=revision)
    # inputs are 90 closes; the context is padded (masked) to the 32-step patch, so no input normalization
    # (it would average the padding in); TimesFM normalizes each series itself
    model.compile(timesfm.ForecastConfig(max_context=96, max_horizon=128, normalize_inputs=False,
                                         use_continuous_quantile_head=False, force_flip_invariance=True,
                                         infer_is_positive=True, fix_quantile_crossing=False))

    def predict(windows: list[Window], future: list[date]) -> list[list[float]]:
        point, _ = model.forecast(horizon=HORIZON, inputs=[np.array(w.close, dtype=float) for w in windows])
        return [list(map(float, row[:HORIZON])) for row in point]

    return predict, {"timesfm_package": getattr(timesfm, "__version__", "?"), "timesfm_weights": revision}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True, help="market events (tools/market_events.py)")
    parser.add_argument("--kronos-code", type=Path, required=True, help="Kronos checkout at the pinned commit")
    parser.add_argument("--record", type=Path, required=True, help="written once; an existing record stops the run")
    args = parser.parse_args(argv)
    if args.record.exists():
        raise SpecUndefinedError(f"{args.record} exists: v26 is recorded once")
    from momentum.agent.cli import read_input
    from momentum.krx.market_events import MarketEvents

    root = args.input
    data = read_input(root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
                      securities_mode="session-confirmed")
    kronos, kronos_pins = kronos_predictor(args.kronos_code)
    timesfm_fn, timesfm_pins = timesfm_predictor()
    started = datetime.now(ma.KST).isoformat(timespec="seconds")
    result = run(data, {"kronos_small": kronos, "timesfm25": timesfm_fn}, MarketEvents.load(args.events))
    ev = evaluate(result)
    ev["pins"] = {**kronos_pins, **timesfm_pins}
    ev["started"] = started
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(ev, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"pins: {json.dumps(ev['pins'], sort_keys=True)}")
    print(text(ev))
    print(json.dumps(ev, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
