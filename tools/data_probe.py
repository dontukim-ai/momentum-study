"""Read-only supplier samples; never imported by the research pipeline.

Repeat --kind-url and --kind-form (in matching order) for the two KIND lists.
Forms and endpoints are deliberately supplied by the operator, not guessed here.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SOURCES = ("fsc_stock", "fsc_etf", "dart", "dart_detail", "kind")
FSC = {
    "fsc_stock": "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo",
    "fsc_etf": "https://apis.data.go.kr/1160100/service/GetSecuritiesProductInfoService/getETFPriceInfo",
}

# Candidate endpoints: the probe records supplier responses to verify these mappings.
DART_DETAILS = (
    ("유무상증자결정", "pifricDecsn"),
    ("무상증자결정", "fricDecsn"),
    ("유상증자결정", "piicDecsn"),
    ("감자결정", "crDecsn"),
)


class ProbeError(Exception):
    """Only credential-checked diagnostics may leave the request boundary."""


# Socket timeouts do not bound DNS lookups or a server that trickles bytes, so
# each request also gets a wall-clock deadline; a stalled call is abandoned.
REQUEST_DEADLINE_SECONDS = 60.0


def _fetch(url, data):
    try:
        with urlopen(Request(url, data=data), timeout=30) as response:
            return response.status, response.read()
    except HTTPError as exc:
        with exc:
            return exc.code, exc.read()


def http_get(url, data, *, deadline=None):
    """Injectable transport returning HTTP status and untouched bytes."""
    outcome = {}

    def run():
        try:
            outcome["value"] = _fetch(url, data)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller thread
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(REQUEST_DEADLINE_SECONDS if deadline is None else deadline)
    if worker.is_alive():
        raise TimeoutError("request deadline exceeded")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def progress(message):
    # stderr keeps the stdout contract (last line is the summary) unchanged.
    print(f"probe: {message}", file=sys.stderr, flush=True)


def masked_url(url):
    parts = urlsplit(url)
    # KIND forms can contain arbitrary credentials; mask every query value.
    query = urlencode([(key, "***") for key, _ in parse_qsl(parts.query)], safe="*")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def infer_type(values):
    if not values:
        return "string"
    strings = [str(value) for value in values]
    if all(re.fullmatch(r"\d{8}", value) for value in strings):
        try:
            for value in strings:
                date.fromisoformat(value)
            return "date YYYYMMDD"
        except ValueError:
            pass
    if all(re.fullmatch(r"[+-]?\d+", value) for value in strings):
        return "integer"
    try:
        if all(Decimal(value).is_finite() for value in strings):
            return "decimal"
    except InvalidOperation:
        pass
    return "string"


def summarize(rows):
    fields = sorted({key for row in rows for key in row})
    empty, samples, types = {}, {}, {}
    for field in fields:
        values = [
            row[field]
            for row in rows
            if row.get(field) is not None and row[field] != ""
        ]
        empty[field] = len(rows) - len(values)
        unique = []
        for value in values:
            if value not in unique:
                unique.append(value)
            if len(unique) == 3:
                break
        samples[field] = unique
        types[field] = infer_type(values)
    return {
        "total_rows": len(rows),
        "fields": fields,
        "empty_counts": empty,
        "samples": samples,
        "types": types,
    }


def main(argv=None, *, http=http_get, sleep=time.sleep, environ=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source", choices=("all", *SOURCES), default="all")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--dart-bgn", type=date.fromisoformat)
    parser.add_argument("--dart-end", type=date.fromisoformat)
    parser.add_argument("--max-detail", type=int, default=10)
    parser.add_argument("--with-dart-detail", action="store_true")
    parser.add_argument("--kind-url", action="append", default=[])
    parser.add_argument("--kind-form", action="append", default=[])
    args = parser.parse_args(argv)
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if args.max_detail < 1:
        parser.error("--max-detail must be positive")
    if (args.dart_bgn is None) != (args.dart_end is None):
        parser.error("provide both --dart-bgn and --dart-end")
    bgn, end = args.dart_bgn or args.date, args.dart_end or args.date
    if not 0 <= (end - bgn).days <= 91:
        parser.error("DART range must be ordered and at most 92 days inclusive")
    dart_period = f"{bgn.isoformat()}_{end.isoformat()}"
    env = os.environ if environ is None else environ
    secrets = [
        env.get(name, "") for name in ("DATA_GO_KR_SERVICE_KEY", "OPENDART_API_KEY")
    ]
    forbidden = {
        variant.encode()
        for key in secrets
        if key
        for variant in (key, quote(key, safe=""), quote_plus(key))
    }
    manifest = {"files": [], "requests": []}
    summary = {}
    requested = False
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    def request(url, data=None):
        nonlocal requested
        if requested:
            sleep(1.0)
        requested = True
        shown = masked_url(url)
        if any(key in shown.encode() for key in forbidden):
            shown = "<url withheld>"
        for attempt in range(3):
            started = time.monotonic()
            progress(f"request {len(manifest['requests']) + 1} {shown}")
            try:
                status, body = http(url, data)
                break
            except HTTPError as exc:
                with exc:
                    status, body = exc.code, exc.read()
                break
            except (URLError, OSError) as exc:
                reason = "timeout" if isinstance(exc, TimeoutError) else "network error"
                progress(f"  {reason} after {time.monotonic() - started:.0f}s")
                if attempt == 2:
                    raise ProbeError("network retries exhausted") from None
                sleep(float(2 ** (attempt + 1)))
            except Exception:  # noqa: BLE001 - transport exceptions must not expose URLs/keys
                raise ProbeError("transport failed") from None
        if any(key in body for key in forbidden):
            raise ProbeError("response contains credentials; not saved")
        try:
            decoded = json.dumps(json.loads(body), ensure_ascii=False).encode()
        except (ValueError, UnicodeError):
            decoded = b""
        if any(key in decoded for key in forbidden):
            raise ProbeError("response contains credentials; not saved")
        metadata = {
            "url": masked_url(url),
            "http_status": status,
            "received_at": datetime.now(UTC).isoformat(),
            "bytes": len(body),
        }
        # Reject unexpected credential echoes in paths/field names as well.
        if any(key in json.dumps(metadata).encode() for key in forbidden):
            raise ProbeError("request metadata contains credentials; not saved")
        manifest["requests"].append(metadata)
        progress(f"  HTTP {status}, {len(body)} bytes")
        if not 200 <= status < 300:
            extension = "txt"
            stripped = body.lstrip()
            if stripped.startswith(b"<"):
                extension = "xml"
            elif stripped.startswith((b"{", b"[")):
                extension = "json"
            save(
                source, f"error-{len(manifest['requests'])}", extension, body, metadata
            )
            excerpt = " ".join(body.decode("utf-8", errors="replace").split())[:200]
            message = f"HTTP status {status}"
            if excerpt:
                message += f": {excerpt}"
            raise ProbeError(message)
        return body, metadata

    def save(source, page, extension, body, metadata):
        period = args.date.isoformat()
        if source in ("dart", "dart_detail"):
            period = dart_period
        filename = f"page-{page}" if isinstance(page, int) else page
        relative = Path(source) / period / f"{filename}.{extension}"
        if any(key in relative.as_posix().encode() for key in forbidden):
            raise ProbeError("file path contains credentials; not saved")
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        manifest["files"].append(
            {
                **metadata,
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )

    dart_rows = []
    selected = (
        tuple(source for source in SOURCES if source != "dart_detail")
        if args.source == "all"
        else (args.source,)
    )
    if args.with_dart_detail or args.source == "dart_detail":
        # Detail probing requires the list first, even when another source is selected.
        selected = tuple(
            source for source in SOURCES if source in (*selected, "dart", "dart_detail")
        )
    for source in selected:
        if source == "dart_detail":
            result = {"status": "ok", "endpoints": {}}
            summary[source] = result
            if summary.get("dart", {}).get("status") != "ok":
                result.update(status="skipped", message="DART source unavailable")
                continue
            seen, endpoint_rows = set(), {}
            for row in dart_rows:
                if not row.get("stock_code"):
                    continue
                endpoint = next(
                    (
                        name
                        for keyword, name in DART_DETAILS
                        if keyword in row.get("report_nm", "")
                    ),
                    None,
                )
                if endpoint is None:
                    continue
                corp, received = row.get("corp_code"), row.get("rcept_dt")
                identity = (endpoint, corp, received)
                if identity in seen:
                    continue
                if len(seen) >= args.max_detail:
                    break
                seen.add(identity)
                entry = result["endpoints"].setdefault(
                    endpoint, {"status": "ok", "calls": 0, **summarize([])}
                )
                batch_rows = endpoint_rows.setdefault(endpoint, [])
                entry["calls"] += 1
                try:
                    # Supplier identifiers become filenames; never permit path components.
                    if (
                        not isinstance(corp, str)
                        or not re.fullmatch(r"\d{8}", corp)
                        or not isinstance(received, str)
                        or not re.fullmatch(r"\d{8}", received)
                    ):
                        raise ProbeError("invalid filing identifiers")
                    params = {
                        "crtfc_key": env["OPENDART_API_KEY"],
                        "corp_code": corp,
                        "bgn_de": received,
                        "end_de": received,
                    }
                    body, metadata = request(
                        f"https://opendart.fss.or.kr/api/{endpoint}.json?"
                        + urlencode(params)
                    )
                    save(
                        source, f"{endpoint}-{corp}-{received}", "json", body, metadata
                    )
                    payload = json.loads(body)
                    if payload["status"] not in ("000", "013"):
                        raise ProbeError("supplier returned an error code")
                    batch = (
                        payload.get("list", []) if payload["status"] == "000" else []
                    )
                    if not isinstance(batch, list) or not all(
                        isinstance(r, dict) for r in batch
                    ):
                        raise ProbeError("unexpected row schema")
                    batch_rows.extend(batch)
                except ProbeError as exc:
                    entry.update(status="error", message=str(exc))
                except Exception:  # noqa: BLE001 - parser input may contain credentials
                    entry.update(
                        status="error", message="invalid response or local file error"
                    )
                entry.update(summarize(batch_rows))
            if not result["endpoints"]:
                result["message"] = "no matching filings"
            if any(
                entry["status"] == "error" for entry in result["endpoints"].values()
            ):
                result["status"] = "error"
            continue
        rows = []
        result = {"status": "ok", **summarize([])}
        summary[source] = result
        key = env.get(
            "OPENDART_API_KEY" if source == "dart" else "DATA_GO_KR_SERVICE_KEY"
        )
        if source != "kind" and not key:
            result.update(status="skipped", message="missing key")
            continue
        if source == "kind" and (not args.kind_url or not args.kind_form):
            result.update(status="skipped", message="missing KIND URL/form")
            continue
        try:
            if source == "kind":
                if len(args.kind_url) != len(args.kind_form):
                    raise ProbeError("provide one JSON form per KIND URL")
                if len(args.kind_url) > args.max_pages:
                    raise ProbeError("max pages reached")
                for page, (url, form) in enumerate(
                    zip(args.kind_url, args.kind_form), 1
                ):
                    parts = urlsplit(url)
                    if (
                        parts.scheme != "https"
                        or parts.hostname != "kind.krx.co.kr"
                        or parts.username
                    ):
                        raise ProbeError("KIND URL must use https://kind.krx.co.kr")
                    fields = json.loads(form)
                    if not isinstance(fields, dict):
                        raise ProbeError("KIND form must be a JSON object")
                    body, metadata = request(url, urlencode(fields).encode())
                    save(source, page, "html", body, metadata)
                result["total_rows"] = None  # HTML is intentionally not parsed.
                result.update(fields=[], empty_counts={}, samples={}, types={})
                continue
            page, pages = 1, 1
            while page <= pages:
                if page > args.max_pages:
                    raise ProbeError("max pages reached; sample incomplete")
                if source in FSC:
                    params = {
                        "serviceKey": key,
                        "resultType": "json",
                        "basDt": args.date.strftime("%Y%m%d"),
                        "numOfRows": 1000,
                        "pageNo": page,
                    }
                    url = FSC[source] + "?" + urlencode(params)
                else:
                    params = {
                        "crtfc_key": key,
                        "bgn_de": bgn.strftime("%Y%m%d"),
                        "end_de": end.strftime("%Y%m%d"),
                        "pblntf_ty": "B",
                        "page_count": 100,
                        "page_no": page,
                    }
                    url = "https://opendart.fss.or.kr/api/list.json?" + urlencode(
                        params
                    )
                body, metadata = request(url)
                save(source, page, "json", body, metadata)
                payload = json.loads(body)
                if source in FSC:
                    response = payload["response"]
                    if str(response["header"]["resultCode"]) not in ("00", "0"):
                        raise ProbeError("supplier returned an error code")
                    content = response["body"]
                    pages = max(1, (int(content["totalCount"]) + 999) // 1000)
                    items = content.get("items") or {}
                    batch = items.get("item", []) if isinstance(items, dict) else []
                    if isinstance(batch, dict):
                        batch = [batch]
                else:
                    if payload["status"] not in ("000", "013"):
                        raise ProbeError("supplier returned an error code")
                    pages = max(1, int(payload.get("total_page", 0)))
                    batch = payload.get("list", [])
                if not isinstance(batch, list) or not all(
                    isinstance(row, dict) for row in batch
                ):
                    raise ProbeError("unexpected row schema")
                rows.extend(batch)
                page += 1
            if source == "dart":
                body, _ = request(
                    "https://opendart.fss.or.kr/api/corpCode.xml?"
                    + urlencode({"crtfc_key": key})
                )
                result["corp_code_bytes"] = len(body)
        except ProbeError as exc:
            result.update(status="error", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate suppliers without exposing parser input
            # Supplier bodies and parser exceptions may echo secrets. Never render them.
            result.update(
                status="error", message="invalid response or local file error"
            )
        result.update(summarize(rows))
        if source == "dart":
            dart_rows = rows
    for filename, content in (("manifest.json", manifest), ("summary.json", summary)):
        (out / filename).write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    counts = {
        status: sum(item["status"] == status for item in summary.values())
        for status in ("ok", "skipped", "error")
    }
    print(
        f"probe: {counts['ok']} sources ok, {counts['skipped']} skipped, {counts['error']} errors"
    )
    return int(counts["error"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
