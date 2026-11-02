"""tools/fm_rank.py (v26) with stand-in predictors; the real models run only on the Mac."""

import importlib.util
import math
import random
import sys
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from momentum.krx.market_events import MarketEvents
from momentum.ledger.ledger import CorporateAction

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("fm_rank", ROOT / "tools/fm_rank.py")
fm = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = fm
spec.loader.exec_module(fm)

N = 200
SESSIONS = t.weekdays(N, start=date(2025, 4, 1))
SYMBOLS = [f"{k:05d}0" for k in range(1, 13)]


def market(seed=7, actions=(), paths=None):
    rng = random.Random(seed)
    if paths is None:
        paths = {}
        for s in SYMBOLS + ["069500"]:
            px, path = 10000.0, []
            for _ in range(N):
                px *= 1 + rng.gauss(0, 0.02)
                path.append(round(px))
            paths[s] = path
    bars = [SimpleNamespace(session=d, symbol=s, open=Decimal(str(p[k])), high=Decimal(str(p[k])),
                            low=Decimal(str(p[k])), close=Decimal(str(p[k])), volume=100_000_000)
            for s, p in paths.items() for k, d in enumerate(SESSIONS)]
    states = {s: t.state(s, "ETF" if s == "069500" else "STOCK") for s in paths}
    data = SimpleNamespace(sessions=tuple(SESSIONS), snapshot=SimpleNamespace(bars=bars), actions=tuple(actions),
                           securities=SimpleNamespace(states=lambda at: dict(states)))
    return data, paths


def test_decision_days_start_on_2025_10_01_every_10_sessions_and_keep_10_sessions_after():
    ks = fm.origins(SESSIONS)
    assert SESSIONS[ks[0]] == date(2025, 10, 1) and all(b - a == 10 for a, b in pairwise(ks))
    assert ks[-1] + fm.HORIZON < N and ks[-1] + fm.STEP + fm.HORIZON >= N


def test_scores_and_rank_correlation_by_hand():
    w = fm.Window("000010", (), (), (), (), tuple([100.0] * 79 + [90.0] + [100.0] * 10), ())
    sd = math.sqrt(sum((c - 99.888888) ** 2 for c in w.close) / 90)
    assert fm.model_score(w, [110.0] * 10) == pytest.approx(10 / (sd + 1e-5), rel=1e-4)
    assert fm.reversal_score(w) == pytest.approx(-(100 / 90 - 1))
    assert fm.spearman([1, 2, 3], [1, 3, 2]) == pytest.approx(0.5)
    assert fm.spearman([1, 1, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9486833)   # ties take the average rank


def test_windows_use_the_split_published_by_the_decision_and_drop_quarantined_or_short_names():
    k = fm.origins(SESSIONS)[0]
    split_day = SESSIONS[k - 5]
    paths = {s: [1000.0] * N for s in SYMBOLS + ["069500"]}
    paths["000010"] = [1000.0 if i < k - 5 else 200.0 for i in range(N)]        # 1:5 split, recorded
    split = SimpleNamespace(action=CorporateAction("000010", "split", split_day, None, Decimal(5), None),
                            available_at=datetime.combine(SESSIONS[k - 20], datetime.min.time(), t.KST))
    data, _ = market(actions=[split], paths=paths)
    data.snapshot.bars = [b for b in data.snapshot.bars if not (b.symbol == "000030" and b.session == SESSIONS[k - 50])]
    filed = datetime.combine(SESSIONS[k - 3], datetime.min.time(), t.KST).replace(hour=18).isoformat()
    events = MarketEvents([{"symbol": "000020", "kind": "restructuring", "rcept_date": SESSIONS[k - 3].isoformat(),
                            "available_at": filed, "title": "x"}])
    windows, left = fm.windows_at(data, k, events)
    by = {w.symbol: w for w in windows}
    assert set(by) == set(SYMBOLS) - {"000020", "000030"}
    assert left == {"quarantined": 1, "short_history": 1}
    assert set(by["000010"].close) == {200.0} and by["000010"].volume[0] == 500_000_000   # prices / 5, volume x 5
    assert len(by["000040"].close) == fm.LOOKBACK and by["000040"].days[-1] == SESSIONS[k]


def test_an_oracle_ranks_well_the_baseline_is_scored_and_a_short_answer_is_refused():
    data, paths = market()
    index = {d: i for i, d in enumerate(SESSIONS)}

    def oracle(windows, future):
        return [[float(paths[w.symbol][index[d]]) for d in future] for w in windows]

    def flat(windows, future):
        return [[w.close[-1]] * len(future) for w in windows]

    result = fm.run(data, {"kronos_small": oracle, "timesfm25": flat})
    ev = fm.evaluate(result)
    assert ev["signals"]["kronos_small"]["mean_rank_ic"] > 0.5
    assert ev["signals"]["timesfm25"]["mean_top_net_excess"] == pytest.approx(
        ev["signals"]["timesfm25"]["mean_top_net_excess"])                     # a flat forecast still evaluates
    assert ev["signals"]["reversal10"]["days"] == len(fm.origins(SESSIONS))
    for s in ev["signals"].values():
        assert s["pass"] == (s["mean_rank_ic"] > 0 and s["t_rank_ic"] >= fm.THRESHOLD and s["mean_top_net_excess"] > 0)
    with pytest.raises(fm.SpecUndefinedError):
        fm.run(data, {"kronos_small": lambda w, f: [[1.0] * 3 for _ in w], "timesfm25": flat})


def test_unfused_attention_weights_are_stacked_query_key_value_and_a_partial_set_is_refused():
    def cat(parts: list[list[int]]) -> list[int]:
        return [x for p in parts for x in p]

    state = {"stacked_xf.0.attn.query.weight": [1], "stacked_xf.0.attn.key.weight": [2],
             "stacked_xf.0.attn.value.weight": [3], "stacked_xf.0.attn.out.weight": [9], "tokenizer.w": [7]}
    assert fm.fused_qkv(state, cat) == {"stacked_xf.0.attn.qkv_proj.weight": [1, 2, 3],
                                         "stacked_xf.0.attn.out.weight": [9], "tokenizer.w": [7]}
    already = {"stacked_xf.0.attn.qkv_proj.weight": [1, 2, 3]}
    assert fm.fused_qkv(already, cat) == already
    with pytest.raises(fm.SpecUndefinedError):
        fm.fused_qkv({"stacked_xf.0.attn.query.weight": [1], "stacked_xf.0.attn.key.weight": [2]}, cat)
