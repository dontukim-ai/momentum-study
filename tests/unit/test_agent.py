"""Economic and persistence regressions for the unified research runtime."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import AgentError, Bar, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput
from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.service import ResearchAgent
from momentum.ledger.ledger import Ledger


def market(count=360, *, cycle=True):
    bars, states = [], []
    day, close = date(2024, 1, 2), D(10000)
    while len(bars) < count:
        if day.weekday() < 5:
            opening = close
            close = (
                (opening * (D("1.02") if len(bars) % 20 < 10 else D(".98"))).quantize(
                    D(1)
                )
                if cycle
                else close
            )
            b = Bar(
                day,
                "FIXTURE",
                opening,
                max(opening, close),
                min(opening, close),
                close,
                100000,
                session_time(day, 15, 40),
                "ENGINEERED_SOFTWARE_FIXTURE",
            )
            bars.append(b)
            states.append(
                SecurityState(
                    "FIXTURE",
                    session_time(day, 15, 30),
                    session_time(day, 15, 40),
                    "NORMAL",
                    "STOCK",
                    "KOSPI",
                    False,
                    False,
                    False,
                    False,
                    close * D(".9995"),
                    close * D("1.0005"),
                    "ENGINEERED_SOFTWARE_FIXTURE",
                )
            )
        day += timedelta(days=1)
    return ResearchInput(Snapshot(bars), SecurityHistory(states))


def policy():
    return AgentPolicy(
        logic=LogicSettings(
            champion=RankedStrategy(20), lookbacks=(2, 20), rankings=("momentum",)
        ),
        holdout_sessions=80,
    )


def test_currency_quantum_keeps_independent_ledger_identity():
    from momentum.execution.engine import Fill

    ledger = Ledger(D(1000000), money_quantum=D(1))
    day = date(2024, 1, 2)
    ledger.apply_fills(
        [Fill("A", "buy", 3, D(10001), D(5), "filled", day, day, "open_raw")], day
    )
    ledger.mark_to_market({"A": D(10010)}, day)
    assert ledger.nav == D(1000022)
    assert (
        ledger.cash + ledger.positions_value + ledger.dividend_receivable == ledger.nav
    )
    with pytest.raises(ValueError):
        Ledger(D(100), money_quantum=D(".03"))
    assert Ledger(D("123.5"), money_quantum=D("1.00")).cash == D(124)


def test_common_evaluator_prior_close_plan_and_missing_open():
    data = market(cycle=False)
    p = policy()
    spec = replace(p.logic.champion, min_momentum=D("-.1"))
    day = data.snapshot.sessions[100]
    rows = [
        replace(b, open=b.open / 2, low=b.low / 2) if b.session == day else b
        for b in data.snapshot.bars
    ]
    dropped = ResearchInput(Snapshot(rows), data.securities)
    result = CommonEvaluator(p).evaluate(
        dropped, spec, day, data.snapshot.sessions[102]
    )
    first = result.trade_log[0]
    assert D(first["open_price"]) == D(5000)
    assert first["fill_price_source"] == "open_raw_with_fixed_slippage"
    assert first["planned_quantity"] == 40  # 40% of 1m at the known 10,000 close
    assert first["quantity"] <= first["planned_quantity"]
    missing = ResearchInput(
        Snapshot([b for b in rows if b.session != day], require_complete=False),
        data.securities,
    )
    # Explicit session clock remains; no fabricated fill at the missing open.
    missing = replace(missing, sessions=data.sessions)
    absent = CommonEvaluator(p).evaluate(missing, spec, day, data.sessions[102])
    assert not any(t["session"] == day.isoformat() for t in absent.trade_log)
    assert absent.execution_events[0]["status"] == "cancelled_no_open"


def test_promotion_cache_forward_shadow_and_keep(tmp_path):
    data = market(460)
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("cycle", policy())
    first = agent.tick("cycle", data)
    assert first["cycles"][0]["research_champion_updated"]
    assert first["cycles"][0]["champion_after"]["lookback"] == 2
    assert first["orders_submitted"] == 0
    consumed = store.db.execute("SELECT count(*) FROM agent_intervals").fetchone()[0]
    again = agent.tick("cycle", data)
    assert again["cycles"][0]["cached"]
    assert (
        consumed
        == store.db.execute("SELECT count(*) FROM agent_intervals").fetchone()[0]
    )
    assert first["shadow"]["observations"] == policy().shadow_sessions
    assert first["shadow"]["research_revision"] == 1
    assert first["shadow"]["positions_continued"] is False
    second = agent.tick("cycle", data, max_cycles=2)
    assert second["cycles"][-1]["verdict"] == "KEEP_CHAMPION"
    assert second["status"]["revision"] == 1
    assert store.db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    store.close()


def test_no_improvement_keeps_champion_and_waits_for_reserved_data(tmp_path):
    data = market(230, cycle=False)
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("flat", replace(policy(), holdout_sessions=40))
    report = agent.tick("flat", data)
    assert report["cycles"][0]["verdict"] == "KEEP_CHAMPION"
    assert report["status"]["revision"] == 0
    assert report["waiting_for_data"]
    assert agent.tick("flat", market(240, cycle=False))["waiting_for_data"]
    changed = market(240, cycle=False)
    changed = replace(
        changed,
        snapshot=Snapshot(
            [replace(b, volume=b.volume + 1) for b in changed.snapshot.bars]
        ),
    )
    with pytest.raises(AgentError, match="frozen"):
        agent.tick("flat", changed)
    store.close()


def test_shared_consumption_survives_study_names_and_old_kis_usage(tmp_path):
    data = market()
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("one", policy())
    agent.tick("one", data)
    agent.init("two", policy())
    with pytest.raises(AgentError, match="consumed"):
        agent.tick("two", data)
    other = Store(tmp_path / "legacy.sqlite3")
    # A different symbol still invalidates the same market-wide final interval.
    other.db.execute(
        "INSERT INTO holdouts VALUES(?,?,?,?)",
        (
            "OLD_TICKER",
            data.sessions[190].isoformat(),
            data.sessions[200].isoformat(),
            "old-run",
        ),
    )
    imported = ResearchAgent(other)
    imported.init("legacy", policy())
    with pytest.raises(AgentError, match="consumed"):
        imported.tick("legacy", data)
    store.close()
    other.close()


def test_crash_after_evaluation_recovers_without_evaluating_again(
    tmp_path, monkeypatch
):
    data = market()
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("recover", policy())
    original = agent._finish
    monkeypatch.setattr(
        agent, "_finish", lambda _: (_ for _ in ()).throw(RuntimeError("process fault"))
    )
    with pytest.raises(RuntimeError):
        agent.tick("recover", data)
    cycle = store.db.execute("SELECT id FROM agent_cycles").fetchone()[0]
    monkeypatch.setattr(agent, "_finish", original)
    monkeypatch.setattr(
        CommonEvaluator,
        "evaluate",
        lambda *a, **k: pytest.fail("must not reevaluate holdout"),
    )
    recovered = agent.recover(cycle)
    assert recovered["research_champion_updated"]
    assert agent.recover(cycle) == recovered
    assert agent.status("recover")["revision"] == 1
    store.close()


def test_evaluation_fault_preserves_claim_and_prevents_replay(tmp_path, monkeypatch):
    data = market()
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("fault", policy())
    original = CommonEvaluator.evaluate
    holdout_start = data.sessions[186]

    def fail_holdout(self, data, spec, start, end, **kwargs):
        if start == holdout_start:
            raise RuntimeError("interrupted during final evaluation")
        return original(self, data, spec, start, end, **kwargs)

    monkeypatch.setattr(CommonEvaluator, "evaluate", fail_holdout)
    with pytest.raises(RuntimeError):
        agent.tick("fault", data)
    assert (
        store.db.execute(
            "SELECT count(*) FROM agent_intervals WHERE kind='holdout'"
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(AgentError, match="recover|interrupted"):
        agent.tick("fault", data)
    assert agent.status("fault")["revision"] == 0
    store.close()


def test_short_sample_does_not_force_a_promotion(tmp_path):
    store = Store(tmp_path / "short.sqlite3")
    agent = ResearchAgent(store)
    agent.init("short", replace(policy(), holdout_sessions=40))
    report = agent.tick("short", market())
    assert report["cycles"][0]["gates"]["net_improvement"]
    assert not report["cycles"][0]["gates"]["confidence"]
    assert not report["cycles"][0]["research_champion_updated"]
    store.close()


def test_manifest_frozen_before_holdout_and_gate_tampering_rejected(
    tmp_path, monkeypatch
):
    import json

    from trading_agent.contracts import canonical

    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("manifest", policy())
    original = agent._finish
    monkeypatch.setattr(
        agent,
        "_finish",
        lambda _: (_ for _ in ()).throw(RuntimeError("stop before promotion")),
    )
    with pytest.raises(RuntimeError):
        agent.tick("manifest", market())
    row = store.db.execute("SELECT * FROM agent_cycles").fetchone()
    assert row["manifest_json"] and row["evidence_json"]
    evidence = json.loads(row["evidence_json"])
    evidence["holdout"]["candidate_return"] = -1
    store.db.execute(
        "UPDATE agent_cycles SET evidence_json=? WHERE id=?",
        (canonical(evidence), row["id"]),
    )
    monkeypatch.setattr(agent, "_finish", original)
    with pytest.raises(AgentError, match="evidence|gate"):
        agent.recover(row["id"])
    assert agent.status("manifest")["revision"] == 0
    store.close()


def test_dividend_accrual_pay_date_and_split_preserve_nav():
    from trading_agent.contracts import Costs

    from momentum.agent.contracts import AnnouncedAction
    from momentum.ledger.ledger import CorporateAction

    data = market(cycle=False)
    p = policy()
    p = replace(p, logic=replace(p.logic, costs=Costs(D(0), D(0), D(0))))
    spec = replace(p.logic.champion, min_momentum=D("-.9"))
    ex, pay = data.sessions[101], data.sessions[103]
    dividend = AnnouncedAction(
        CorporateAction("FIXTURE", "cash_dividend", ex, pay, None, D(100)),
        session_time(data.sessions[99], 16),
    )
    rows = [
        replace(b, open=D(9900), high=D(9900), low=D(9900), close=D(9900))
        if b.session >= ex
        else b
        for b in data.snapshot.bars
    ]
    adjusted = replace(data, snapshot=Snapshot(rows), actions=(dividend,))
    r = CommonEvaluator(p).evaluate(
        adjusted, spec, data.sessions[100], data.sessions[104]
    )
    assert r.net_return == 0
    assert r.equity_curve[1]["receivable"] == "4000"
    assert r.equity_curve[3]["receivable"] == "0"
    assert all(
        D(row["cash"]) + D(row["positions_value"]) + D(row["receivable"])
        == D(row["equity"])
        for row in r.equity_curve
    )
    split = AnnouncedAction(
        CorporateAction("FIXTURE", "split", ex, None, D(2), None),
        session_time(data.sessions[99], 16),
    )
    rows = [
        replace(b, open=D(5000), high=D(5000), low=D(5000), close=D(5000))
        if b.session >= ex
        else b
        for b in data.snapshot.bars
    ]
    r = CommonEvaluator(p).evaluate(
        replace(data, snapshot=Snapshot(rows), actions=(split,)),
        spec,
        data.sessions[100],
        data.sessions[104],
    )
    assert r.net_return == 0
    assert r.equity_curve[1]["positions"]["FIXTURE"] == 80


def test_holdout_and_future_metadata_cannot_change_development_trials(tmp_path):
    import json

    data = market()
    p = policy()
    cutoff = data.sessions[186]
    future = replace(
        data,
        snapshot=Snapshot(
            [
                replace(b, volume=b.volume + 1000) if b.session >= cutoff else b
                for b in data.snapshot.bars
            ]
        ),
        securities=SecurityHistory(
            [
                replace(s, caution=True)
                if s.effective_at >= session_time(cutoff, 0)
                else s
                for s in data.securities.records
            ]
        ),
    )
    trials = []
    for i, inputs in enumerate((data, future)):
        store = Store(tmp_path / f"isolated-fixture-{i}.sqlite3")
        agent = ResearchAgent(store)
        agent.init("test", p)
        agent.tick("test", inputs)
        trials.append(
            json.loads(
                store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
            )["trials"]
        )
        store.close()
    assert trials[0] == trials[1]


def test_legacy_research_cannot_bypass_unified_claim_with_different_symbol(tmp_path):
    from trading_agent.contracts import Costs, StrategySpec
    from trading_agent.research import research

    data = market()
    store = Store(tmp_path / "shared.sqlite3")
    agent = ResearchAgent(store)
    agent.init("shared", policy())
    agent.tick("shared", data)
    shorter = Snapshot([replace(b, symbol="RENAMED") for b in data.snapshot.bars[:240]])
    with pytest.raises(AgentError, match="consumed unified"):
        research(shorter, StrategySpec(20), Costs(), store)
    store.close()


def test_imported_history_is_idempotent_and_blocks_new_studies(tmp_path):
    source = Store(tmp_path / "old.sqlite3")
    source.db.execute(
        "INSERT INTO holdouts VALUES(?,?,?,?)",
        ("REMOVED", "2024-09-20", "2024-09-25", "old"),
    )
    source.close()
    store = Store(tmp_path / "new.sqlite3")
    agent = ResearchAgent(store)
    first = agent.import_history(tmp_path / "old.sqlite3")
    assert first == agent.import_history(tmp_path / "old.sqlite3")
    assert store.db.execute("SELECT count(*) FROM agent_intervals").fetchone()[0] == 1
    agent.init("new", policy())
    with pytest.raises(AgentError, match="consumed"):
        agent.tick("new", market())
    store.close()


def test_watch_advances_only_with_new_reserved_data_and_survives_restart(tmp_path):
    from momentum.agent.cli import watch

    store = Store(tmp_path / "watch.sqlite3")
    agent = ResearchAgent(store)
    agent.init("watch", policy())
    inputs = iter((market(270), market(380)))
    pauses = []
    result = watch(
        agent,
        "watch",
        lambda: next(inputs),
        tmp_path / "output",
        max_cycles=2,
        max_ticks=2,
        pause=pauses.append,
    )
    assert len(result["cycles"]) == 2
    assert pauses == [30]
    assert (tmp_path / "output" / "agent.json").exists()
    store.close()
    reopened = Store(tmp_path / "watch.sqlite3")
    agent = ResearchAgent(reopened)
    result = agent.tick("watch", market(380), max_cycles=2)
    assert all(c["cached"] for c in result["cycles"])
    assert agent.status("watch")["revision"] == 1
    reopened.close()


def test_same_database_workers_cannot_register_same_final_window_twice(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "workers.sqlite3")
    other = Store(tmp_path / "workers.sqlite3")
    first, second = ResearchAgent(store), ResearchAgent(other)
    first.init("workers", policy())
    original = first._evaluate

    def competing(*args):
        with pytest.raises(AgentError, match="interrupted"):
            second.tick("workers", market())
        return original(*args)

    monkeypatch.setattr(first, "_evaluate", competing)
    first.tick("workers", market())
    assert first.status("workers")["revision"] == 1
    assert (
        store.db.execute(
            "SELECT count(*) FROM agent_intervals WHERE kind='holdout'"
        ).fetchone()[0]
        == 1
    )
    other.close()
    store.close()


def test_abandon_preserves_claim_and_requires_next_scheduled_window(
    tmp_path, monkeypatch
):
    data = market(460)
    store = Store(tmp_path / "abandon.sqlite3")
    agent = ResearchAgent(store)
    agent.init("abandon", policy())
    original = agent._evaluate
    monkeypatch.setattr(
        agent,
        "_evaluate",
        lambda *args: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError):
        agent.tick("abandon", data)
    cycle_id = store.db.execute("SELECT id FROM agent_cycles").fetchone()[0]
    assert agent.abandon(cycle_id, "Worker stopped; preserve consumed evaluation")[
        "claims_preserved"
    ]
    monkeypatch.setattr(agent, "_evaluate", original)
    result = agent.tick("abandon", data, max_cycles=2)
    assert result["cycles"][0]["ordinal"] == 1
    assert (
        store.db.execute(
            "SELECT count(*) FROM agent_intervals WHERE kind='holdout'"
        ).fetchone()[0]
        == 2
    )
    store.close()


def test_us_final_evaluation_cannot_be_enabled_by_policy():
    with pytest.raises(AgentError, match="H1-H4"):
        replace(policy(), market="US", currency="USD", money_quantum=".01")


def test_unpaid_dividend_counts_in_nav_targets_but_cannot_be_spent():
    from trading_agent.contracts import Costs

    from momentum.agent.contracts import AnnouncedAction
    from momentum.ledger.ledger import CorporateAction

    data = market(cycle=False)
    p = policy()
    # Isolate accounting under an artificial 50% distribution; the ordinary
    # volatility screen would correctly exclude this extreme fixture.
    p = replace(
        p,
        logic=replace(
            p.logic,
            costs=Costs(D(0), D(0), D(0)),
            selection=replace(p.logic.selection, max_daily_volatility=D(1)),
        ),
    )
    spec = replace(p.logic.champion, min_momentum=D("-.9"))
    ex, pay = data.sessions[101], data.sessions[104]
    action = AnnouncedAction(
        CorporateAction("FIXTURE", "cash_dividend", ex, pay, None, D(5000)),
        session_time(data.sessions[99], 16),
    )
    bars = [
        replace(b, open=D(5000), high=D(5000), low=D(5000), close=D(5000))
        if b.session >= ex
        else b
        for b in data.snapshot.bars
    ]
    data = replace(data, snapshot=Snapshot(bars), actions=(action,))
    result = CommonEvaluator(p).evaluate(
        data, spec, data.sessions[100], data.sessions[105]
    )
    assert result.equity_curve[2]["positions"]["FIXTURE"] == 80
    assert result.equity_curve[2]["receivable"] == "200000"
    assert all(D(r["cash"]) >= 0 for r in result.equity_curve)
    assert all(D(r["equity"]) == D(1000000) for r in result.equity_curve)


def test_explicit_session_clock_does_not_hide_a_market_wide_history_gap():
    data = market(cycle=False)
    data = replace(
        data,
        snapshot=Snapshot(
            [b for b in data.snapshot.bars if b.session != data.sessions[90]],
            require_complete=False,
        ),
    )
    p = policy()
    spec = replace(p.logic.champion, min_momentum=D("-.1"))
    result = CommonEvaluator(p).evaluate(
        data, spec, data.sessions[100], data.sessions[102]
    )
    assert result.trades == 0


def test_manifest_and_claim_exist_before_first_final_evaluation(tmp_path, monkeypatch):
    data = market()
    store = Store(tmp_path / "before.sqlite3")
    agent = ResearchAgent(store)
    agent.init("before", policy())
    original = CommonEvaluator.evaluate
    checks = []

    def inspect(self, inputs, spec, start, end, **kwargs):
        if start == data.sessions[186]:
            row = store.db.execute("SELECT * FROM agent_cycles").fetchone()
            assert row["status"] == "REGISTERED"
            frozen = store.db.execute(
                "SELECT manifest_json FROM evaluation_manifests WHERE run_id=?",
                (row["id"],),
            ).fetchone()
            assert frozen[0] == row["manifest_json"]
            assert (
                store.db.execute(
                    "SELECT count(*) FROM agent_intervals WHERE owner=? AND kind='holdout'",
                    (row["id"],),
                ).fetchone()[0]
                == 1
            )
            checks.append(True)
        return original(self, inputs, spec, start, end, **kwargs)

    monkeypatch.setattr(CommonEvaluator, "evaluate", inspect)
    agent.tick("before", data)
    assert len(checks) == 4
    store.close()
