"""Point-in-time disclosure events from saved OpenDART list pages only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from momentum.config import SpecUndefinedError

KST = ZoneInfo("Asia/Seoul")
# list.json provides a date, never a reception time. Conservatively prevent
# even intraday disclosures from entering that day's decision inputs.
DISCLOSURE_AVAILABLE_TIME = time(18, 0)
_REPORT_KINDS = (
    ("유무상증자결정", "BONUS_AND_RIGHTS"),
    ("무상증자결정", "BONUS_ISSUE"),
    ("유상증자결정", "RIGHTS_ISSUE"),
    ("감자결정", "CAPITAL_REDUCTION"),
    ("주식분할결정", "STOCK_SPLIT"),
    ("주식병합결정", "REVERSE_SPLIT"),
)


@dataclass(frozen=True)
class DisclosureEvent:
    rcept_no: str
    rcept_date: date
    corp_code: str
    corp_cls: str
    stock_code: str | None
    corp_name: str
    report_nm: str
    report_kind: str
    is_correction: bool
    remarks: str
    available_at: datetime
    source_file_sha256: str


@dataclass(frozen=True)
class AppendResult:
    added: int
    unchanged: int


def _text(row: dict[str, Any], key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _digits(value: str, count: int) -> str:
    if re.fullmatch(rf"[0-9]{{{count}}}", value) is None:
        raise ValueError(f"expected {count} digits: {value!r}")
    return value


def _parse_row(row: dict[str, Any], source_hash: str) -> DisclosureEvent:
    reception = _digits(_text(row, "rcept_no"), 14)
    reception_date = _digits(_text(row, "rcept_dt"), 8)
    day = date(int(reception_date[:4]), int(reception_date[4:6]), int(reception_date[6:]))
    if reception[:8] != reception_date:
        raise ValueError("rcept_no date differs from rcept_dt")
    corp_code = _digits(_text(row, "corp_code"), 8)
    corp_cls = _text(row, "corp_cls")
    if corp_cls not in {"Y", "K", "N", "E"}:
        raise ValueError(f"unknown corp_cls: {corp_cls!r}")
    stock_code = _text(row, "stock_code")
    if stock_code:
        _digits(stock_code, 6)
    report = _text(row, "report_nm")
    kind = next((kind for token, kind in _REPORT_KINDS if token in report), "OTHER")
    return DisclosureEvent(
        reception, day, corp_code, corp_cls, stock_code or None,
        _text(row, "corp_name"), report, kind,
        re.match(r"^\[[^\[\]]*정정\]", report) is not None,
        _text(row, "rm"), datetime.combine(day, DISCLOSURE_AVAILABLE_TIME, KST),
        source_hash,
    )


def parse_list_pages(paths: Sequence[Path]) -> list[DisclosureEvent]:
    events = []
    for path in paths:
        try:
            raw = path.read_bytes()
            page = json.loads(raw)
            if not isinstance(page, dict):
                raise TypeError("page must be an object")
            if page.get("status") == "013":
                continue
            if page.get("status") != "000":
                raise ValueError(f"OpenDART status: {page.get('status')!r}")
            rows = page["list"]
            if not isinstance(rows, list):
                raise TypeError("list must be an array")
            source_hash = hashlib.sha256(raw).hexdigest()
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("disclosure must be an object")
                events.append(_parse_row(row, source_hash))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SpecUndefinedError(f"{path}: {exc}") from exc
    return events


def _payload(event: DisclosureEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload["rcept_date"] = event.rcept_date.isoformat()
    payload["available_at"] = event.available_at.isoformat()
    return payload


def _content(event: DisclosureEvent) -> dict[str, Any]:
    payload = _payload(event)
    del payload["available_at"], payload["source_file_sha256"]
    return payload


class DisclosureLedger:
    """Append-only event contents; atomic replacement for a single writer.

    Callers must serialize writers to the same path. Replacement protects readers
    from partial batches; it is not a concurrent-writer locking protocol.
    """

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> tuple[bytes, list[DisclosureEvent]]:
        try:
            raw = self.path.read_bytes() if self.path.exists() else b""
            events = []
            for line in raw.splitlines():
                payload = json.loads(line)
                payload["rcept_date"] = date.fromisoformat(payload["rcept_date"])
                ts = datetime.fromisoformat(payload["available_at"])
                if ts.utcoffset() is None:
                    raise ValueError("available_at must be timezone-aware")
                payload["available_at"] = ts.astimezone(KST)
                events.append(DisclosureEvent(**payload))
            return raw, events
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SpecUndefinedError(f"{self.path}: {exc}") from exc

    def append(self, events: Iterable[DisclosureEvent]) -> AppendResult:
        raw, existing = self._read()
        by_id = {event.rcept_no: event for event in existing}
        added = []
        unchanged = 0
        # Validate the entire batch before touching the destination, including
        # duplicate IDs within the incoming batch.
        for event in events:
            previous = by_id.get(event.rcept_no)
            if previous is not None:
                if _content(previous) != _content(event):
                    raise SpecUndefinedError(
                        f"Disclosure changed after recording: {event.rcept_no}"
                    )
                unchanged += 1
            else:
                by_id[event.rcept_no] = event
                added.append(event)
        if added:
            lines = "".join(
                json.dumps(_payload(event), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n"
                for event in added
            ).encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as out:
                    temporary = Path(out.name)
                    out.write(raw)
                    if raw and not raw.endswith(b"\n"):
                        out.write(b"\n")
                    out.write(lines)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return AppendResult(len(added), unchanged)

    def as_of(self, ts: datetime) -> list[DisclosureEvent]:
        if ts.utcoffset() is None:
            raise SpecUndefinedError("as_of requires a timezone-aware datetime")
        return [event for event in self._read()[1] if event.available_at <= ts]

    def listed_only(self) -> list[DisclosureEvent]:
        return [event for event in self._read()[1] if event.stock_code is not None]
