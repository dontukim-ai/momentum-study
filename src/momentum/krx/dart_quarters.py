"""v30: point-in-time quarterly operating income over assets (docs/blueprint/ma-cross-v20.md, v30).

Construction, fixed after inspecting the saved answers (Samsung 00126380, 2024) and before any price outcome:

* Each (company, business year, report) uses its latest filing's rows (as the annual records), consolidated
  (``CFS``) when the answer has any, else separate (``OFS``).
* Q1, Q2 and Q3 operating income is the interim answer's ``thstrm_amount``: the three months of that quarter
  (the half-year answer's ``thstrm_amount`` is the second quarter alone; the cumulative is
  ``thstrm_add_amount``). Q4 is the annual ``thstrm_amount`` less the Q3 answer's ``thstrm_add_amount`` (nine
  months), both from the same statement; nothing else stands in for a missing value.
* Assets are that quarter end's ``자산총계`` ``thstrm_amount``; the ratio needs them positive.
* Only December fiscal years: the balance-sheet date must end in March, June, September or December of the
  business year; anything else is counted and left out.
* A quarter is public from its original report's first reception, 18:00 (``KRX.disclosure_at``), the annual
  rule; with no original in the saved list, the answer's own reception (a later or equal date). Q4 is public
  when both the annual and the Q3 report are.
* Missing values stay missing; a symbol with two values for one quarter end is undefined.
"""

from __future__ import annotations

import calendar
import json
import re
import statistics
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

from .dart_financials import (
    ANNUAL_REPORT,
    QUARTERLY_REPORTS,
    AnnualFiling,
    _amount,
    _period_end,
    _reception,
    originals,
)

WINDOW, MINIMUM = 8, 6                           # last eight quarters, at least six present
STALE_DAYS = 200                                 # the latest public quarter ended at most this long before
_INTERIM = re.compile(r"(?:\[[^\]]+\])?(?:분기|반기)보고서 \((\d{4})\.(\d{2})\)")
_INTERIM_ORIGINAL = re.compile(r"(?:분기|반기)보고서 \((\d{4})\.(\d{2})\)")
END_MONTH = {**QUARTERLY_REPORTS, ANNUAL_REPORT: 12}
_KEEP = ("corp_code", "stock_code", "bsns_year", "reprt_code", "rcept_no", "fs_div", "account_nm", "thstrm_dt",
         "thstrm_amount", "thstrm_add_amount")


def interim_filings(pages: Iterable[Path]) -> list[AnnualFiling]:
    """Quarterly and half-year report rows (originals and corrections) of saved A-type list pages."""
    filings = []
    for path in pages:
        for row in json.loads(Path(path).read_text(encoding="utf-8")).get("list") or []:
            title = str(row.get("report_nm", "")).strip()
            match = _INTERIM.fullmatch(title)
            if match is None:
                continue
            day = row["rcept_dt"]
            filings.append(AnnualFiling(
                row["rcept_no"], date(int(day[:4]), int(day[4:6]), int(day[6:])), row["corp_code"],
                row["corp_cls"], (row.get("stock_code") or "").strip() or None,
                f"{match.group(1)}-{match.group(2)}", _INTERIM_ORIGINAL.fullmatch(title) is not None))
    return filings


def read_rows(root: Path) -> list[dict[str, Any]]:
    """Saved ``fnlttMultiAcnt`` rows, annual (``dart_fin``) and interim (``dart_finq``), slimmed for memory:
    the two accounts used here in full, every other account as one marker per statement (so a company's
    consolidated statement is still chosen whenever the answer has one)."""
    rows, markers = [], set()
    for path in sorted([*(Path(root) / "dart_fin").glob("*/*.json"), *(Path(root) / "dart_finq").glob("*/*.json")]):
        header, _, body = path.read_bytes().partition(b"\n")
        report = json.loads(header).get("reprt_code", ANNUAL_REPORT)
        for row in json.loads(body).get("list") or []:
            row = {k: row.get(k) for k in _KEEP} | {"reprt_code": row.get("reprt_code") or report}
            if row["account_nm"] in ("영업이익", "자산총계"):
                rows.append(row)
            else:
                markers.add(tuple((k, row[k]) for k in ("corp_code", "bsns_year", "reprt_code", "rcept_no",
                                                        "fs_div", "stock_code")))
    return rows + [dict(m) | {"account_nm": ""} for m in sorted(markers, key=str)]


