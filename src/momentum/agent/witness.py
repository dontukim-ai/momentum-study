"""Durable consumption evidence independent of SQLite rollback and restore."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_agent.contracts import AgentError, aware, canonical, digest


class WitnessLog:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        try:
            if raw and not raw.endswith(b"\n"):
                raise ValueError("Incomplete record")
            records = []
            previous = "genesis"
            for seq, line in enumerate(raw.splitlines(), 1):
                row = json.loads(line)
                content = {
                    key: row[key]
                    for key in ("seq", "kind", "payload", "recorded_at", "prev_hash")
                }
                if (
                    type(row["seq"]) is not int
                    or row["seq"] != seq
                    or row["prev_hash"] != previous
                    or row["kind"] not in {"interval_claim", "commitment"}
                    or digest(content) != row["hash"]
                ):
                    raise ValueError("Invalid chain")
                previous = row["hash"]
                records.append(row)
            return records
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise AgentError("Witness log is corrupted") from exc

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        if kind not in {"interval_claim", "commitment"}:
            raise AgentError("Unsupported witness kind")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Callers append only inside the research DB's BEGIN IMMEDIATE write
        # transaction, which already serializes writers on every platform.
        # No OS file lock: fcntl is unavailable on Windows, where this runs.
        rows = self.read()
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            content = {
                "seq": len(rows) + 1,
                "kind": kind,
                "payload": payload,
                "recorded_at": aware(self.clock()).isoformat(),
                "prev_hash": rows[-1]["hash"] if rows else "genesis",
            }
            stream.write(canonical({**content, "hash": digest(content)}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
