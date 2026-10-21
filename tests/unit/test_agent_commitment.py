"""Economic and persistence regressions for the unified research runtime."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import AgentError, Bar, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings, ranked_spec
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput
from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.service import ResearchAgent


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


def staged(data, p):
    end = p.warmup_sessions + p.development_sessions - 1
    return replace(data.prefix(data.sessions[end]), calendar=data.sessions)


@pytest.mark.parametrize(
    "timing,grade",
    [("early", "E2"), ("execution", "E1"), ("open", "E1"), ("late", "E1")],
)
def test_commitment_grades_restart_and_budget(tmp_path, monkeypatch, timing, grade):
    data, p = market(), policy()
    start = p.warmup_sessions + p.development_sessions + p.embargo_sessions
    now = [
        session_time(data.sessions[start - 1], 18)
        if timing == "early"
        else session_time(data.sessions[start], 9)
        if timing == "execution"
        else session_time(data.sessions[start], 9, 30)
        if timing == "open"
        else session_time(data.sessions[-1], 19)
    ]
    store = Store(tmp_path / "commit.sqlite3")
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p)
    report = agent.tick("a", staged(data, p))
    assert report["waiting_for_data"]
    assert report["committed_cycle"]["evidence_grade_if_on_time"] == grade
    row = store.db.execute("SELECT * FROM agent_commitments").fetchone()
    import json

    payload = json.loads(row["payload_json"])
    assert store.db.execute("SELECT count(*) FROM agent_cycles").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM agent_intervals").fetchone()[0] == 1
    assert (
        report["status"]["budget"]["reserved"],
        report["status"]["budget"]["consumed"],
    ) == (1, 0)
    now[0] = session_time(data.sessions[-1], 19)
    agent = ResearchAgent(store, clock=lambda: now[0])
    monkeypatch.setattr(agent, "_develop", lambda *a: pytest.fail("must not reselect"))
    report = agent.tick("a", data)
    evidence = json.loads(
        store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
    )
    assert (
        evidence["selected_version"]
        == payload["development_evidence"]["selected_version"]
    )
    assert evidence["evidence_grade"] == grade
    assert report["cycles"][0]["evidence_grade"] == grade
    assert report["status"]["cycles"][0]["evidence_grade"] == grade
    assert report["status"]["budget"]["consumed"] == 1
    assert store.db.execute("SELECT count(*) FROM agent_commitments").fetchone()[0] == 1
    store.close()


def test_full_data_e1_preserves_promotion(tmp_path):
    data = market()
    store = Store(tmp_path / "commit.sqlite3")
    agent = ResearchAgent(store, clock=lambda: session_time(data.sessions[-1], 19))
    agent.init("a", policy())
    report = agent.tick("a", data)
    result = report["cycles"][0]
    assert result["evidence_grade"] == "E1"
    assert result["research_champion_updated"]
    assert result["champion_after"]["lookback"] == 2
    assert result["revision_after"] == 1
    assert all(result["gates"].values())
    store.close()


def test_calendar_contract_and_waiting(tmp_path):
    data, p = market(), policy()
    prefix = staged(data, p)
    assert (
        replace(data.prefix(prefix.sessions[-1]), calendar=data.calendar).calendar
        == data.sessions
    )
    assert prefix.prefix(prefix.sessions[-1]).calendar == prefix.sessions
    assert prefix.fingerprint(prefix.sessions[-1]) == replace(
        prefix, calendar=()
    ).fingerprint(prefix.sessions[-1])
    with pytest.raises(AgentError):
        replace(prefix, calendar=prefix.calendar[1:])
    with pytest.raises(AgentError):
        replace(prefix, calendar=tuple(reversed(prefix.calendar)))
    store = Store(tmp_path / "commit.sqlite3")
    agent = ResearchAgent(store, clock=lambda: session_time(prefix.sessions[-1], 19))
    agent.init("a", p)
    report = agent.tick("a", replace(prefix, calendar=()))
    assert report["waiting_for_data"]
    assert store.db.execute("SELECT count(*) FROM agent_commitments").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("change", ["development", "calendar", "holdout"])
def test_committed_inputs_and_candidate_are_frozen(tmp_path, monkeypatch, change):
    import json

    data, p = market(), policy()
    prefix = staged(data, p)
    now = [session_time(prefix.sessions[-1], 19)]
    store = Store(tmp_path / "commit.sqlite3")
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p)
    agent.tick("a", prefix)
    payload = json.loads(
        store.db.execute("SELECT payload_json FROM agent_commitments").fetchone()[0]
    )
    if change == "calendar":
        day = data.sessions[190]
        changed = ResearchInput(
            Snapshot([b for b in data.snapshot.bars if b.session != day]),
            data.securities,
        )
    elif change == "holdout":
        rows, states = [], []
        start = p.warmup_sessions + p.development_sessions + p.embargo_sessions
        close = data.snapshot.bars[start - 1].close
        for i, b in enumerate(data.snapshot.bars):
            if i >= start:
                opening = close
                close = (
                    close * (D(".99") if (i - start) % 4 < 2 else D("1.05"))
                ).quantize(D(1))
                b = replace(
                    b,
                    open=opening,
                    close=close,
                    high=max(opening, close),
                    low=min(opening, close),
                )
            rows.append(b)
        for state, b in zip(
            data.securities.visible(session_time(data.sessions[-1], 19)),
            rows,
            strict=True,
        ):
            states.append(
                replace(state, bid=b.close * D(".9995"), ask=b.close * D("1.0005"))
            )
        changed = replace(
            data, snapshot=Snapshot(rows), securities=SecurityHistory(states)
        )
        evaluator = CommonEvaluator(p)
        incumbent = evaluator.evaluate(
            changed,
            p.logic.champion,
            data.sessions[start],
            data.sessions[start + p.holdout_sessions - 1],
        )
        selected = evaluator.evaluate(
            changed,
            ranked_spec(payload["selected_spec"]),
            data.sessions[start],
            data.sessions[start + p.holdout_sessions - 1],
        )
        assert incumbent.net_return > selected.net_return
    else:
        changed = replace(
            data,
            snapshot=Snapshot(
                [
                    replace(b, close=b.close + D(1), high=b.high + D(1))
                    if b.session == data.sessions[100]
                    else b
                    for b in data.snapshot.bars
                ]
            ),
        )
    now[0] = session_time(data.sessions[-1], 19)
    if change != "holdout":
        with pytest.raises(
            AgentError,
            match="Exchange calendar changed after commitment"
            if change == "calendar"
            else "changed",
        ):
            agent.tick("a", changed)
    else:
        monkeypatch.setattr(
            agent, "_develop", lambda *a: pytest.fail("must not reselect")
        )
        agent.tick("a", changed)
        evidence = json.loads(
            store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
        )
        assert (
            evidence["selected_version"]
            == payload["development_evidence"]["selected_version"]
        )
        assert (
            evidence["holdout"]["candidate"]["strategy_version"]
            == evidence["selected_version"]
        )
    store.close()


def test_implicit_calendar_follows_explicit_observed_clock():
    data = market(200)
    missing = ResearchInput(
        Snapshot(
            [b for b in data.snapshot.bars if b.session != data.sessions[100]],
            require_complete=False,
        ),
        data.securities,
    )
    assert replace(missing, sessions=data.sessions).calendar == data.sessions
    explicit = replace(missing, calendar=tuple(missing.sessions))
    with pytest.raises(AgentError):
        replace(explicit, sessions=data.sessions)


def test_cli_reads_published_calendar(tmp_path, monkeypatch):
    import json

    from momentum.agent import cli

    data = market(200)
    prefix = data.prefix(data.sessions[180])
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps([d.isoformat() for d in data.sessions]))
    args = cli.parser().parse_args(["run", "--calendar", str(calendar_path)])
    assert args.calendar == calendar_path
    monkeypatch.setattr(cli, "read_csv", lambda *a, **k: prefix.snapshot)
    monkeypatch.setattr(cli, "read_security_history", lambda *a: prefix.securities)
    inputs = cli.read_input(
        tmp_path / "prices.csv", tmp_path / "securities.json", calendar=args.calendar
    )
    assert inputs.sessions == prefix.sessions
    assert inputs.calendar == data.sessions


@pytest.mark.parametrize("state", ["COMPLETE", "ABANDONED"])
def test_legacy_terminal_cycles_report_e1_without_backfill(tmp_path, state):
    from trading_agent.contracts import canonical

    store = Store(tmp_path / "legacy.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    store.db.execute(
        "INSERT INTO agent_cycles VALUES(?,?,?,?,?,?,?,?)",
        ("legacy", "a", 0, state, canonical({"policy": policy()}), None, None, "{}"),
    )
    before = tuple(store.db.execute("SELECT * FROM agent_cycles").fetchone())
    assert agent.status("a")["cycles"][0]["evidence_grade"] == "E1"
    assert tuple(store.db.execute("SELECT * FROM agent_cycles").fetchone()) == before
    assert store.db.execute("SELECT count(*) FROM agent_commitments").fetchone()[0] == 0
    store.close()


def test_commitment_without_challenger_and_market_reservation(tmp_path):
    import json

    data, p = market(cycle=False), policy()
    prefix = staged(data, p)
    now = [session_time(prefix.sessions[-1], 19)]
    store = Store(tmp_path / "commit.sqlite3")
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p)
    agent.tick("a", prefix)
    payload = json.loads(
        store.db.execute("SELECT payload_json FROM agent_commitments").fetchone()[0]
    )
    assert payload["selected_spec"] is None
    assert payload["selected_version"] is None
    agent.init("b", p)
    with pytest.raises(AgentError, match="consumed"):
        agent.tick("b", prefix)
    assert agent.status("a")["budget"]["reserved"] == 1
    assert store.db.execute("SELECT count(*) FROM agent_commitments").fetchone()[0] == 1
    now[0] = session_time(data.sessions[-1], 19)
    result = agent.tick("a", data)["cycles"][0]
    assert result["evidence_grade"] == "E2"
    assert result["verdict"] == "KEEP_CHAMPION"
    assert result["revision_after"] == 0
    store.close()


def test_watch_waits_for_reserved_last_cycle(tmp_path):
    from momentum.agent.cli import watch

    calls = []

    class Agent:
        def tick(self, *args, **kwargs):
            calls.append(True)
            return {
                "validation_budget_exhausted": True,
                "committed_cycle": {"ordinal": 1},
                "shadow": {"observations": 20},
                "status": {"policy": {"shadow_sessions": 20}},
            }

    watch(
        Agent(),
        "a",
        lambda: market(200),
        tmp_path,
        max_cycles=2,
        max_ticks=2,
        pause=lambda _: None,
    )
    assert len(calls) == 2


def test_interrupted_committed_cycle_status_retains_grade(tmp_path, monkeypatch):
    data, p = market(), policy()
    now = [session_time(data.sessions[180], 19)]
    store = Store(tmp_path / "commit.sqlite3")
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p)
    agent.tick("a", staged(data, p))
    now[0] = session_time(data.sessions[-1], 19)

    def interrupt(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(agent, "_evaluate", interrupt)
    with pytest.raises(RuntimeError):
        agent.tick("a", data)
    status = agent.status("a")
    assert status["cycles"][0]["status"] == "FAILED"
    assert status["cycles"][0]["evidence_grade"] == "E2"
    assert status["budget"]["consumed"] == 1
    store.close()
