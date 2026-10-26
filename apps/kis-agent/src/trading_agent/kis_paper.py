"""Paper-host-only REST adapter; strict contracts and conservative unknown recovery."""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path

from .brokers import BrokerRejected
from .contracts import (
    KST,
    UTC,
    AgentError,
    D,
    OrderIntent,
    Quote,
    Wallet,
    aware,
    canonical,
    dec,
    digest,
    kis_account,
)
from .kis import http_json
from .store import Store
from .token_cache import cached_token

PAPER_HOST = "https://openapivts.koreainvestment.com:29443"
ROOT = "/uapi/domestic-stock/v1/"
ROUTES = {
    "buy": ("POST", ROOT + "trading/order-cash", "VTTC0012U"),
    "sell": ("POST", ROOT + "trading/order-cash", "VTTC0011U"),
    "cancel": ("POST", ROOT + "trading/order-rvsecncl", "VTTC0013U"),
    "orders": ("GET", ROOT + "trading/inquire-daily-ccld", "VTTC0081R"),
    "balance": ("GET", ROOT + "trading/inquire-balance", "VTTC8434R"),
    "buyable": ("GET", ROOT + "trading/inquire-psbl-order", "VTTC8908R"),
    "quote": ("GET", ROOT + "quotations/inquire-price", "FHKST01010100"),
}


def integer(value, label: str) -> int:
    number = dec(value)
    if number < 0 or number != number.to_integral_value():
        raise AgentError("Invalid nonnegative integer field: " + label)
    return int(number)