def _report(group: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest = max(r["rcept_no"] for r in group)
    group = [r for r in group if r["rcept_no"] == latest]
    divs = {r.get("fs_div") for r in group}
    statement = "CFS" if "CFS" in divs else "OFS" if "OFS" in divs else None
    if statement is None:
        return None
    chosen = [r for r in group if r.get("fs_div") == statement]
    out: dict[str, Any] = {"rcept_no": latest, "statement": statement, "income": None, "cumulative": None,
                           "assets": None, "end": None, "stock": str(chosen[0].get("stock_code") or "").strip()}
    for r in chosen:
        if r.get("account_nm") == "영업이익":
            out["income"], out["cumulative"] = _amount(r.get("thstrm_amount")), _amount(r.get("thstrm_add_amount"))
        elif r.get("account_nm") == "자산총계":
            out["assets"], out["end"] = _amount(r.get("thstrm_amount")), _period_end(r)
    return out


def build_quarters(rows: Iterable[dict[str, Any]],
                   filings: Sequence[AnnualFiling]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One record per (symbol, quarter end): three-month operating income, assets, ratio and when public."""
    first = originals(filings)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("reprt_code") in END_MONTH:
            groups.setdefault((r["corp_code"], str(r["bsns_year"]), r["reprt_code"]), []).append(r)
    report: dict[str, Any] = {"quarters": 0, "fallback_api_reception": 0, "other_fiscal_year": 0, "no_symbol": 0,
                              "missing": {"operating_income": 0, "assets": 0}, "cumulative_mismatch": 0,
                              "statement": {"CFS": 0, "OFS": 0}}
    reports: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for (corp, year, code), group in sorted(groups.items()):
        found = _report(group)
        if found is None:
            continue
        end = found["end"]
        if end is None or end.year != int(year) or end.month != END_MONTH[code]:
            report["other_fiscal_year"] += 1
            continue
        original = first.get((corp, f"{end.year}-{end.month:02d}"))
        if original is None:
            report["fallback_api_reception"] += 1
        found["reception"] = original.rcept_date if original else _reception(found["rcept_no"])
        found["original"] = original.rcept_no if original else "none"
        found["symbol"] = (original.stock_code if original and original.stock_code else found["stock"]) or None
        reports.setdefault((corp, year), {})[code] = found
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (corp, _), by in sorted(reports.items()):
        q1, h1, q3 = by.get("11013"), by.get("11012"), by.get("11014")
        if q1 and h1 and q1["statement"] == h1["statement"] and None not in (q1["income"], h1["income"], h1["cumulative"]):
            report["cumulative_mismatch"] += int(q1["income"]) + int(h1["income"]) != int(h1["cumulative"])
        for code, found in by.items():
            income, sources = found["income"], [found]
            if code == ANNUAL_REPORT:
                ok = q3 is not None and q3["statement"] == found["statement"]
                income = (str(int(found["income"]) - int(q3["cumulative"]))
                          if ok and q3 and found["income"] is not None and q3["cumulative"] is not None else None)
                sources = [found, q3] if q3 else sources
            if income is None:
                report["missing"]["operating_income"] += 1
                continue
            if found["assets"] is None or int(found["assets"]) <= 0:
                report["missing"]["assets"] += 1
                continue
            if found["symbol"] is None:
                report["no_symbol"] += 1
                continue
            key = (found["symbol"], found["end"].isoformat())
            if key in out:
                raise SpecUndefinedError(f"two values for {key} ({corp} and {out[key]['corp_code']})")
            report["statement"][found["statement"]] += 1
            out[key] = {
                "symbol": found["symbol"], "corp_code": corp, "period_end": found["end"].isoformat(),
                "available_at": KRX.disclosure_at(max(s["reception"] for s in sources)).isoformat(),
                "statement": found["statement"], "operating_income": income, "assets": found["assets"],
                "ratio": float(Decimal(income) / Decimal(found["assets"])),
                "source": ";".join(f"{s['rcept_no']}/original:{s['original']}" for s in sources),
            }
    report["quarters"] = len(out)
    return [out[k] for k in sorted(out)], report


def quarter_ends(last: date, n: int) -> list[date]:
    """The ``n`` calendar quarter ends up to and including ``last``, oldest first."""
    ends, year, month = [], last.year, last.month
    for _ in range(n):
        ends.append(date(year, month, calendar.monthrange(year, month)[1]))
        year, month = (year, month - 3) if month > 3 else (year - 1, month + 9)
    return ends[::-1]


def volatility(quarters: Sequence[dict[str, Any]], at: datetime) -> float | None:
    """Standard deviation of the ratio over the last WINDOW quarter ends public at ``at``; None when short."""
    known = {date.fromisoformat(q["period_end"]): q["ratio"] for q in quarters
             if datetime.fromisoformat(q["available_at"]) <= at}
    if not known:
        return None
    last = max(known)
    if (at.date() - last).days > STALE_DAYS:
        return None
    values = [known[e] for e in quarter_ends(last, WINDOW) if e in known]
    return statistics.stdev(values) if len(values) >= MINIMUM else None
