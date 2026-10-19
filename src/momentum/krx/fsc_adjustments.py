"""Exchange base-price adjustments seen in collected FSC quotes (FIN-02a).

Raw closes jump on splits, bonus issues, rights issues and capital
reductions. The exchange then quotes ``vs`` (change) against an adjusted base
price, so ``clpr - vs`` differs from the previous traded close exactly on
those days. This module only *detects and reports* such days with the
adjustment factor ``previous close / adjusted base`` and the listed-share
ratio. It creates no corporate action: a detection is observed after the
event, so using it as the announcement would leak the future into decisions.
FIN-02b joins detections with prior OpenDART disclosures for that.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from momentum.config import SpecUndefinedError

from .fsc_collect import weekdays

# The adjusted base is an exchange tick-rounded price, so a split by r gives
# previous / base within one tick of r; 0.5% covers ticks on prices >= 1,000.
_RATIO_TOLERANCE = Decimal("0.005")


@dataclass(frozen=True)
class Adjustment:
    session: date
    symbol: str
    isin: str
    previous_session: date
    previous_close: Decimal
    adjusted_base: Decimal
    factor: Decimal
    share_ratio: Decimal | None
    kind: str  # SPLIT_LIKE, REVERSE_SPLIT_LIKE, OTHER

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key, item in value.items():
            if isinstance(item, date | Decimal):
                value[key] = str(item)
        return value


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool | float):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _rows(day_dir: Path, day: date) -> list[dict[str, Any]] | None:
    record_path = day_dir / "fetch.json"
    if not record_path.exists():
        return None
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("requested_date") != day.isoformat():
        raise SpecUndefinedError(f"{record_path}: requested_date mismatch")
    rows: list[dict[str, Any]] = []
    for page in record["pages"]:
        body = json.loads((day_dir / page["file"]).read_text(encoding="utf-8"))
        items = body["response"]["body"].get("items")
        item = items.get("item", []) if isinstance(items, dict) else []
        rows.extend([item] if isinstance(item, dict) else item)
    return rows


def _classify(factor: Decimal, share_ratio: Decimal | None) -> str:
    if share_ratio is None or share_ratio == 1:
        return "OTHER"
    for ratio, kind in (
        (share_ratio, "SPLIT_LIKE"),
        (1 / share_ratio, "REVERSE_SPLIT_LIKE"),
    ):
        whole = ratio.to_integral_value()
        if ratio > 1 and abs(ratio - whole) <= _RATIO_TOLERANCE * whole:
            target = whole if kind == "SPLIT_LIKE" else 1 / whole
            if abs(factor - target) <= _RATIO_TOLERANCE * target:
                return kind
    return "OTHER"


def detect(root: Path, start: date, end: date, kind: str = "stock") -> list[Adjustment]:
    """Scan collected days in order; uncollected weekdays break comparisons."""
    last: dict[str, tuple[date, Decimal, Decimal | None]] = {}
    found: list[Adjustment] = []
    for day in weekdays(start, end):
        rows = _rows(Path(root) / kind / day.isoformat(), day)
        if rows is None:
            # A gap could hide an adjustment day; never compare across it.
            last.clear()
            continue
        for row in rows:
            isin = str(row.get("isinCd") or "")
            close, change = _decimal(row.get("clpr")), _decimal(row.get("vs"))
            shares = _decimal(row.get("lstgStCnt"))
            volume = _decimal(row.get("trqu"))
            opening = _decimal(row.get("mkp"))
            if not isin or close is None or close <= 0:
                continue
            if volume == 0 and opening == 0:
                continue  # no-trade row: its close is carried forward, not traded
            previous = last.get(isin)
            if previous is not None and change is not None:
                prior_day, prior_close, prior_shares = previous
                base = close - change
                if base > 0 and base != prior_close:
                    factor = prior_close / base
                    share_ratio = (
                        shares / prior_shares
                        if shares and prior_shares and prior_shares > 0
                        else None
                    )
                    found.append(
                        Adjustment(
                            day,
                            str(row.get("srtnCd") or ""),
                            isin,
                            prior_day,
                            prior_close,
                            base,
                            factor.quantize(Decimal("0.000001")),
                            share_ratio.quantize(Decimal("0.000001"))
                            if share_ratio is not None
                            else None,
                            _classify(factor, share_ratio),
                        )
                    )
            last[isin] = (day, close, shares)
    return found