class PaperSession:
    """No host override and no access to real-account credentials."""

    def __init__(self, transport=http_json, minimum_interval: float = .7,
                 token_cache_dir: Path | None = None):
        self._key = os.environ.get("KIS_PAPER_APP_KEY", "")
        self._secret = os.environ.get("KIS_PAPER_APP_SECRET", "")
        account = kis_account(os.environ.get("KIS_PAPER_ACCOUNT", ""))
        if not self._key or not self._secret or not account:
            raise AgentError("Configure KIS_PAPER_APP_KEY, KIS_PAPER_APP_SECRET and KIS_PAPER_ACCOUNT (8 digits-01)")
        if not minimum_interval >= .1:
            raise AgentError("Invalid request interval")
        self._cano, self._product = account.split("-")
        self.identity = digest({"environment": "kis-paper", "account": account})
        self._transport, self._interval = transport, minimum_interval
        self._token, self._expiry, self._last = "", 0., 0.
        self._lock = threading.Lock()
        self._token_cache_dir = token_cache_dir

    def _issue_token(self) -> tuple[str, int]:
        token = self._transport("POST", PAPER_HOST + "/oauth2/tokenP", {"content-type": "application/json"},
                                {"grant_type": "client_credentials", "appkey": self._key, "appsecret": self._secret})
        try:
            lifetime = int(token.get("expires_in", 86400))
        except (TypeError, ValueError, AttributeError):
            raise AgentError("Invalid paper token lifetime") from None
        if not isinstance(token.get("access_token"), str) or not token["access_token"] or lifetime <= 60:
            raise AgentError("Paper token issuance failed")
        return token["access_token"], lifetime

    def account_params(self) -> dict:
        return {"CANO": self._cano, "ACNT_PRDT_CD": self._product}

    def request(self, operation: str, params: dict, continuation: str = "") -> dict:
        if operation not in ROUTES:
            raise AgentError("Unknown paper operation")
        method, path, tr_id = ROUTES[operation]
        if operation != "quote" and any(params.get(k) != v for k, v in self.account_params().items()):
            raise AgentError("Paper account parameters do not match the configured identity")
        with self._lock:
            if time.monotonic() >= self._expiry:
                if self._token_cache_dir is None:
                    token, lifetime = self._issue_token()
                else:
                    token, expiry = cached_token(self._token_cache_dir, digest((self.identity, self._key)), self._issue_token)
                    lifetime = expiry - time.time()
                self._token, self._expiry = token, time.monotonic() + lifetime - 60
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            headers = {"content-type": "application/json", "authorization": "Bearer " + self._token,
                       "appkey": self._key, "appsecret": self._secret, "tr_id": tr_id,
                       "custtype": "P", "tr_cont": continuation}
            url = PAPER_HOST + path
            result = self._transport(method, url + ("?" + urllib.parse.urlencode(params) if method == "GET" else ""),
                                     headers, params if method == "POST" else None)
        if not isinstance(result, dict) or str(result.get("rt_cd")) != "0":
            # Only an explicit valid business rejection is known not to have accepted an order.
            if isinstance(result, dict) and result.get("rt_cd") in ("1", 1) and operation in {"buy", "sell"}:
                raise BrokerRejected("Paper broker explicitly rejected the submission")
            raise AgentError("Paper business response failed or is malformed")
        return result

    def pages(self, operation: str, params: dict, max_pages: int = 20) -> tuple[list[dict], list[dict]]:
        if operation not in {"orders", "balance"}:
            raise AgentError("Only order/balance queries support this pagination contract")
        params = {**params, "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        rows, summaries, seen, continuation = [], [], set(), ""
        for _ in range(max_pages):
            result = self.request(operation, params, continuation)
            if not isinstance(result.get("output1"), list) or not all(isinstance(x, dict) for x in result["output1"]):
                raise AgentError("Missing paper query rows")
            rows.extend(result["output1"])
            output2 = result.get("output2", [])
            summaries.extend(output2 if isinstance(output2, list) else [output2])
            headers = result.get("_response_headers")
            if not isinstance(headers, dict) or "tr_cont" not in headers:
                raise AgentError("Pagination completion header is unavailable")
            flag = headers["tr_cont"]
            if flag in ("", "D", "E"):
                return rows, summaries
            if flag not in ("M", "F"):
                raise AgentError("Unknown KIS pagination flag")
            cursor = (result.get("ctx_area_fk100", ""), result.get("ctx_area_nk100", ""))
            if not any(cursor) or cursor in seen:
                raise AgentError("Repeated or missing pagination cursor")
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
            continuation = "N"
        raise AgentError("KIS page budget exhausted; incomplete results were not accepted")


class KisPaperBroker:
    def __init__(self, store: Store, session: PaperSession, account: str = "kis-paper",
                 tick_sizes: dict | None = None, cancelability_confirmed: bool = False):
        self.store, self.session, self.account = store, session, account
        self.tick_sizes = {s: dec(t) for s, t in (tick_sizes or {}).items()}
        self.cancelability_confirmed = cancelability_confirmed
        if any(t <= 0 for t in self.tick_sizes.values()):
            raise AgentError("Tick sizes must be positive verified instrument values")

    def assert_account(self, account: str) -> None:
        if account != self.account:
            raise AgentError("KIS paper account alias mismatch")
        binding = self.store.db.execute("SELECT mode,identity_hash FROM broker_bindings WHERE account=?", (account,)).fetchone()
        if not binding or tuple(binding) != ("KIS_PAPER", self.session.identity):
            raise AgentError("Initialize or verify this paper account binding before execution")

    def _balance(self) -> tuple[Wallet, dict[str, int]]:
        rows, summaries = self.session.pages("balance", {**self.session.account_params(), "AFHR_FLPR_YN": "N", "OFL_YN": "",
            "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00"})
        if not summaries or any(not isinstance(s, dict) or "prvs_rcdl_excc_amt" not in s for s in summaries):
            raise AgentError("Expected-settlement cash field is missing; deposit cash is not substituted")
        cash_values = {dec(s["prvs_rcdl_excc_amt"]) for s in summaries}
        if len(cash_values) != 1:
            raise AgentError("Balance changed during pagination; retry a read-only snapshot")
        positions, sellable = {}, {}
        for row in rows:
            symbol = row.get("pdno", "")
            if not re.fullmatch(r"[0-9]{6}", symbol):
                raise AgentError("Unsupported instrument in paper balance")
            quantity, available = integer(row.get("hldg_qty"), "hldg_qty"), integer(row.get("ord_psbl_qty"), "ord_psbl_qty")
            if available > quantity or (symbol in sellable and (sellable[symbol] != available or positions.get(symbol, 0) != quantity)):
                raise AgentError("Conflicting paper position rows")
            if quantity:
                positions[symbol] = quantity
            sellable[symbol] = available
        return Wallet(next(iter(cash_values)), positions), sellable

    def orders(self, day: date) -> list[dict]:
        today = datetime.now(KST).date()
        if not 0 <= (today - day).days <= 80:
            raise AgentError("Only recent order dates are supported; older queries require a separate contract")
        rows, _ = self.session.pages("orders", {**self.session.account_params(), "INQR_STRT_DT": day.strftime("%Y%m%d"),
            "INQR_END_DT": day.strftime("%Y%m%d"), "SLL_BUY_DVSN_CD": "00", "CCLD_DVSN": "00", "INQR_DVSN": "01",
            "INQR_DVSN_3": "01", "PDNO": "", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "", "EXCG_ID_DVSN_CD": "KRX"})
        unique = {}
        for row in rows:
            key = (row.get("ord_dt"), row.get("ord_gno_brno"), row.get("odno"))
            if not all(key) or key[0] != day.strftime("%Y%m%d"):
                raise AgentError("Missing or out-of-range broker order identity")
            if key in unique and canonical(unique[key]) != canonical(row):
                raise AgentError("Order changed during pagination; retry read-only reconciliation")
            unique[key] = row
        return list(unique.values())

    def bootstrap(self) -> dict:
        existing = self.store.db.execute("SELECT 1 FROM accounts WHERE account=?", (self.account,)).fetchone()
        if existing:
            self.assert_account(self.account)
            return self.store.status(self.account)
        wallet, _ = self._balance()
        if set(wallet.positions) - self.tick_sizes.keys():
            raise AgentError("Bootstrap holdings exceed the explicitly configured instrument contracts")
        if any(integer(r.get("rmn_qty"), "rmn_qty") for r in self.orders(datetime.now(KST).date())):
            raise AgentError("Bootstrap requires no untracked open broker orders")
        with self.store.transaction():
            if self.store.db.execute("SELECT 1 FROM accounts WHERE account=?", (self.account,)).fetchone():
                raise AgentError("Account was concurrently initialized")
            self.store.bind_broker(self.account, "KIS_PAPER", self.session.identity)
            for ledger in ("LOCAL", "REMOTE"):
                self.store.db.execute("INSERT INTO accounts(account,ledger,cash,initial_cash) VALUES(?,?,?,?)",
                                      (self.account, ledger, str(wallet.cash), str(wallet.cash)))
                self.store.save_wallet(self.account, ledger, wallet)
            self.store.event(self.account, None, "KIS_PAPER_BOOTSTRAPPED", {"cash_basis": "prvs_rcdl_excc_amt", "positions": wallet.positions})
        return self.store.status(self.account)

    def wallet(self, account: str) -> Wallet:
        self.assert_account(account)
        wallet, _ = self._balance()
        with self.store.transaction():
            self.store.save_wallet(account, "REMOTE", wallet)
        return wallet

    def quote(self, symbol: str) -> Quote:
        if not re.fullmatch(r"[0-9]{6}", symbol):
            raise AgentError("KIS paper supports six-digit Korean codes")
        result = self.session.request("quote", {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        output = result.get("output", {})
        if not isinstance(output, dict) or "stck_prpr" not in output:
            raise AgentError("Missing quote price")
        # Receipt time is explicit; this is not a verified exchange event timestamp.
        return Quote(symbol, dec(output["stck_prpr"]), datetime.now(UTC))

    def preflight(self, intent: OrderIntent, quotes: dict[str, Quote], now: datetime) -> None:
        self._validate_intent(intent, now)
        if intent.side == "BUY":
            result = self.session.request("buyable", {**self.session.account_params(), "PDNO": intent.symbol,
                "ORD_UNPR": str(int(intent.limit_price)), "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
            output = result.get("output", {})
            if not isinstance(output, dict) or "ord_psbl_cash" not in output or "nrcvb_buy_qty" not in output:
                raise AgentError("Missing no-credit buying-power fields")
            if intent.quantity > integer(output["nrcvb_buy_qty"], "nrcvb_buy_qty") or intent.quantity * intent.limit_price > dec(output["ord_psbl_cash"]):
                raise AgentError("Broker no-credit buying power is insufficient")
        else:
            _, sellable = self._balance()
            if intent.quantity > sellable.get(intent.symbol, 0):
                raise AgentError("Broker sellable quantity is insufficient")

    def _validate_intent(self, intent: OrderIntent, now: datetime) -> None:
        self.assert_account(intent.account)
        if not aware(intent.created_at) <= aware(now) < aware(intent.expires_at):
            raise AgentError("Paper intent is not yet valid or expired")
        local = aware(now).astimezone(KST)
        if local.weekday() >= 5 or not (9, 0) <= (local.hour, local.minute) < (15, 20):
            raise AgentError("Paper submission is limited to the configured regular-session window")
        if intent.symbol not in self.tick_sizes or intent.limit_price % self.tick_sizes[intent.symbol]:
            raise AgentError("Missing verified tick size or invalid limit-price tick")
        if intent.limit_price != intent.limit_price.to_integral_value():
            raise AgentError("KRW paper order price must be an integer")
        if self.issues(intent.account):
            raise AgentError("Resolve unverified broker fee/settlement contracts before another submission")

    def submit(self, intent: OrderIntent, now: datetime, lose_response: bool = False) -> str:
        self._validate_intent(intent, now)
        if lose_response:
            raise AgentError("Fault injection is supported only by the local simulator")
        result = self.session.request(intent.side.lower(), {**self.session.account_params(), "PDNO": intent.symbol,
            "ORD_DVSN": "00", "ORD_QTY": str(intent.quantity), "ORD_UNPR": str(int(intent.limit_price)),
            "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if intent.side == "SELL" else "", "CNDT_PRIC": ""})
        output = result.get("output", {})
        orgno, odno = output.get("KRX_FWDG_ORD_ORGNO"), output.get("ODNO")
        if not isinstance(orgno, str) or not orgno or not isinstance(odno, str) or not odno:
            raise AgentError("Paper acknowledgement lacks complete broker identity")
        return self._link(intent, aware(now).astimezone(KST).date(), orgno, odno)

    def _link(self, intent: OrderIntent, day: date, orgno: str, odno: str) -> str:
        broker_id = "KIS-PAPER:" + self.session.identity[:16] + ":" + day.isoformat() + ":" + orgno + ":" + odno
        with self.store.transaction():
            previous = self.store.db.execute("SELECT client_key FROM remote_orders WHERE broker_id=?", (broker_id,)).fetchone()
            if previous and previous[0] != intent.key:
                raise AgentError("Broker order is already linked to another intent")
            self.store.db.execute("INSERT OR IGNORE INTO remote_orders VALUES(?,?,?,?,?,?,?,?,0)",
                                  (broker_id, intent.key, intent.account, intent.symbol, intent.side, intent.quantity, str(intent.limit_price), "ACKED"))
            self.store.db.execute("INSERT OR IGNORE INTO kis_order_refs(broker_id,session,orgno,odno) VALUES(?,?,?,?)", (broker_id, day.isoformat(), orgno, odno))
        return broker_id

    def find(self, intent: OrderIntent) -> list[dict]:
        # KIS does not carry our client key. Even one similar order is not proof.
        self.assert_account(intent.account)
        return []

    def adopt(self, intent: OrderIntent, day: date, orgno: str, odno: str) -> str:
        self.assert_account(intent.account)
        if day != intent.created_at.astimezone(KST).date():
            raise AgentError("Reviewed order date differs from the intent's submission session")
        rows = [r for r in self.orders(day) if r.get("odno") == odno and r.get("ord_gno_brno") == orgno]
        if len(rows) != 1:
            raise AgentError("Cannot uniquely identify the explicitly reviewed broker order")
        self._validate_row(rows[0], intent)
        broker_id = self._link(intent, day, orgno, odno)
        self.store.event(intent.account, intent.key, "MANUALLY_LINKED_BROKER_IDENTITY", {"broker_id": broker_id})
        return broker_id

    def _validate_row(self, row: dict, intent: OrderIntent) -> None:
        if (row.get("pdno") != intent.symbol or row.get("sll_buy_dvsn_cd") != ("02" if intent.side == "BUY" else "01")
                or integer(row.get("ord_qty"), "ord_qty") != intent.quantity or dec(row.get("ord_unpr")) != intent.limit_price):
            raise AgentError("Broker order does not match the reviewed intent")

    def get(self, broker_id: str) -> dict:
        ref = self.store.db.execute("SELECT * FROM kis_order_refs WHERE broker_id=?", (broker_id,)).fetchone()
        stored = self.store.db.execute("SELECT * FROM remote_orders WHERE broker_id=?", (broker_id,)).fetchone()
        if not ref or not stored:
            raise AgentError("Unknown KIS paper broker identity")
        from .execution import intent_from_json
        intent = intent_from_json(self.store.order(stored["client_key"])["intent_json"])
        rows = [r for r in self.orders(date.fromisoformat(ref["session"])) if r.get("odno") == ref["odno"] and r.get("ord_gno_brno") == ref["orgno"]]
        if len(rows) != 1:
            raise AgentError("Acknowledged paper order is missing or ambiguous in query")
        row = rows[0]
        self._validate_row(row, intent)
        filled, remaining = integer(row.get("tot_ccld_qty"), "tot_ccld_qty"), integer(row.get("rmn_qty"), "rmn_qty")
        cancelled, rejected = integer(row.get("cncl_cfrm_qty"), "cncl_cfrm_qty"), integer(row.get("rjct_qty"), "rjct_qty")
        amount = dec(row.get("tot_ccld_amt"))
        if filled + remaining + cancelled + rejected != intent.quantity or amount < 0 or (filled == 0) != (amount == 0):
            raise AgentError("Paper cumulative quantities/notional are inconsistent")
        if filled and ((intent.side == "BUY" and amount > filled * intent.limit_price) or (intent.side == "SELL" and amount < filled * intent.limit_price)):
            raise AgentError("Paper cumulative fills violate the limit price")
        verified = row.get("tot_fee") not in (None, "")
        fee = dec(row["tot_fee"]) if verified else D(0)
        if fee < 0:
            raise AgentError("Negative cumulative fees are unsupported")
        state = "REJECTED" if rejected == intent.quantity else "FILLED" if filled == intent.quantity else ("CANCELLED" if remaining == 0 else (
            "CANCEL_PENDING" if ref["cancel_requested"] else "PARTIAL" if filled else "ACKED"))
        with self.store.transaction():
            old_qty = stored["filled_qty"]
            old_amount = sum((dec(r[0]) for r in self.store.db.execute("SELECT a.notional FROM fill_amounts a JOIN remote_fills f ON f.fill_id=a.fill_id WHERE a.ledger='REMOTE' AND f.broker_id=?", (broker_id,))), D(0))
            old_fee = sum((dec(r[0]) for r in self.store.db.execute("SELECT fee FROM remote_fills WHERE broker_id=?", (broker_id,))), D(0))
            if filled < old_qty or amount < old_amount or fee < old_fee:
                raise AgentError("Cumulative broker observation regressed")
            if filled == old_qty and (amount != old_amount or fee != old_fee):
                raise AgentError("Cash-only broker correction requires explicit review")
            if filled > old_qty:
                delta_qty, delta_amount, delta_fee = filled - old_qty, amount - old_amount, fee - old_fee
                if delta_amount <= 0:
                    raise AgentError("Positive fill delta requires positive notional")
                if (intent.side == "BUY" and delta_amount > delta_qty * intent.limit_price) or (intent.side == "SELL" and delta_amount < delta_qty * intent.limit_price):
                    raise AgentError("Incremental broker fill violates the limit price")
                fill_id = digest((broker_id, filled, amount))
                self.store.db.execute("INSERT INTO remote_fills VALUES(?,?,?,?,?)", (fill_id, broker_id, delta_qty, str(delta_amount / delta_qty), str(delta_fee)))
                self.store.db.execute("INSERT INTO fill_amounts VALUES(?,'REMOTE',?)", (fill_id, str(delta_amount)))
            self.store.db.execute("UPDATE remote_orders SET state=?,filled_qty=? WHERE broker_id=?", (state, filled, broker_id))
            self.store.db.execute("UPDATE kis_order_refs SET fee_verified=? WHERE broker_id=?", (int(verified or not filled), broker_id))
        return dict(self.store.db.execute("SELECT * FROM remote_orders WHERE broker_id=?", (broker_id,)).fetchone())

    def fills(self, broker_id: str) -> list[dict]:
        return [dict(r) for r in self.store.db.execute("SELECT f.*,a.notional FROM remote_fills f JOIN fill_amounts a ON a.fill_id=f.fill_id AND a.ledger='REMOTE' WHERE f.broker_id=? ORDER BY f.rowid", (broker_id,))]

    def issues(self, account: str) -> list[str]:
        self.assert_account(account)
        count = self.store.db.execute("SELECT count(*) FROM kis_order_refs r JOIN remote_orders o ON o.broker_id=r.broker_id WHERE o.account=? AND o.filled_qty>0 AND r.fee_verified=0", (account,)).fetchone()[0]
        return ["Actual cumulative fees were not supplied; zero is a provisional ledger value, not a verified fee"] if count else []

    def request_cancel(self, broker_id: str) -> None:
        if not self.cancelability_confirmed:
            raise AgentError("Paper cancelability contract is unverified; cancel POST is disabled")
        order = self.get(broker_id)
        if order["state"] in {"FILLED", "CANCELLED"}:
            return
        ref = self.store.db.execute("SELECT * FROM kis_order_refs WHERE broker_id=?", (broker_id,)).fetchone()
        row = next(r for r in self.orders(date.fromisoformat(ref["session"])) if r.get("odno") == ref["odno"] and r.get("ord_gno_brno") == ref["orgno"])
        # Official cancelability sample is real-only. Never invent a paper TR ID
        # or equate remaining quantity with verified cancelable quantity.
        if "psbl_qty" not in row or integer(row["psbl_qty"], "psbl_qty") < order["quantity"] - order["filled_qty"]:
            raise AgentError("Paper cancelability is unverified; a confirmed psbl_qty contract is required")
        self.store.db.execute("UPDATE kis_order_refs SET cancel_requested=1 WHERE broker_id=?", (broker_id,))
        self.session.request("cancel", {**self.session.account_params(), "KRX_FWDG_ORD_ORGNO": ref["orgno"], "ORGN_ODNO": ref["odno"],
            "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX"})
