"""Single research worker per study: lease, heartbeat and stop reasons (OPS-01a).

The lease lives in the research SQLite database rather than in a PID file:
probing a PID is not portable (on Windows ``os.kill(pid, 0)`` sends CTRL_C).
A worker that stops renewing is presumed dead after three intervals (minimum
two minutes); a new worker may then take over and records whom it replaced.

A tick can run for hours (the 8-cycle explore run spent 8.7 h in one tick), so
while it runs a separate connection renews the lease every interval (OPS-01b);
otherwise the lease looked expired after two minutes and a second worker could
have taken over a study that was still being written.

Failures are never retried here. An interrupted final evaluation needs an
operator decision (``recover`` or ``abandon``), and any other error stops the
worker with its reason recorded, so a restart is always a deliberate act.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_agent.contracts import AgentError, aware, canonical
from trading_agent.store import Store

LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_worker_leases (
 study TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER NOT NULL,
 acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL);
"""
_INTERRUPTED = "Cycle interrupted"


def lease_seconds(interval: float) -> float:
    return max(3 * interval, 120)


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(canonical(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class WorkerMonitor:
    def __init__(
        self,
        store: Store,
        study: str,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        interval: float = 30,
    ) -> None:
        self.store, self.db, self.study, self.path = store, store.db, study, Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interval = interval
        self.ttl = timedelta(seconds=lease_seconds(interval))
        self.owner = uuid.uuid4().hex
        self.db.executescript(LEASE_SCHEMA)
        self.state: dict[str, Any] = {}
        # The renewal thread and the main thread both rewrite the state file.
        self._lock = threading.Lock()

    def _now(self) -> datetime:
        return aware(self.clock())

    def start(self) -> None:
        now = self._now()
        took_over = None
        with self.store.transaction():
            row = self.db.execute(
                "SELECT * FROM agent_worker_leases WHERE study=?", (self.study,)
            ).fetchone()
            if row is not None:
                heartbeat = datetime.fromisoformat(row["heartbeat_at"])
                if now - heartbeat < self.ttl:
                    raise AgentError(
                        f"Another worker holds this study; last heartbeat {row['heartbeat_at']}"
                    )
                took_over = row["owner"]
            self.db.execute(
                "INSERT INTO agent_worker_leases VALUES(?,?,?,?,?) ON CONFLICT(study) DO UPDATE SET "
                "owner=excluded.owner,pid=excluded.pid,acquired_at=excluded.acquired_at,"
                "heartbeat_at=excluded.heartbeat_at",
                (self.study, self.owner, os.getpid(), now.isoformat(), now.isoformat()),
            )
        self.state = {
            "study": self.study,
            "owner": self.owner,
            "pid": os.getpid(),
            "interval_seconds": self.interval,
            "started_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "took_over_from": took_over,
            "state": "STARTING",
            "ticks": 0,
            "wait_reason": None,
            "last_input": None,
            "last_ok_at": None,
            "last_error": None,
            "operator_action": None,
            "orders_submitted": 0,
        }
        with self._lock:
            _write(self.path, self.state)

    def _renew(self, now: datetime) -> None:
        with self.store.transaction():
            row = self.db.execute(
                "SELECT owner FROM agent_worker_leases WHERE study=?", (self.study,)
            ).fetchone()
            if row is None or row["owner"] != self.owner:
                raise AgentError(
                    "Worker lease was taken over by another worker; stopping"
                )
            self.db.execute(
                "UPDATE agent_worker_leases SET heartbeat_at=? WHERE study=? AND owner=?",
                (now.isoformat(), self.study, self.owner),
            )

    def heartbeat(self, result: dict[str, Any], data: Any = None) -> None:
        now = self._now()
        self._renew(now)
        last_input: dict[str, Any] | None = self.state.get("last_input")
        if data is not None and getattr(data, "sessions", None):
            last = data.sessions[-1]
            last_input = {
                "last_session": last.isoformat(),
                "fingerprint": data.fingerprint(last),
            }
            if result.get("input_bundle"):
                last_input["bundle_id"] = result["input_bundle"].get("bundle_id")
        with self._lock:
            self._after_tick(now, result, last_input)

    def _after_tick(
        self, now: datetime, result: dict[str, Any], last_input: dict[str, Any] | None
    ) -> None:
        waiting = bool(result.get("waiting_for_data"))
        self.state.update(
            heartbeat_at=now.isoformat(),
            ticks=self.state.get("ticks", 0) + (1 if result else 0),
            state="WAITING_FOR_DATA" if waiting else "RUNNING",
            wait_reason=(
                f"{result.get('next_window_sessions_required')} more sessions required "
                "for the next final window"
                if waiting
                else None
            ),
            last_input=last_input,
            last_ok_at=now.isoformat() if result else self.state.get("last_ok_at"),
        )
        _write(self.path, self.state)

    @contextmanager
    def beating(self) -> Iterator[None]:
        """Keep the lease alive while long work runs in this thread."""
        if self.store.path == ":memory:":
            yield
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=self._beat, args=(stop,), name="worker-heartbeat", daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def _beat(self, stop: threading.Event) -> None:
        # sqlite3 connections belong to one thread; WAL lets this short update
        # interleave with the main connection's transactions.
        db = sqlite3.connect(self.store.path, isolation_level=None, timeout=10)
        try:
            while not stop.wait(self.interval):
                now = self._now()
                try:
                    renewed = db.execute(
                        "UPDATE agent_worker_leases SET heartbeat_at=? WHERE study=? AND owner=?",
                        (now.isoformat(), self.study, self.owner),
                    ).rowcount
                except sqlite3.Error:
                    continue  # Busy: the next pulse retries well within the lease.
                if renewed != 1:
                    return  # Taken over; the main thread's next renewal stops it.
                with self._lock:
                    self.state.update(
                        heartbeat_at=now.isoformat(),
                        state="RUNNING"
                        if self.state.get("state") == "STARTING"
                        else self.state.get("state"),
                    )
                    _write(self.path, self.state)
        finally:
            db.close()

    def _release(self) -> None:
        with self.store.transaction():
            self.db.execute(
                "DELETE FROM agent_worker_leases WHERE study=? AND owner=?",
                (self.study, self.owner),
            )

    def stopped(self, state: str) -> None:
        self._release()
        with self._lock:
            self.state.update(state=state, heartbeat_at=self._now().isoformat())
            _write(self.path, self.state)

    def failed(self, error: BaseException) -> None:
        if isinstance(error, KeyboardInterrupt):
            self.stopped("STOPPED_BY_OPERATOR")
            return
        interrupted = str(error).startswith(_INTERRUPTED)
        try:
            self._release()
        finally:
            with self._lock:
                self.state.update(
                    state="NEEDS_OPERATOR" if interrupted else "STOPPED_ERROR",
                    heartbeat_at=self._now().isoformat(),
                    last_error={
                        "type": type(error).__name__,
                        "message": str(error),
                        "at": self._now().isoformat(),
                    },
                    operator_action=(
                        "An interrupted final evaluation must not be re-run blindly: "
                        "run `momentum-agent recover --cycle-id ID` if its evidence is "
                        "complete, or `momentum-agent abandon --cycle-id ID --reason ...` "
                        "after stopping the worker."
                        if interrupted
                        else None
                    ),
                )
                _write(self.path, self.state)


def worker_status(
    store: Store, study: str, path: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Read-only view for operators: stored lease plus the last state file."""
    store.db.executescript(LEASE_SCHEMA)
    row = store.db.execute(
        "SELECT * FROM agent_worker_leases WHERE study=?", (study,)
    ).fetchone()
    state = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else None
    current = aware(now or datetime.now(UTC))
    age = None
    if state and state.get("heartbeat_at"):
        age = int((current - datetime.fromisoformat(state["heartbeat_at"])).total_seconds())
    return {
        "study": study,
        "lease": dict(row) if row else None,
        "worker": state,
        "heartbeat_age_seconds": age,
        "stale": bool(row)
        and age is not None
        and age > lease_seconds((state or {}).get("interval_seconds", 30)),
    }
