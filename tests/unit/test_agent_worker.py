from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta

import pytest
from trading_agent.contracts import AgentError
from trading_agent.store import Store

from momentum.agent.cli import watch
from momentum.agent.worker import WorkerMonitor, worker_status


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 19, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Data:
    def __init__(self, last: date) -> None:
        self.sessions = (date(2026, 1, 2), last)

    def fingerprint(self, end: date) -> str:
        return f"fp-{end.isoformat()}"


def result(*, waiting: bool = True, exhausted: bool = False, **extra) -> dict:
    return {
        "study": "s",
        "cycles": [],
        "shadow": None,
        "waiting_for_data": waiting,
        "next_window_sessions_required": 7,
        "validation_budget_exhausted": exhausted,
        "status": {"policy": {"shadow_sessions": 20}},
        **extra,
    }


class Agent:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def tick(self, study, data, *, max_cycles):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def monitor(tmp_path, store, clock, study="s"):
    return WorkerMonitor(store, study, tmp_path / "out" / "worker.json", clock=clock, interval=30)


def read(tmp_path) -> dict:
    return json.loads((tmp_path / "out" / "worker.json").read_text(encoding="utf-8"))


def test_heartbeat_state_last_input_and_wait_reason(tmp_path):
    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    agent = Agent([result(), result(waiting=False, input_bundle={"bundle_id": "b1"})])
    days = iter((date(2026, 3, 2), date(2026, 3, 3)))
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        state = read(tmp_path)
        assert state["state"] == "WAITING_FOR_DATA"
        assert state["wait_reason"] == "7 more sessions required for the next final window"
        assert state["last_input"] == {"last_session": "2026-03-02", "fingerprint": "fp-2026-03-02"}
        clock.now += timedelta(seconds=seconds)

    watch(
        agent,
        "s",
        lambda: Data(next(days)),
        tmp_path / "out",
        max_cycles=1,
        max_ticks=2,
        pause=pause,
        monitor=monitor(tmp_path, store, clock),
    )
    state = read(tmp_path)
    assert state["state"] == "STOPPED"
    assert state["ticks"] == 2 and state["pid"] == os.getpid()
    assert state["last_input"]["bundle_id"] == "b1"
    assert state["heartbeat_at"] == clock.now.isoformat()
    assert state["last_error"] is None
    # The lease is released on a clean stop.
    assert worker_status(store, "s", tmp_path / "out" / "worker.json")["lease"] is None


def test_only_one_live_worker_per_study_and_stale_lease_takeover(tmp_path):
    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    first = monitor(tmp_path, store, clock)
    first.start()
    other = Store(tmp_path / "db.sqlite3")
    second = monitor(tmp_path, other, clock)
    with pytest.raises(AgentError, match="Another worker holds"):
        second.start()
    # A lease is stale after three intervals without a heartbeat (minimum 120 s).
    clock.now += timedelta(seconds=121)
    second.start()
    assert read(tmp_path)["took_over_from"] == first.owner
    with pytest.raises(AgentError, match="lease was taken over"):
        first.heartbeat({})


def test_interrupted_evaluation_needs_operator_and_is_not_retried(tmp_path):
    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    agent = Agent(
        [
            AgentError(
                "Cycle interrupted; recover stored evidence or explicitly abandon after stopping the worker"
            ),
            result(),
        ]
    )
    with pytest.raises(AgentError, match="interrupted"):
        watch(
            agent,
            "s",
            lambda: Data(date(2026, 3, 2)),
            tmp_path / "out",
            max_cycles=1,
            max_ticks=5,
            pause=lambda s: None,
            monitor=monitor(tmp_path, store, clock),
        )
    assert agent.calls == 1
    state = read(tmp_path)
    assert state["state"] == "NEEDS_OPERATOR"
    assert "recover" in state["operator_action"] and "abandon" in state["operator_action"]
    assert state["last_error"]["type"] == "AgentError"
    assert worker_status(store, "s", tmp_path / "out" / "worker.json")["lease"] is None


def test_other_failures_stop_without_retry(tmp_path):
    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    agent = Agent([AgentError("Input bundle integrity failure"), result()])
    with pytest.raises(AgentError):
        watch(
            agent,
            "s",
            lambda: Data(date(2026, 3, 2)),
            tmp_path / "out",
            max_cycles=1,
            max_ticks=5,
            pause=lambda s: None,
            monitor=monitor(tmp_path, store, clock),
        )
    assert agent.calls == 1
    state = read(tmp_path)
    assert state["state"] == "STOPPED_ERROR"
    assert state["last_error"]["message"] == "Input bundle integrity failure"
    assert state["operator_action"] is None


def test_finished_when_budget_and_observation_complete(tmp_path):
    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    done = result(waiting=False, exhausted=True, shadow={"observations": 20})
    watch(
        Agent([done]),
        "s",
        lambda: Data(date(2026, 3, 2)),
        tmp_path / "out",
        max_cycles=1,
        pause=lambda s: None,
        monitor=monitor(tmp_path, store, clock),
    )
    assert read(tmp_path)["state"] == "FINISHED"


