"""Allowlisted read-only KIS REST collector. No order endpoint exists here."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Callable

from .contracts import KST, UTC, AgentError, Bar, dec, session_time
from .data import Snapshot
from .transport import open_authenticated

BASE_URLS = {"paper": "https://openapivts.koreainvestment.com:29443",
             "real-readonly": "https://openapi.koreainvestment.com:9443"}
TOKEN_PATH = "/oauth2/tokenP"
DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"


def http_json(method: str, url: str, headers: dict, body: dict | None) -> dict:
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with open_authenticated(request, timeout=15) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            continuation = response.headers.get("tr_cont", "")
        if len(raw) > 8 * 1024 * 1024:
            raise AgentError("KIS response exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise AgentError("Unexpected KIS response shape")
        result["_response_headers"] = {"tr_cont": continuation}
        return result
    except urllib.error.HTTPError as exc:
        # Never echo headers, access tokens, body, credentials or arbitrary exception URLs.
        raise AgentError(f"KIS HTTP error {exc.code}; verify connectivity/entitlement/rate limits") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise AgentError("KIS transport or JSON failure; caller must classify the request outcome") from None


class KisReadOnly:
    def __init__(self, environment: str = "paper", transport: Callable = http_json,
                 minimum_interval: float = .7) -> None:
        if environment not in BASE_URLS or not minimum_interval >= .1:
            raise AgentError("Unknown KIS read-only environment or invalid request interval")
        prefix = "KIS_PAPER_" if environment == "paper" else "KIS_"
        self._key = os.environ.get(prefix + "APP_KEY", "")
        self._secret = os.environ.get(prefix + "APP_SECRET", "")
        if not self._key or not self._secret:
            raise AgentError(f"Configure {prefix}APP_KEY and {prefix}APP_SECRET via environment variables")
        self.environment, self._base, self._transport = environment, BASE_URLS[environment], transport
        self._token, self._expiry = "", 0.0
        self._last, self._interval = 0.0, minimum_interval
        self._lock = threading.Lock()

    def _request(self, path: str, params: dict | None = None) -> dict:
        if path != DAILY_PATH:
            raise AgentError("Only the daily quotation endpoint is allowed")
        with self._lock:
            if time.monotonic() >= self._expiry:
                token = self._transport("POST", self._base + TOKEN_PATH,
                                        {"content-type": "application/json"},
                                        {"grant_type": "client_credentials", "appkey": self._key, "appsecret": self._secret})
                if not token.get("access_token"):
                    raise AgentError("KIS token issuance failed")
                try:
                    lifetime = int(token.get("expires_in", 86400))
                except (ValueError, TypeError):
                    raise AgentError("Invalid KIS token lifetime") from None
                if lifetime <= 60:
                    raise AgentError("Invalid KIS token lifetime")
                self._token, self._expiry = token["access_token"], time.monotonic() + lifetime - 60
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            result = self._transport("GET", self._base + path + "?" + urllib.parse.urlencode(params or {}),
                                     {"content-type": "application/json", "authorization": "Bearer " + self._token,
                                      "appkey": self._key, "appsecret": self._secret,
                                      "tr_id": "FHKST03010100", "custtype": "P"}, None)
        if str(result.get("rt_cd")) != "0":
            raise AgentError("KIS business request failed; verify endpoint/permission/quota using official portal")
        return result

    def collect(self, symbols: list[str], start: date, end: date,
                assume_historical_availability: bool = False) -> Snapshot:
        if not symbols or len(set(symbols)) != len(symbols) or any(not re.fullmatch(r"[0-9]{6}", s) for s in symbols):
            raise AgentError("Provide unique six-digit Korean instrument codes")
        observed_at = datetime.now(UTC)
        if end < start or end > observed_at.astimezone(KST).date():
            raise AgentError("Invalid date range or future end date")
        bars = []
        for symbol in symbols:
            cursor = end
            seen = set()
            for _ in range(1000):
                response = self._request(DAILY_PATH, {
                    "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": cursor.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"})
                rows = response.get("output2")
                if not isinstance(rows, list):
                    raise AgentError("Missing KIS daily rows")
                dated = []
                for row in rows:
                    if not isinstance(row, dict):
                        raise AgentError("Invalid KIS daily row shape")
                    if not row.get("stck_bsop_date"):
                        continue
                    try:
                        day = datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date()
                    except (ValueError, TypeError):
                        raise AgentError("Invalid KIS daily session date") from None
                    if start <= day <= cursor:
                        dated.append((day, row))
                if not dated:
                    break
                earliest = min(day for day, _ in dated)
                collected_at = datetime.now(UTC)
                for day, row in dated:
                    if day in seen:
                        continue
                    seen.add(day)
                    estimated = session_time(day, 15, 40)
                    if estimated > collected_at:
                        continue  # Never persist a current, unfinished daily bar as final.
                    available = estimated if assume_historical_availability else collected_at
                    source = "KIS_" + self.environment.upper() + ("_ASSUMED_HISTORICAL_1540" if assume_historical_availability else "_OBSERVED_AT_COLLECTION")
                    try:
                        bars.append(Bar(day, symbol, *(dec(row[k]) for k in (
                            "stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr")),
                            int(row["acml_vol"]), available, source))
                    except (KeyError, ValueError, TypeError, ArithmeticError):
                        raise AgentError("Invalid KIS daily price/volume row") from None
                if earliest <= start:
                    break
                cursor = earliest - timedelta(days=1)
            else:
                raise AgentError("Daily-history request budget exceeded")
        return Snapshot(bars)
