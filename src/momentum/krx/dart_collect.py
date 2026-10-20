"""Read-only OpenDART disclosure-list collector (DATA-06b).

Stock splits and reverse splits are filed as exchange disclosures
(``pblntf_ty=I``), not as the B-type reports the probe samples, and there is
no structured API for them. This collector stores every list page of a
period so ``dart_events.parse_list_pages`` can classify them offline.

A period (at most 92 days, the list API's range) becomes one directory with
untouched pages and ``fetch.json``; it is written by renaming a temporary
directory only after every page arrived and ``total_count`` added up, and a
completed period is never fetched again. The key comes from the environment
and is never written; transport errors carry no URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote, quote_plus, urlencode

from .fsc_collect import CollectError, Http, http_get

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
KEY_ENV = "OPENDART_API_KEY"
PAGE_COUNT = 100
MAX_PAGES = 1000
MAX_PERIOD_DAYS = 92


@dataclass(frozen=True)
class PeriodResult:
    start: date
    end: date
    status: str  # "fetched", "skipped", "error"
    total_count: int | None = None
    message: str = ""


def three_months_later(day: date) -> date:
    """The list API's last allowed end: ``day`` plus 3 calendar months,
    clamped to the month's last day (probed: 2020-11-30 -> 2021-02-28)."""
    month = day.month + 2  # zero-based month index three months on
    year, month = day.year + month // 12, month % 12 + 1
    following = date(year + month // 12, month % 12 + 1, 1)
    return min(
        date(year, month, 1) + timedelta(days=day.day - 1),
        following - timedelta(days=1),
    )


def periods(
    start: date, end: date, days: int = MAX_PERIOD_DAYS
) -> Iterator[tuple[date, date]]:
    first = start
    while first <= end:
        last = min(end, first + timedelta(days=days - 1), three_months_later(first))
        yield first, last
        first = last + timedelta(days=1)


class DartListCollector:
    def __init__(
        self,
        root: Path,
        disclosure_type: str,
        *,
        environ: Mapping[str, str] | None = None,
        http: Http = http_get,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pause: float = 1.0,
        progress: Callable[[str], None] = lambda message: None,
    ) -> None:
        if len(disclosure_type) != 1 or not disclosure_type.isalpha():
            raise CollectError(f"invalid pblntf_ty: {disclosure_type!r}")
        key = (os.environ if environ is None else environ).get(KEY_ENV, "")
        if not key:
            raise CollectError(f"{KEY_ENV} is not set")
        self.root, self.kind, self._key = Path(root), disclosure_type.upper(), key
        self._forbidden = {
            v.encode() for v in (key, quote(key, safe=""), quote_plus(key))
        }
        self.http, self.sleep, self.clock = http, sleep, clock
        self.pause, self.progress = pause, progress
        self._requested = False

    def period_dir(self, start: date, end: date) -> Path:
        return (
            self.root
            / "dart_list"
            / self.kind
            / f"{start.isoformat()}_{end.isoformat()}"
        )

    def _request(self, start: date, end: date, page: int) -> dict[str, Any]:
        if self._requested:
            self.sleep(self.pause)
        self._requested = True
        query = {
            "crtfc_key": self._key,
            "bgn_de": start.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "pblntf_ty": self.kind,
            "page_count": PAGE_COUNT,
            "page_no": page,
        }
        url = LIST_URL + "?" + urlencode(query)
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
        self.progress(
            f"{self.kind} {start}~{end} page {page}: HTTP {status}, {len(body)} bytes"
        )
        if status != 200:
            raise CollectError(f"HTTP {status}")
        try:
            payload = json.loads(body)
            code = payload["status"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CollectError("response is not an OpenDART list page") from exc
        if code == "013":  # no data for the period
            return {"body": body, "total": 0, "pages": 1, "rows": 0}
        if code != "000":
            raise CollectError(f"OpenDART status {code!r}")
        rows = payload.get("list")
        if not isinstance(rows, list):
            raise CollectError("list must be an array")
        return {
            "body": body,
            "total": int(payload["total_count"]),
            "pages": max(1, int(payload["total_page"])),
            "rows": len(rows),
        }

    def collect_period(self, start: date, end: date) -> PeriodResult:
        if (end - start).days + 1 > MAX_PERIOD_DAYS or end < start:
            return PeriodResult(start, end, "error", message="period exceeds 92 days")
        target = self.period_dir(start, end)
        if (target / "fetch.json").exists():
            return PeriodResult(start, end, "skipped")
        temporary = target.with_name(f".{target.name}.partial")
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            first = self._request(start, end, 1)
            if first["pages"] > MAX_PAGES:
                raise CollectError(f"{first['pages']} pages exceed {MAX_PAGES}")
            fetched = [first] + [
                self._request(start, end, n) for n in range(2, first["pages"] + 1)
            ]
            if sum(p["rows"] for p in fetched) != first["total"] or any(
                p["total"] != first["total"] for p in fetched
            ):
                raise CollectError("page rows do not add up to total_count")
            temporary.mkdir(parents=True)
            records = []
            for number, page in enumerate(fetched, 1):
                name = f"page-{number}.json"
                (temporary / name).write_bytes(page["body"])
                records.append(
                    {
                        "file": name,
                        "sha256": hashlib.sha256(page["body"]).hexdigest(),
                        "rows": page["rows"],
                    }
                )
            record = {
                "endpoint": LIST_URL,
                "pblntf_ty": self.kind,
                "bgn_de": start.isoformat(),
                "end_de": end.isoformat(),
                "received_at": self.clock().isoformat(),
                "total_count": first["total"],
                "pages": records,
            }
            (temporary / "fetch.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, target)
        except CollectError as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            return PeriodResult(start, end, "error", message=str(exc))
        return PeriodResult(start, end, "fetched", first["total"])

    def collect(self, start: date, end: date) -> list[PeriodResult]:
        results = []
        for first, last in periods(start, end):
            result = self.collect_period(first, last)
            results.append(result)
            if result.status == "error":
                break
        return results


def list_pages(root: Path, disclosure_type: str = "I") -> list[Path]:
    """Every saved list page of completed periods, in period/page order."""
    base = Path(root) / "dart_list" / disclosure_type.upper()
    pages: list[Path] = []
    for period in sorted(p for p in base.glob("*_*") if (p / "fetch.json").exists()):
        record = json.loads((period / "fetch.json").read_text(encoding="utf-8"))
        pages.extend(period / page["file"] for page in record["pages"])
    return pages


DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"


class DartDocumentFetcher:
    """Saves ``document.xml`` zips as ``<root>/dart_docs/<rcept_no>.zip``.

    A saved document is never fetched again. Error answers come back as a
    small JSON or XML status instead of a zip and are refused, never saved.
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
        self.root, self._key = Path(root) / "dart_docs", key
        self._forbidden = {
            v.encode() for v in (key, quote(key, safe=""), quote_plus(key))
        }
        self.http, self.sleep, self.pause, self.progress = http, sleep, pause, progress
        self._requested = False

    def path(self, rcept_no: str) -> Path:
        return self.root / f"{rcept_no}.zip"

    def fetch(self, rcept_no: str) -> str:
        """Returns "fetched" or "skipped"; raises CollectError otherwise."""
        if len(rcept_no) != 14 or not rcept_no.isdigit():
            raise CollectError(f"invalid rcept_no: {rcept_no!r}")
        target = self.path(rcept_no)
        if target.exists():
            return "skipped"
        if self._requested:
            self.sleep(self.pause)
        self._requested = True
        url = (
            DOCUMENT_URL
            + "?"
            + urlencode({"crtfc_key": self._key, "rcept_no": rcept_no})
        )
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
        self.progress(f"document {rcept_no}: HTTP {status}, {len(body)} bytes")
        if status != 200 or body[:2] != b"PK":
            excerpt = " ".join(body[:200].decode("utf-8", "replace").split())
            raise CollectError(f"not a document zip (HTTP {status}): {excerpt}")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.partial")
        temporary.write_bytes(body)
        os.replace(temporary, target)
        return "fetched"


COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"


class DartCompanyFetcher:
    """Saves 기업개황 answers as ``<root>/dart_company/<corp_code>.json``.

    Its ``corp_name`` is the registered legal name (에스케이하이닉스(주)),
    which subsidiary dividend filings use, while list rows carry the short
    listing name (SK하이닉스); checked 2026-10-20. Only status 000 is saved.
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
        self.root, self._key = Path(root) / "dart_company", key
        self._forbidden = {
            v.encode() for v in (key, quote(key, safe=""), quote_plus(key))
        }
        self.http, self.sleep, self.pause, self.progress = http, sleep, pause, progress
        self._requested = False

    def path(self, corp_code: str) -> Path:
        return self.root / f"{corp_code}.json"

    def fetch(self, corp_code: str) -> str:
        """Returns "fetched" or "skipped"; raises CollectError otherwise."""
        if len(corp_code) != 8 or not corp_code.isdigit():
            raise CollectError(f"invalid corp_code: {corp_code!r}")
        target = self.path(corp_code)
        if target.exists():
            return "skipped"
        if self._requested:
            self.sleep(self.pause)
        self._requested = True
        url = (
            COMPANY_URL
            + "?"
            + urlencode({"crtfc_key": self._key, "corp_code": corp_code})
        )
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
        self.progress(f"company {corp_code}: HTTP {status}, {len(body)} bytes")
        try:
            payload = json.loads(body)
            code = payload["status"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CollectError(f"company {corp_code}: not a company answer") from exc
        if status != 200 or code != "000" or not payload.get("corp_name"):
            raise CollectError(f"company {corp_code}: HTTP {status}, status {code!r}")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.partial")
        temporary.write_bytes(body)
        os.replace(temporary, target)
        return "fetched"


def legal_names(root: Path) -> dict[str, str]:
    """corp_code -> registered legal name from saved 기업개황 answers."""
    names = {}
    for path in sorted((Path(root) / "dart_company").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        names[path.stem] = payload["corp_name"]
    return names


DETAIL_URL = "https://opendart.fss.or.kr/api/{api}.json"
DETAIL_APIS = ("fricDecsn", "crDecsn")


class DartDetailFetcher:
    """Saves one 주요사항보고서 detail answer per filing.

    The detail APIs answer by company and reception-date range, so a filing
    is requested with its own ``rcept_dt`` as both ends and stored as
    ``<root>/dart_detail/<api>/<rcept_no>.json``. Only status 000 answers are
    saved; a saved answer is never fetched again.
    """

    def __init__(
        self,
        root: Path,
        api: str,
        *,
        environ: Mapping[str, str] | None = None,
        http: Http = http_get,
        sleep: Callable[[float], None] = time.sleep,
        pause: float = 1.0,
        progress: Callable[[str], None] = lambda message: None,
    ) -> None:
        if api not in DETAIL_APIS:
            raise CollectError(f"unsupported detail API: {api!r}")
        key = (os.environ if environ is None else environ).get(KEY_ENV, "")
        if not key:
            raise CollectError(f"{KEY_ENV} is not set")
        self.api, self.root, self._key = api, Path(root) / "dart_detail" / api, key
        self._forbidden = {
            v.encode() for v in (key, quote(key, safe=""), quote_plus(key))
        }
        self.http, self.sleep, self.pause, self.progress = http, sleep, pause, progress
        self._requested = False

    def path(self, rcept_no: str) -> Path:
        return self.root / f"{rcept_no}.json"

    def fetch(self, rcept_no: str, corp_code: str, rcept_date: date) -> str:
        """Returns "fetched" or "skipped"; raises CollectError otherwise."""
        if len(rcept_no) != 14 or not rcept_no.isdigit():
            raise CollectError(f"invalid rcept_no: {rcept_no!r}")
        if len(corp_code) != 8 or not corp_code.isdigit():
            raise CollectError(f"invalid corp_code: {corp_code!r}")
        target = self.path(rcept_no)
        if target.exists():
            return "skipped"
        if self._requested:
            self.sleep(self.pause)
        self._requested = True
        day = rcept_date.strftime("%Y%m%d")
        query = {"crtfc_key": self._key, "corp_code": corp_code, "bgn_de": day, "end_de": day}
        url = DETAIL_URL.format(api=self.api) + "?" + urlencode(query)
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
        self.progress(f"{self.api} {rcept_no}: HTTP {status}, {len(body)} bytes")
        try:
            payload = json.loads(body)
            code = payload["status"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CollectError(f"{self.api} {rcept_no}: not a detail answer") from exc
        if status != 200 or code != "000":
            raise CollectError(f"{self.api} {rcept_no}: HTTP {status}, status {code!r}")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.partial")
        temporary.write_bytes(body)
        os.replace(temporary, target)
        return "fetched"