def test_worker_status_reports_stale_heartbeat(tmp_path):
    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    worker = monitor(tmp_path, store, clock)
    worker.start()
    clock.now += timedelta(seconds=500)
    status = worker_status(store, "s", tmp_path / "out" / "worker.json", now=clock.now)
    assert status["lease"]["owner"] == worker.owner
    assert status["heartbeat_age_seconds"] == 500
    assert status["stale"] is True


def test_watch_without_monitor_is_unchanged(tmp_path):
    result_ = watch(
        Agent([result()]),
        "s",
        lambda: Data(date(2026, 3, 2)),
        tmp_path / "out",
        max_cycles=1,
        max_ticks=1,
        pause=lambda s: None,
    )
    assert result_["waiting_for_data"] is True
    assert not (tmp_path / "out" / "worker.json").exists()


def test_cli_run_records_worker_state_and_status(tmp_path, monkeypatch):
    from test_agent import market
    from trading_agent.data import write_csv
    from trading_agent.selection import write_security_history

    from momentum.agent import cli

    class FixedClockAgent(cli.ResearchAgent):
        def __init__(self, store, **kwargs):
            super().__init__(store, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    monkeypatch.setattr(cli, "ResearchAgent", FixedClockAgent)
    data = market(230)
    write_csv(data.snapshot, tmp_path / "prices.csv")
    write_security_history(data.securities, tmp_path / "securities.jsonl")
    common = ["--db", str(tmp_path / "db.sqlite3"), "--output", str(tmp_path / "out")]
    cli.run(cli.parser().parse_args(["init", *common]))
    args = [
        "--csv",
        str(tmp_path / "prices.csv"),
        "--securities",
        str(tmp_path / "securities.jsonl"),
    ]
    cli.run(cli.parser().parse_args(["run", *common, *args]))
    status = cli.run(cli.parser().parse_args(["worker-status", *common]))
    assert status["lease"] is None
    assert status["worker"]["state"] in {"STOPPED", "FINISHED"}
    assert status["worker"]["ticks"] == 1
    assert status["worker"]["last_input"]["last_session"] == data.sessions[-1].isoformat()


def test_operator_interrupt_is_a_clean_stop(tmp_path):
    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    with pytest.raises(KeyboardInterrupt):
        watch(
            Agent([KeyboardInterrupt()]),
            "s",
            lambda: Data(date(2026, 3, 2)),
            tmp_path / "out",
            max_cycles=1,
            pause=lambda s: None,
            monitor=monitor(tmp_path, store, clock),
        )
    state = read(tmp_path)
    assert state["state"] == "STOPPED_BY_OPERATOR" and state["last_error"] is None
    assert worker_status(store, "s", tmp_path / "out" / "worker.json")["lease"] is None


def test_lease_is_renewed_while_a_long_tick_runs(tmp_path):
    # OPS-01b: the 8-cycle explore worker spent 8.7 h in one tick with its
    # heartbeat frozen at start, so its lease looked expired after 2 minutes.
    import time

    store, clock = Store(tmp_path / "db.sqlite3"), Clock()
    first = WorkerMonitor(
        store, "s", tmp_path / "out" / "worker.json", clock=clock, interval=0.05
    )
    seen = {}

    class SlowAgent:
        def tick(self, study, data, *, max_cycles):
            clock.now += timedelta(minutes=3)
            time.sleep(0.4)
            other = Store(tmp_path / "db.sqlite3")
            try:
                with pytest.raises(AgentError, match="Another worker holds"):
                    WorkerMonitor(
                        other, "s", tmp_path / "other.json", clock=clock, interval=0.05
                    ).start()
            finally:
                other.close()
            seen["state"] = read(tmp_path)
            return result()

    watch(
        SlowAgent(),
        "s",
        lambda: Data(date(2026, 3, 2)),
        tmp_path / "out",
        max_cycles=1,
        max_ticks=1,
        pause=lambda s: None,
        monitor=first,
    )
    assert seen["state"]["state"] == "RUNNING"
    assert seen["state"]["heartbeat_at"] == clock.now.isoformat()
    assert read(tmp_path)["state"] == "STOPPED"
    assert worker_status(store, "s", tmp_path / "out" / "worker.json")["lease"] is None


@pytest.mark.parametrize("name", ["SIGTERM", "SIGINT"])
def test_stop_signals_mean_operator_stop_even_when_ignored(name):
    import signal

    from momentum.agent.cli import install_stop_signals

    number = getattr(signal, name)
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        # `nohup ... &` starts the worker with SIGINT ignored.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        install_stop_signals()
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), number)
            for _ in range(100):  # Delivery happens between bytecodes.
                pass
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)
