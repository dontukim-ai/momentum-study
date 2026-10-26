"""Real-key account reads. No order-writing API or OMS broker implementation."""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from .contracts import (
    KST,
    UTC,
    AgentError,
    D,
    Quote,
    canonical,
    dec,
    digest,
    kis_account,
)
from .kis import http_json
from .token_cache import cached_token

REAL_HOST = "https://openapi.koreainvestment.com:9443"
REAL_CAPITAL_LIMIT = D("1000000")
REAL_NAMES = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT")
READ_ROUTES = {
    "quote": ("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100"),
    "balance": ("/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R"),
    "orders": ("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", "TTTC0081R"),
    # Market-data reads for the intraday cost probe (intraday-cost-probe-v10 §7).
    "asking": ("/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn", "FHKST01010200"),
    "minute": ("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice", "FHKST03010200"),
    # Past-date one-minute bars; KIS keeps about a year (intraday-candidates-v10 §5).
    "minute_daily": ("/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice", "FHKST03010230"),
}
MARKET_READS = ("quote", "asking", "minute", "minute_daily")
MINUTE_HISTORY_DAYS = 380


def _integer(value) -> int:
    number = dec(value)
    if number < 0 or number != number.to_integral_value():
        raise AgentError("Invalid nonnegative real-account quantity")
    return int(number)


def _symbol(symbol) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
        raise AgentError("A six-digit Korean symbol is required")
    return symbol


