"""Read-only daily collector for FSC (data.go.kr) V2 quotes (DATA-06a).

One directory per requested date holds the untouched supplier pages and a
``fetch.json`` record (requested date, receipt time, totalCount, page hashes).
A date is written only after every page arrived and checked out, by renaming a
temporary directory, so a crash never leaves a half-written day behind, and a
completed day is never fetched or overwritten again. The requested date is
recorded because an empty response (holiday, or not yet published) carries no
``basDt`` of its own; the calendar builder needs that fact.

The service key is read from the environment and never written: responses
that echo it are refused and errors carry no URLs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import Request, urlopen

ENDPOINTS = {
    "stock": "https://apis.data.go.kr/1160100/GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2",
    "etf": "https://apis.data.go.kr/1160100/GetSecuritiesProductInfoService_V2/getETFPriceInfo_V2",
}
KEY_ENV = "DATA_GO_KR_SERVICE_KEY"
PAGE_ROWS = 1000
MAX_PAGES = 20
REQUEST_DEADLINE_SECONDS = 60.0

Http = Callable[[str], tuple[int, bytes]]


class CollectError(Exception):
    """A day could not be collected; the message never contains the key or URL."""


@dataclass(frozen=True)
class DayResult:
    day: date
    status: str  # "fetched", "skipped", "error"
    total_count: int | None = None
    message: str = ""


def http_get(url: str) -> tuple[int, bytes]:
    """Socket timeouts do not bound DNS or trickling servers; add a deadline."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            with urlopen(Request(url), timeout=30) as response:
                outcome["value"] = (response.status, response.read())
        except HTTPError as exc:
            with exc:
                outcome["value"] = (exc.code, exc.read())
        except BaseException as exc:  # noqa: BLE001 - re-raised below without the URL
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(REQUEST_DEADLINE_SECONDS)
    if worker.is_alive():
        raise TimeoutError("request deadline exceeded")
    if "error" in outcome:
        raise outcome["error"]
    value: tuple[int, bytes] = outcome["value"]
    return value


def weekdays(start: date, end: date) -> Iterator[date]:
    # KRX has no weekend sessions; weekdays are requested and the supplier's
    # answer, not this filter, decides which of them were sessions.
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


class Collector:
    def __init__(
        self,
        root: Path,
        kind: str,
        *,
        environ: Mapping[str, str] | None = None,
        http: Http = http_get,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        pause: float = 1.0,
        progress: Callable[[str], None] = lambda message: None,
    ) -> None:
        if kind not in ENDPOINTS:
            raise CollectError(f"unknown kind: {kind!r}")
        key = (os.environ if environ is None else environ).get(KEY_ENV, "")
        if not key:
            raise CollectError(f"{KEY_ENV} is not set")
        self.root, self.kind, self._key = Path(root), kind, key
        self._forbidden = {
            v.encode() for v in (key, quote(key, safe=""), quote_plus(key))
        }
        self.http, self.sleep, self.clock = http, sleep, clock
        self.pause, self.progress = pause, progress
        self._requested = False

    def day_dir(self, day: date) -> Path:
        return self.root / self.kind / day.isoformat()

    def _request(self, day: date, page: int) -> dict[str, Any]:
        if self._requested:
            self.sleep(self.pause)
        self._requested = True
        query = {
            "serviceKey": self._key,
            "resultType": "json",
            "basDt": day.strftime("%Y%m%d"),
            "numOfRows": PAGE_ROWS,
            "pageNo": page,
        }
        url = ENDPOINTS[self.kind] + "?" + urlencode(query)
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
            raise CollectError("response echoes the service key; not saved")
        self.progress(
            f"{self.kind} {day} page {page}: HTTP {status}, {len(body)} bytes"
        )
        if status != 200:
            excerpt = " ".join(body.decode("utf-8", "replace").split())[:200]
            raise CollectError(f"HTTP {status}: {excerpt}")
        try:
            payload = json.loads(body)
            header = payload["response"]["header"]
            content = payload["response"]["body"]
            total = int(content["totalCount"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CollectError("response is not an FSC JSON page") from exc
        if str(header.get("resultCode")) != "00":
            raise CollectError(f"supplier resultCode {header.get('resultCode')!r}")
        return {"body": body, "total": total, "rows": _row_count(content)}

    def collect_day(self, day: date) -> DayResult:
        target = self.day_dir(day)
        if (target / "fetch.json").exists():
            return DayResult(day, "skipped")
        temporary = target.with_name(f".{target.name}.partial")
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            first = self._request(day, 1)
            total = first["total"]
            pages = max(1, -(-total // PAGE_ROWS))
            if pages > MAX_PAGES:
                raise CollectError(f"{total} rows exceed {MAX_PAGES} pages")
            fetched = [first] + [self._request(day, n) for n in range(2, pages + 1)]
            if sum(p["rows"] for p in fetched) != total or any(
                p["total"] != total for p in fetched
            ):
                raise CollectError("page rows do not add up to totalCount")
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
                "kind": self.kind,
                "endpoint": ENDPOINTS[self.kind],
                "requested_date": day.isoformat(),
                "received_at": self.clock().isoformat(),
                "total_count": total,
                "pages": records,
            }
            (temporary / "fetch.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, target)
        except CollectError as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            return DayResult(day, "error", message=str(exc))
        return DayResult(day, "fetched", total)

    def collect(
        self, start: date, end: date, *, stop_on_error: bool = True
    ) -> list[DayResult]:
        results = []
        for day in weekdays(start, end):
            result = self.collect_day(day)
            results.append(result)
            if result.status == "error" and stop_on_error:
                break
        return results


def _row_count(content: dict[str, Any]) -> int:
    items = content.get("items")
    if items in (None, ""):
        return 0
    item = items.get("item", []) if isinstance(items, dict) else None
    if isinstance(item, dict):
        return 1
    if not isinstance(item, list):
        raise CollectError("unexpected items shape")
    return len(item)
