"""Point-in-time annual financials from OpenDART (explore v7 §2, RES-05c).

Two sources, both saved untouched under ``<root>`` (kept out of Git):

* periodic-report list pages (``tools/dart_collect.py --type A``): every
  annual report filing with its reception date and the stock code it was
  filed under at the time;
* ``fnlttMultiAcnt`` answers per business year, 100 companies a request.

``fnlttMultiAcnt`` returns one reception number per company and year, the
latest filing (a correction or re-filing; probe 2026-10-26). User decision B
(explore v7 §2.1): a value is public from the ORIGINAL annual report's first
reception (18:00 KST, as every disclosure here), using the latest values. When
no original appears in the list, the API's own reception date is used and the
record says so. Missing accounts stay missing; nothing is filled in.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, quote_plus, urlencode

from momentum.agent.market import KRX

from .dart_collect import KEY_ENV
from .fsc_collect import CollectError, Http, http_get

MULTI_URL = "https://opendart.fss.or.kr/api/fnlttMultiAcnt.json"
ANNUAL_REPORT = "11011"
BATCH = 100
ACCOUNTS = {"자본총계": "equity", "자산총계": "assets", "영업이익": "operating_income"}
LISTED = ("Y", "K", "N")  # KOSPI, KOSDAQ, KONEX
# An original annual report title is exactly this; corrections and attachments
# carry a bracketed prefix ([기재정정], [첨부정정], [첨부추가]) and a deadline
# extension notice has a longer title.
_ORIGINAL = re.compile(r"사업보고서 \((\d{4})\.(\d{2})\)")
_ANY_ANNUAL = re.compile(r"(?:\[[^\]]+\])?사업보고서 \((\d{4})\.(\d{2})\)")


@dataclass(frozen=True)
class AnnualFiling:
    rcept_no: str
    rcept_date: date
    corp_code: str
    corp_cls: str
    stock_code: str | None
    period: str  # "YYYY-MM" of the fiscal year end
    original: bool


def _reception(rcept_no: str) -> date:
    if re.fullmatch(r"\d{14}", rcept_no) is None:
        raise ValueError(f"invalid rcept_no {rcept_no!r}")
    return date(int(rcept_no[:4]), int(rcept_no[4:6]), int(rcept_no[6:8]))


def annual_filings(pages: Iterable[Path]) -> list[AnnualFiling]:
    """Annual-report rows (originals and corrections) of saved A-type list pages."""
    filings = []
    for path in pages:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("list") or []:
            title = str(row.get("report_nm", "")).strip()
            match = _ANY_ANNUAL.fullmatch(title)
            if match is None:
                continue
            day = row["rcept_dt"]
            stock = (row.get("stock_code") or "").strip() or None
            filings.append(AnnualFiling(
                row["rcept_no"], date(int(day[:4]), int(day[4:6]), int(day[6:])), row["corp_code"],
                row["corp_cls"], stock, f"{match.group(1)}-{match.group(2)}",
                _ORIGINAL.fullmatch(title) is not None,
            ))
    return filings


def originals(filings: Iterable[AnnualFiling]) -> dict[tuple[str, str], AnnualFiling]:
    """Earliest original annual report per (corp_code, fiscal period)."""
    first: dict[tuple[str, str], AnnualFiling] = {}
    for f in filings:
        key = (f.corp_code, f.period)
        if f.original and (key not in first or f.rcept_no < first[key].rcept_no):
            first[key] = f
    return first


def listed_corps(filings: Iterable[AnnualFiling], year: int) -> list[str]:
    """Listed companies with any annual filing for a fiscal year ending in ``year``."""
    return sorted({f.corp_code for f in filings if f.period.startswith(f"{year}-") and f.corp_cls in LISTED})


class DartFinancialsFetcher:
    """Saves ``fnlttMultiAcnt`` answers as ``<root>/dart_fin/<year>/<batch>.json``.

    A batch file is named by the hash of its company list and never fetched
    again. ``013`` (no data) answers are saved too; other statuses stop.
    """

    def __init__(
        self,
        root: Path,
        *,
        environ: Mapping[str, str] | None = None,
        http: Http = http_get,
        sleep: Callable[[float], None] = time.sleep,
        pause: float = 1.0,
        progress: Callable[[str], None] = lambda message: None,
    ) -> None:
        key = (os.environ if environ is None else environ).get(KEY_ENV, "")
        if not key:
            raise CollectError(f"{KEY_ENV} is not set")
        self.root, self._key = Path(root), key
        self._forbidden = {v.encode() for v in (key, quote(key, safe=""), quote_plus(key))}
        self.http, self.sleep, self.pause, self.progress = http, sleep, pause, progress
        self._requested = False

    def batch_path(self, year: int, corps: Sequence[str]) -> Path:
        name = hashlib.sha256(",".join(corps).encode()).hexdigest()[:16]
        return self.root / "dart_fin" / str(year) / f"{name}.json"

    def _request(self, year: int, corps: Sequence[str]) -> bytes:
        if self._requested:
            self.sleep(self.pause)
        self._requested = True
        query = {"crtfc_key": self._key, "corp_code": ",".join(corps),
                 "bsns_year": str(year), "reprt_code": ANNUAL_REPORT}
        url = MULTI_URL + "?" + urlencode(query)
        for attempt in range(3):
            try:
                status, body = self.http(url)
                break
            except (URLError, OSError):
                if attempt == 2:
                    raise CollectError("network retries exhausted") from None
                self.sleep(float(2 ** (attempt + 1)))
            except Exception:  # noqa: BLE001 - transport text may contain the URL
                raise CollectError("transport failed") from None
        if any(secret in body for secret in self._forbidden):
            raise CollectError("response echoes the API key; not saved")
        if status != 200:
            raise CollectError(f"HTTP {status}")
        try:
            code = json.loads(body)["status"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CollectError("response is not an OpenDART answer") from exc
        if code not in ("000", "013"):
            raise CollectError(f"OpenDART status {code!r}")
        return body

    def fetch_year(self, year: int, corps: Sequence[str]) -> dict[str, int]:
        counts = {"fetched": 0, "skipped": 0}
        corps = sorted(corps)
        for start in range(0, len(corps), BATCH):
            chunk = corps[start:start + BATCH]
            path = self.batch_path(year, chunk)
            if path.exists():
                counts["skipped"] += 1
                continue
            body = self._request(year, chunk)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(json.dumps({"bsns_year": year, "corp_codes": chunk}).encode() + b"\n" + body)
            os.replace(temporary, path)
            counts["fetched"] += 1
            self.progress(f"{year} batch {start // BATCH + 1}: {len(body)} bytes")
        return counts


def read_batches(root: Path) -> list[dict[str, Any]]:
    """Every saved ``fnlttMultiAcnt`` row, with the batch's requested year."""
    rows = []
    for path in sorted((Path(root) / "dart_fin").glob("*/*.json")):
        header, _, body = path.read_bytes().partition(b"\n")
        year = json.loads(header)["bsns_year"]
        for row in json.loads(body).get("list") or []:
            rows.append({**row, "_requested_year": year})
    return rows