class KisRealReadOnly:
    def __init__(self, transport=http_json, token_cache_dir: Path | None = None,
                 minimum_interval: float = .7):
        self._key, self._secret, account = (os.environ.get(name, "") for name in REAL_NAMES)
        account = kis_account(account)
        if not self._key or not self._secret or not account:
            raise AgentError("Configure KIS_APP_KEY, KIS_APP_SECRET and KIS_ACCOUNT (8 digits-01)")
        if not minimum_interval >= .1:
            raise AgentError("Invalid real-account read interval")
        self._cano, self._product = account.split("-")
        self._transport, self._cache = transport, token_cache_dir
        self._interval, self._last, self._expiry, self._token = minimum_interval, 0., 0., ""
        self._lock = threading.Lock()
        self._cache_id = digest({"mode": "KIS_REAL_READ_ONLY", "account": account, "appkey": self._key})

    def _issue_token(self):
        result = self._transport("POST", REAL_HOST + "/oauth2/tokenP", {"content-type": "application/json"},
                                 {"grant_type": "client_credentials", "appkey": self._key, "appsecret": self._secret})
        try:
            token, lifetime = result["access_token"], int(result.get("expires_in", 86400))
            if not isinstance(token, str) or not token or lifetime <= 60:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AgentError("Real-account token response is invalid") from None
        return token, lifetime

    def request(self, operation: str, params: dict, continuation: str = "") -> dict:
        if operation not in READ_ROUTES:
            raise AgentError("Only real-account quote, order-book, minute-bar, balance and recent-order reads are allowed")
        if operation not in MARKET_READS and any(params.get(k) != v for k, v in self.account_params().items()):
            raise AgentError("Real-account read identity mismatch")
        path, tr_id = READ_ROUTES[operation]
        with self._lock:
            if time.monotonic() >= self._expiry:
                if self._cache is None:
                    token, lifetime = self._issue_token()
                else:
                    token, expiry = cached_token(self._cache, self._cache_id, self._issue_token)
                    lifetime = expiry - time.time()
                self._token, self._expiry = token, time.monotonic() + lifetime - 60
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            result = self._transport("GET", REAL_HOST + path + "?" + urlencode(params),
                                     {"content-type": "application/json", "authorization": "Bearer " + self._token,
                                      "appkey": self._key, "appsecret": self._secret, "tr_id": tr_id,
                                      "custtype": "P", "tr_cont": continuation}, None)
        if not isinstance(result, dict) or str(result.get("rt_cd")) != "0":
            raise AgentError("Real-account read failed or returned a malformed response")
        return result

    def account_params(self):
        return {"CANO": self._cano, "ACNT_PRDT_CD": self._product}

    def _pages(self, operation: str, params: dict):
        params = {**params, "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        rows, summaries, seen, continuation = [], [], set(), ""
        for _ in range(20):
            result = self.request(operation, params, continuation)
            page = result.get("output1")
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise AgentError("Missing real-account query rows")
            rows.extend(page)
            summary = result.get("output2", [])
            summaries.extend(summary if isinstance(summary, list) else [summary])
            headers = result.get("_response_headers")
            if not isinstance(headers, dict):
                raise AgentError("Missing real-account continuation headers")
            flag = headers.get("tr_cont")
            if flag in ("", "D", "E"):
                return rows, summaries
            cursor = (result.get("ctx_area_fk100", ""), result.get("ctx_area_nk100", ""))
            if flag not in ("M", "F") or not all(isinstance(c, str) for c in cursor) or not any(cursor) or cursor in seen:
                raise AgentError("Unverified or repeated real-account continuation")
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
            continuation = "N"
        raise AgentError("Real-account page budget exhausted; partial results rejected")

    def quote(self, symbol: str) -> Quote:
        result = self.request("quote", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": _symbol(symbol)})
        output = result.get("output")
        if not isinstance(output, dict) or "stck_prpr" not in output:
            raise AgentError("Missing real quote price")
        return Quote(symbol, dec(output["stck_prpr"]), datetime.now(UTC))

    def asking(self, symbol: str) -> dict:
        """The raw ten-level order book; prices and sizes are left for the caller to parse."""
        result = self.request("asking", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": _symbol(symbol)})
        output = result.get("output1")
        if not isinstance(output, dict) or not {"askp1", "bidp1", "aspr_acpt_hour"} <= output.keys():
            raise AgentError("Missing real order-book levels")
        return result

    def minute_bars(self, symbol: str, hour: str) -> dict:
        """Up to 30 same-day one-minute bars ending at ``hour`` (HHMMSS, KST)."""
        if not isinstance(hour, str) or not re.fullmatch(r"(0[89]|1[0-5])[0-5][0-9][0-5][0-9]", hour):
            raise AgentError("A KRX session time HHMMSS is required")
        result = self.request("minute", {"FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": "J",
                                         "FID_INPUT_ISCD": _symbol(symbol), "FID_INPUT_HOUR_1": hour,
                                         "FID_PW_DATA_INCU_YN": "Y"})
        rows = result.get("output2")
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not {"stck_bsop_date", "stck_cntg_hour", "stck_prpr"} <= row.keys()
                for row in rows):
            raise AgentError("Missing real minute-bar rows")
        return result

    def minute_bars_on(self, symbol: str, day: date, hour: str) -> dict:
        """Up to 120 one-minute bars of a past ``day`` ending at ``hour`` (HHMMSS, KST)."""
        today = datetime.now(KST).date()
        if type(day) is not date or not 0 <= (today - day).days <= MINUTE_HISTORY_DAYS:
            raise AgentError("Only days inside the retained minute-bar window are supported")
        if not isinstance(hour, str) or not re.fullmatch(r"(0[89]|1[0-5])[0-5][0-9][0-5][0-9]", hour):
            raise AgentError("A KRX session time HHMMSS is required")
        result = self.request("minute_daily", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": _symbol(symbol),
                                               "FID_INPUT_HOUR_1": hour, "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
                                               "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""})
        rows = result.get("output2")
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not {"stck_bsop_date", "stck_cntg_hour", "stck_prpr"} <= row.keys()
                for row in rows):
            raise AgentError("Missing real minute-bar rows")
        return result

    def balance(self) -> dict:
        rows, summaries = self._pages("balance", {**self.account_params(), "AFHR_FLPR_YN": "N", "OFL_YN": "",
            "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00"})
        if not summaries or any(not isinstance(s, dict) or not {"dnca_tot_amt", "prvs_rcdl_excc_amt"} <= s.keys() for s in summaries):
            raise AgentError("Missing distinct deposit and expected-settlement cash fields")
        cash = {(dec(s["dnca_tot_amt"]), dec(s["prvs_rcdl_excc_amt"])) for s in summaries}
        if len(cash) != 1 or any(value < 0 for pair in cash for value in pair):
            raise AgentError("Conflicting or negative real-account cash snapshot")
        holdings = {}
        for row in rows:
            symbol = row.get("pdno")
            if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
                raise AgentError("Unsupported real-account instrument")
            quantity, sellable = _integer(row.get("hldg_qty")), _integer(row.get("ord_psbl_qty"))
            if sellable > quantity or (symbol in holdings and holdings[symbol] != (quantity, sellable)):
                raise AgentError("Conflicting real-account holding snapshot")
            holdings[symbol] = (quantity, sellable)
        deposit, expected = next(iter(cash))
        return {"deposit_cash": deposit, "expected_settlement_cash": expected, "holdings": holdings,
                "cash_is_verified_buying_power": False}

    def orders(self, day: date) -> list[dict]:
        if not 0 <= (datetime.now(KST).date() - day).days <= 80:
            raise AgentError("Only recent real-account order dates are supported")
        rows, _ = self._pages("orders", {**self.account_params(), "INQR_STRT_DT": day.strftime("%Y%m%d"),
            "INQR_END_DT": day.strftime("%Y%m%d"), "SLL_BUY_DVSN_CD": "00", "CCLD_DVSN": "00", "INQR_DVSN": "01",
            "INQR_DVSN_3": "01", "PDNO": "", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "", "EXCG_ID_DVSN_CD": "KRX"})
        unique = {}
        for row in rows:
            identity = (row.get("ord_dt"), row.get("ord_gno_brno"), row.get("odno"))
            if not all(isinstance(value, str) and value for value in identity) or identity[0] != day.strftime("%Y%m%d"):
                raise AgentError("Missing or out-of-range real order identity")
            if identity in unique and canonical(unique[identity]) != canonical(row):
                raise AgentError("Real order changed during pagination")
            unique[identity] = row
        return list(unique.values())
