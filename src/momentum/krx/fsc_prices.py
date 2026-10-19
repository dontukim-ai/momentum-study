"""Price CSV rows from saved FSC (data.go.kr) daily quote pages only.

The converter never repairs supplier values: an empty or zero price is copied
as-is so that the DATA-05a row screen quarantines it with a reason code. What
it does refuse is an input whose completeness cannot be shown, because a page
missing from a day would silently drop symbols from that session.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

SOURCES = {"stock": "fsc-stock-price", "etf": "fsc-etf-price"}
# Field names from the FSC guide (ETF V2) and the 2026-10-19 probe responses.
_PRICE_FIELDS = (
    ("open", "mkp"),
    ("high", "hipr"),
    ("low", "lopr"),
    ("close", "clpr"),
    ("volume", "trqu"),
)
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


@dataclass(frozen=True)
class SecurityIdentity:
    session: date
    symbol: str
    isin: str
    name: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        value = asdict(self)
        value["session"] = self.session.isoformat()
        return value


@dataclass(frozen=True)
class Conversion:
    rows: list[dict[str, str]]
    identities: list[SecurityIdentity]
    pages: list[dict[str, Any]]


def _session(value: Any) -> date:
    text = _scalar(value, "basDt")
    if re.fullmatch(r"[0-9]{8}", text) is None:
        raise SpecUndefinedError(f"basDt must be YYYYMMDD: {text!r}")
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def _scalar(value: Any, field: str) -> str:
    # Numbers may arrive as JSON numbers or strings; floats would hide
    # rounding, so only integers are converted and anything else is refused.
    if value is None:
        return ""
    if isinstance(value, bool | float):
        raise SpecUndefinedError(f"{field} has an unsupported JSON type")
    if isinstance(value, int | str):
        return str(value).strip()
    raise SpecUndefinedError(f"{field} has an unsupported JSON type")


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items")
    if items in (None, ""):
        return []
    if not isinstance(items, dict):
        raise SpecUndefinedError("items must be an object")
    item = items.get("item", [])
    batch = [item] if isinstance(item, dict) else item
    if not isinstance(batch, list) or not all(isinstance(row, dict) for row in batch):
        raise SpecUndefinedError("item must be an object or a list of objects")
    return batch


def _load(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
        response = payload["response"]
        header, body = response["header"], response["body"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SpecUndefinedError(f"{path.name}: not an FSC JSON response") from exc
    if str(header.get("resultCode")) != "00":
        raise SpecUndefinedError(
            f"{path.name}: supplier resultCode {header.get('resultCode')!r}"
        )
    if not isinstance(body, dict):
        raise SpecUndefinedError(f"{path.name}: body must be an object")
    return body, hashlib.sha256(raw).hexdigest()


def convert_pages(paths: Sequence[Path], kind: str) -> Conversion:
    """Convert every page of one or more sessions; each session must be complete."""
    if kind not in SOURCES:
        raise SpecUndefinedError(f"unknown FSC price kind: {kind!r}")
    sessions: dict[date, dict[str, Any]] = {}
    rows: list[dict[str, str]] = []
    identities: dict[tuple[date, str], SecurityIdentity] = {}
    pages = []
    for path in sorted(paths):
        body, digest = _load(path)
        batch = _items(body)
        try:
            page_no, total = int(body["pageNo"]), int(body["totalCount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SpecUndefinedError(f"{path.name}: pageNo/totalCount missing") from exc
        days = {_session(item.get("basDt")) for item in batch}
        if len(days) > 1:
            raise SpecUndefinedError(f"{path.name}: one page must hold one basDt")
        pages.append(
            {"file": path.name, "sha256": digest, "page": page_no, "rows": len(batch)}
        )
        if not days:
            continue
        day = days.pop()
        seen = sessions.setdefault(day, {"total": total, "pages": set(), "rows": 0})
        if seen["total"] != total or page_no in seen["pages"]:
            raise SpecUndefinedError(
                f"{path.name}: inconsistent or repeated page for {day}"
            )
        seen["pages"].add(page_no)
        seen["rows"] += len(batch)
        source = f"{SOURCES[kind]}:{digest}"
        available = KRX.historical_bar_at(day).isoformat()
        for item in batch:
            symbol = _scalar(item.get("srtnCd"), "srtnCd")
            isin = _scalar(item.get("isinCd"), "isinCd")
            row = {"session": day.isoformat(), "symbol": symbol}
            for name, field in _PRICE_FIELDS:
                row[name] = _scalar(item.get(field), field)
            row.update(available_at=available, source=source)
            rows.append(row)
            if _ISIN.fullmatch(isin) is None:
                continue  # the row screen still sees the row; identity stays unknown
            identity = SecurityIdentity(
                day, symbol, isin, _scalar(item.get("itmsNm"), "itmsNm"), kind
            )
            previous = identities.setdefault((day, symbol), identity)
            if previous.isin != isin:
                raise SpecUndefinedError(f"{day} {symbol}: two ISINs in one session")
    for day, seen in sorted(sessions.items()):
        if seen["rows"] != seen["total"]:
            raise SpecUndefinedError(
                f"{day}: {seen['rows']} of {seen['total']} rows; fetch every page"
            )
    return Conversion(
        rows, sorted(identities.values(), key=lambda i: (i.session, i.symbol)), pages
    )


def identity_changes(identities: Iterable[SecurityIdentity]) -> list[dict[str, str]]:
    """Report symbol reuse and ISIN code changes across sessions (no merging)."""
    by_symbol: dict[str, SecurityIdentity] = {}
    by_isin: dict[str, SecurityIdentity] = {}
    changes = []
    for identity in sorted(identities, key=lambda i: i.session):
        before = by_symbol.get(identity.symbol)
        if before is not None and before.isin != identity.isin:
            changes.append(
                {
                    "kind": "SYMBOL_REUSED",
                    "symbol": identity.symbol,
                    "from_isin": before.isin,
                    "to_isin": identity.isin,
                    "session": identity.session.isoformat(),
                }
            )
        moved = by_isin.get(identity.isin)
        if moved is not None and moved.symbol != identity.symbol:
            changes.append(
                {
                    "kind": "SYMBOL_CHANGED",
                    "isin": identity.isin,
                    "from_symbol": moved.symbol,
                    "to_symbol": identity.symbol,
                    "session": identity.session.isoformat(),
                }
            )
        by_symbol[identity.symbol] = identity
        by_isin[identity.isin] = identity
    return changes
