"""Preregistered observation checkpoint regressions on synthetic inputs."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import AgentError, Bar, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput, policy_from_dict
from momentum.agent.service import ResearchAgent


def market(count=360, *, cycle=True, start=date(2024, 1, 2)):
    bars, states = [], []
    day, close = start, D(10000)
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


def test_policy_defaults_and_legacy_json():
    p = policy_from_dict({})
    assert p.observation_checkpoint_sessions == 20
    assert p.degradation_reference == "equal_weight_eligible"
    assert p.degradation_underperformance == 0.03
    assert p.degradation_drawdown is None
    assert p.demotion_checkpoints == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("observation_checkpoint_sessions", 1),
        ("observation_checkpoint_sessions", True),
        ("degradation_reference", "market_etf"),
        ("degradation_reference", []),
        ("degradation_underperformance", 0),
        ("degradation_underperformance", float("nan")),
        ("degradation_drawdown", 1),
        ("demotion_checkpoints", 0),
    ],
)
def test_invalid_degradation_policy(field, value):
    with pytest.raises(AgentError, match="Invalid preregistered degradation policy"):
        replace(policy(), **{field: value})


def rows(store):
    return [
        dict(r)
        for r in store.db.execute(
            "SELECT * FROM agent_observation_checkpoints ORDER BY seq"
        )
    ]


def test_checkpoint_boundaries_idempotency_and_read_purity(tmp_path):
    data = market(283)  # observation starts at index 266: 2 * 7 + 3 sessions
    store = Store(tmp_path / "checkpoints.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", replace(policy(), observation_checkpoint_sessions=7))
    report = agent.tick("a", data)
    saved = rows(store)
    assert [r["seq"] for r in saved] == [0, 1]
    assert report["observation_checkpoints"]["stored"] == 2
    before = list(store.db.iterdump())
    assert agent.observation_ledger("a", data) == report["observation"]
    assert list(store.db.iterdump()) == before
    assert agent.tick("a", data)["observation_checkpoints"]["stored"] == 0
    assert rows(store) == saved
    store.close()


def install_decline(monkeypatch):
    from momentum.agent.evaluator import CommonEvaluator

    original = CommonEvaluator.evaluate_schedule

    def declining(self, data, schedule, start, end):
        result = original(self, data, schedule, start, end)
        capital = self.policy.logic.selection.capital_limit
        curve = tuple(
            {**row, "equity": str(capital * D("0.98") ** (i + 1))}
            for i, row in enumerate(result.equity_curve)
        )
        return replace(result, equity_curve=curve)

    monkeypatch.setattr(CommonEvaluator, "evaluate_schedule", declining)


def test_persistent_breach_memory_and_no_demotion(tmp_path, monkeypatch):
    install_decline(monkeypatch)
    store = Store(tmp_path / "decline.sqlite3")
    agent = ResearchAgent(store)
    p = replace(
        policy(), observation_checkpoint_sessions=5, degradation_reference="cash"
    )
    agent.init("a", p)
    report = agent.tick("a", market(284))
    saved = rows(store)
    assert [r["state"] for r in saved] == ["WARNING", "WARNING", "DEMOTION_CANDIDATE"]
    payload = json.loads(saved[-1]["payload_json"])
    assert payload["consecutive_breaches"] == 3
    assert payload["demotion_target"]["revision"] == 0
    assert report["status"]["registry"]["current_revision"] == 1
    assert report["status"]["revision"] == 1
    assert report["status"]["observation_state"]["state"] == "DEMOTION_CANDIDATE"
    memories = [
        m
        for m in agent.development_memory()
        if m["source_cycle_id"] in {r["checkpoint_id"] for r in saved}
    ]
    assert len(memories) == 3
    assert {m["failure_code"] for m in memories} == {
        "TRACKING_WARNING",
        "TRACKING_DEMOTION_CANDIDATE",
    }
    store.close()


def test_window_nav_calculation(tmp_path):
    from trading_agent.logic import ranked_spec

    from momentum.agent.evaluator import CommonEvaluator

    store = Store(tmp_path / "nav.sqlite3")
    agent = ResearchAgent(store)
    p = replace(policy(), observation_checkpoint_sessions=7)
    agent.init("a", p)
    data = market(284)
    observation = agent.tick("a", data)["observation"]
    start = date.fromisoformat(observation["start"])
    schedule = []
    for entry in observation["schedule"]:
        row = store.db.execute(
            "SELECT spec_json FROM agent_champion_registry WHERE revision=?",
            (entry["revision"],),
        ).fetchone()
        schedule.append(
            (
                date.fromisoformat(entry["effective_from"]),
                ranked_spec(json.loads(row[0])),
            )
        )
    for row in rows(store):
        end = date.fromisoformat(row["window_end"])
        evaluator = CommonEvaluator(p)
        result = evaluator.evaluate_schedule(data.prefix(end), schedule, start, end)
        benchmark = evaluator.evaluate_benchmark(
            data.prefix(end), p.degradation_reference, start, end
        )
        offset = row["seq"] * 7

        def window(curve, offset=offset):
            base = (
                D(curve[offset - 1]["equity"])
                if offset
                else p.logic.selection.capital_limit
            )
            peak, dd = base, D(0)
            for point in curve[offset : offset + 7]:
                nav = D(point["equity"])
                peak = max(peak, nav)
                dd = max(dd, 1 - nav / peak)
            return float(nav / base - 1), float(dd)

        payload = json.loads(row["payload_json"])
        assert (payload["window_return"], payload["window_drawdown"]) == window(
            result.equity_curve
        )
        assert payload["reference_return"] == window(benchmark.equity_curve)[0]
    store.close()


def test_checkpoint_changed_is_isolated(tmp_path):
    data = market(294)
    store = Store(tmp_path / "changed.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", replace(policy(), observation_checkpoint_sessions=7))
    agent.tick("a", data)
    saved = rows(store)
    bars = list(data.snapshot.bars)
    bars[286] = replace(
        bars[286], close=bars[286].close + D(1), high=bars[286].high + D(1)
    )
    changed = replace(data, snapshot=Snapshot(bars))
    report = agent.tick("a", changed)
    assert (
        report["observation_checkpoints"]["error"]
        == "Stored observation checkpoint changed"
    )
    assert rows(store) == saved
    assert report["status"]["cycles"][0]["status"] == "COMPLETE"
    store.close()


def test_cohort_sealed_memory_is_released_without_updating_checkpoint(
    tmp_path, monkeypatch
):
    install_decline(monkeypatch)
    data = market(480)
    p = replace(
        policy(), observation_checkpoint_sessions=5, degradation_reference="cash"
    )
    store = Store(tmp_path / "sealed.sqlite3")
    now = [session_time(data.sessions[281], 17)]
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p)

    def tick(index, **kwargs):
        now[0] = session_time(data.sessions[index], 17)
        return agent.tick(
            "a",
            replace(data.prefix(data.sessions[index]), calendar=data.calendar),
            **kwargs,
        )

    tick(281)
    previous = store.db.execute("SELECT * FROM agent_cycles").fetchone()
    plan = agent._plan(data, p, previous)
    tick(plan["dev_end"], max_cycles=2)
    report = tick(plan["holdout_start"] + 10, max_cycles=2)
    saved = [r for r in rows(store) if r["sealed"]]
    assert saved
    latest = report["observation_checkpoints"]["latest"]
    assert latest["sealed"] is True
    assert "window_return" not in latest
    assert "demotion_target" not in report["status"]["observation_state"]
    for row in saved:
        assert not store.db.execute(
            "SELECT 1 FROM agent_failure_snapshots WHERE source_cycle_id=?",
            (row["checkpoint_id"],),
        ).fetchone()
    report = tick(plan["holdout_end"], max_cycles=2)
    assert report["status"]["cycles"][1]["status"] == "COMPLETE"
    for row in saved:
        assert (
            dict(
                store.db.execute(
                    "SELECT * FROM agent_observation_checkpoints WHERE checkpoint_id=?",
                    (row["checkpoint_id"],),
                ).fetchone()
            )
            == row
        )
        assert store.db.execute(
            "SELECT 1 FROM agent_failure_snapshots JOIN agent_memory_releases "
            "USING(snapshot_id) WHERE source_cycle_id=?",
            (row["checkpoint_id"],),
        ).fetchone()
    assert report["observation_checkpoints"]["latest"]["sealed"] is False
    assert "window_return" in report["status"]["observation_state"]
    store.close()


@pytest.mark.parametrize(
    "rate,threshold,state",
    [
        ("0.999", 0.03, "OBSERVING"),
        ("0.98", 0.03, "WARNING"),
        ("0.98", 0.03, "DEMOTION_CANDIDATE"),
    ],
)
def test_preregistered_threshold_and_first_breach(
    tmp_path, monkeypatch, rate, threshold, state
):
    from momentum.agent.evaluator import CommonEvaluator

    original = CommonEvaluator.evaluate_schedule

    def curve(self, data, schedule, start, end):
        result = original(self, data, schedule, start, end)
        return replace(
            result,
            equity_curve=tuple(
                {
                    **row,
                    "equity": str(
                        self.policy.logic.selection.capital_limit * D(rate) ** (i + 1)
                    ),
                }
                for i, row in enumerate(result.equity_curve)
            ),
        )

    monkeypatch.setattr(CommonEvaluator, "evaluate_schedule", curve)
    store = Store(tmp_path / "threshold.sqlite3")
    agent = ResearchAgent(store)
    agent.init(
        "a",
        replace(
            policy(),
            observation_checkpoint_sessions=5,
            degradation_reference="cash",
            degradation_underperformance=threshold,
            demotion_checkpoints=1 if state == "DEMOTION_CANDIDATE" else 3,
        ),
    )
    agent.tick("a", market(271))
    assert rows(store)[0]["state"] == state
    if state == "OBSERVING":
        assert json.loads(rows(store)[0]["payload_json"])["difference"] < 0
    store.close()


def test_revision_change_resets_breach_count(tmp_path, monkeypatch):
    install_decline(monkeypatch)
    store = Store(tmp_path / "revision.sqlite3")
    agent = ResearchAgent(store)
    p = replace(
        policy(), observation_checkpoint_sessions=5, degradation_reference="cash"
    )
    agent.init("a", p)
    data = market(281)
    observation = agent.tick("a", data.prefix(data.sessions[275]))["observation"]
    assert [r["state"] for r in rows(store)] == ["WARNING", "WARNING"]
    row = dict(
        store.db.execute(
            "SELECT * FROM agent_champion_registry WHERE revision=1"
        ).fetchone()
    )
    row.update(revision=2, previous_revision=1)
    store.db.execute(
        "INSERT INTO agent_champion_registry VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        tuple(row.values()),
    )
    observation["end"] = data.sessions[-1].isoformat()
    observation["schedule"].append(
        {
            "revision": 2,
            "spec_version": row["spec_version"],
            "effective_from": data.sessions[276].isoformat(),
        }
    )
    result = agent._isolated_checkpoints("a", data, p, observation)
    assert "error" not in result
    last = rows(store)[-1]
    assert last["state"] == "WARNING"
    assert json.loads(last["payload_json"])["consecutive_breaches"] == 1
    store.close()


def test_no_previous_revision_has_null_demotion_target(tmp_path, monkeypatch):
    install_decline(monkeypatch)
    store = Store(tmp_path / "baseline.sqlite3")
    agent = ResearchAgent(store)
    agent.init(
        "a",
        replace(
            policy(),
            observation_checkpoint_sessions=5,
            degradation_reference="cash",
            demotion_checkpoints=1,
        ),
    )
    agent.tick("a", market(271, cycle=False))
    payload = json.loads(rows(store)[0]["payload_json"])
    assert payload["revision"] == 0
    assert payload["demotion_target"] is None
    assert rows(store)[0]["state"] == "DEMOTION_CANDIDATE"
    store.close()


def test_exposure_failure_does_not_store_checkpoints(tmp_path, monkeypatch):
    store = Store(tmp_path / "exposure.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", replace(policy(), observation_checkpoint_sessions=5))
    claim = agent._claim

    def fail_exposure(policy, start, end, owner, kind, data):
        if kind == "observation_exposure":
            raise AgentError("exposure refused")
        return claim(policy, start, end, owner, kind, data)

    monkeypatch.setattr(agent, "_claim", fail_exposure)
    report = agent.tick("a", market(281))
    assert report["observation_checkpoints"] == {
        "stored": 0,
        "latest": None,
        "error": "exposure refused",
    }
    assert rows(store) == []
    store.close()


@pytest.mark.parametrize(
    "observation", [None, {"status": "unavailable", "error": "fixture"}]
)
def test_unavailable_observation_has_no_checkpoint_report(
    tmp_path, monkeypatch, observation
):
    store = Store(tmp_path / "unavailable.sqlite3")
    agent = ResearchAgent(store)
    agent.init("a", policy())
    monkeypatch.setattr(agent, "_isolated_observation", lambda name, data: observation)
    report = agent.tick("a", market(271))
    assert report["observation"] == observation
    assert report["observation_checkpoints"] is None
    assert rows(store) == []
    store.close()