def _amount(text: Any) -> str | None:
    value = str(text or "").replace(",", "").strip()
    return value if re.fullmatch(r"-?\d+", value) else None


def _period_end(row: dict[str, Any]) -> date | None:
    found = re.findall(r"(\d{4})\.(\d{2})\.(\d{2})", str(row.get("thstrm_dt") or ""))
    return date(*map(int, found[-1])) if found else None


def build_records(
    rows: Iterable[dict[str, Any]], filings: Sequence[AnnualFiling]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """FinancialRecord dictionaries (kis-agent ``read_fundamentals`` JSONL) and a report."""
    first = originals(filings)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("reprt_code") != ANNUAL_REPORT:
            continue
        groups.setdefault((row["corp_code"], str(row["bsns_year"])), []).append(row)
    records: list[dict[str, Any]] = []
    report: dict[str, Any] = {"companies": 0, "fallback_api_reception": 0, "no_symbol": 0,
                           "no_period_end": 0, "statement": {"CFS": 0, "OFS": 0},
                           "missing": {name: 0 for name in ACCOUNTS.values()}, "fallbacks": []}
    seen: set[tuple[str, str, str]] = set()
    for (corp, _), group in sorted(groups.items()):
        # Batches fetched at different times may both hold a company; only
        # the latest filing's rows are its current values.
        latest = max(r["rcept_no"] for r in group)
        group = [r for r in group if r["rcept_no"] == latest]
        divs = {r.get("fs_div") for r in group}
        statement = "CFS" if "CFS" in divs else "OFS" if "OFS" in divs else None
        if statement is None:
            continue
        chosen = [r for r in group if r.get("fs_div") == statement]
        ends = {
            end
            for r in chosen
            if r.get("account_nm") in ("자산총계", "자본총계") and (end := _period_end(r)) is not None
        }
        if len(ends) != 1:
            report["no_period_end"] += 1
            continue
        period_end = ends.pop()
        report["companies"] += 1
        values: dict[str, str | None] = dict.fromkeys(ACCOUNTS.values())
        for r in chosen:
            if r.get("account_nm") in ACCOUNTS:
                values[ACCOUNTS[r["account_nm"]]] = _amount(r.get("thstrm_amount"))
        for name, value in values.items():
            if value is None:
                report["missing"][name] += 1
        api_rcept = latest
        original = first.get((corp, f"{period_end.year}-{period_end.month:02d}"))
        if original is None:
            report["fallback_api_reception"] += 1
            report["fallbacks"].append([corp, period_end.isoformat(), api_rcept])
        reception = original.rcept_date if original else _reception(api_rcept)
        symbol = (original.stock_code if original and original.stock_code
                  else (str(chosen[0].get("stock_code") or "").strip() or None))
        if symbol is None:
            report["no_symbol"] += 1
            continue
        key = (symbol, period_end.isoformat(), reception.isoformat())
        if key in seen:
            raise ValueError(f"duplicate financial record {key}")
        seen.add(key)
        report["statement"][statement] += 1
        records.append({
            "symbol": symbol,
            "period_end": period_end.isoformat(),
            "available_at": KRX.disclosure_at(reception).isoformat(),
            "statement": statement,
            **values,
            "source": f"dart-fnlttMultiAcnt:{api_rcept};original:{original.rcept_no if original else 'none'}",
        })
    records.sort(key=lambda r: (r["symbol"], r["period_end"]))
    return records, report


def write_records(records: Sequence[dict[str, Any]], out: Path, report: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "financials.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
