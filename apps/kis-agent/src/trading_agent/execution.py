"""Durable reservation, ambiguous submissions and idempotent fill reconciliation."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .brokers import Broker, BrokerRejected
from .contracts import (
    AgentError,
    Costs,
    D,
    OrderIntent,
    Quote,
    aware,
    canonical,
    dec,
    timestamp,
)
from .store import Store

OPEN_STATES = ("RESERVED", "SUBMITTING", "UNKNOWN", "ACKED", "PARTIAL", "CANCEL_PENDING")


class RiskRejected(AgentError):
    pass


@dataclass(frozen=True)
class RiskPolicy:
    """Local-demo limits only. No live policy is implied by these defaults."""

    allowlist: tuple[str, ...]
    max_gross: Decimal = D("0.8")
    max_position: Decimal = D("0.4")
    max_order_notional: Decimal = D("5000000")
    max_quote_age_seconds: int = 60
    version: str = "local-demo-risk-v1"

    def __post_init__(self) -> None:
        for name in ("max_gross", "max_position", "max_order_notional"):
            object.__setattr__(self, name, dec(getattr(self, name)))
        if not self.allowlist or len(set(self.allowlist)) != len(self.allowlist):
            raise AgentError("A unique explicit symbol allowlist is required")
        if not D(0) < dec(self.max_position) <= dec(self.max_gross) <= D(1):
            raise AgentError("Risk weights must be in (0,1] with position <= gross")
        if dec(self.max_order_notional) <= 0 or self.max_quote_age_seconds <= 0:
            raise AgentError("Risk notional and quote age must be positive")


def intent_from_json(text: str) -> OrderIntent:
    raw = json.loads(text)
    raw["limit_price"] = dec(raw["limit_price"])
    raw["created_at"] = timestamp(raw["created_at"])
    raw["expires_at"] = timestamp(raw["expires_at"])
    return OrderIntent(**raw)


class SimulatedBroker:
    """Independent durable remote state. Never connects to a brokerage service."""

    def __init__(self, store: Store, costs: Costs = Costs()) -> None:
        self.store, self.costs = store, costs

    def assert_account(self, account: str) -> None:
        self.store.bind_broker(account, "LOCAL_SIMULATED_PAPER", "local-simulator-v1")

    def preflight(self, intent: OrderIntent, quotes: dict[str, Quote], now: datetime) -> None:
        self.assert_account(intent.account)

    def wallet(self, account: str):
        self.assert_account(account)
        return self.store.wallet(account, "REMOTE")

    def issues(self, account: str) -> list[str]:
        return []

    def submit(self, intent: OrderIntent, now: datetime, lose_response: bool = False) -> str:
        broker_id = uuid.uuid4().hex
        with self.store.transaction():
            self.store.wallet(intent.account, "REMOTE")
            self.store.db.execute("INSERT INTO remote_orders VALUES(?,?,?,?,?,?,?,?,0)",
                                  (broker_id, intent.key, intent.account, intent.symbol, intent.side,
                                   intent.quantity, str(intent.limit_price), "ACKED"))
        if lose_response:
            raise TimeoutError("Injected response loss after broker acceptance")
        return broker_id

    def find(self, intent: OrderIntent) -> list[dict]:
        # Simulator-only correlation key; KIS native idempotency is NOT assumed.
        return [dict(r) for r in self.store.db.execute(
            "SELECT * FROM remote_orders WHERE account=? AND client_key=?", (intent.account, intent.key))]

    def get(self, broker_id: str) -> dict:
        row = self.store.db.execute("SELECT * FROM remote_orders WHERE broker_id=?", (broker_id,)).fetchone()
        if row is None:
            raise AgentError("Unknown broker order")
        return dict(row)

    def fills(self, broker_id: str) -> list[dict]:
        return [dict(r) for r in self.store.db.execute(
            "SELECT * FROM remote_fills WHERE broker_id=? ORDER BY rowid", (broker_id,))]

    def fill(self, broker_id: str, quantity: int, price: Decimal, fill_id: str | None = None) -> str:
        price = dec(price)
        fill_id = fill_id or uuid.uuid4().hex
        with self.store.transaction():
            existing = self.store.db.execute("SELECT * FROM remote_fills WHERE fill_id=?", (fill_id,)).fetchone()
            if existing:
                if (existing["broker_id"], existing["quantity"], dec(existing["price"])) != (broker_id, quantity, price):
                    raise AgentError("Conflicting duplicate fill")
                return fill_id
            order = self.get(broker_id)
            if order["state"] not in {"ACKED", "PARTIAL", "CANCEL_PENDING"}:
                raise AgentError("Cannot fill terminal order")
            if type(quantity) is not int or not 0 < quantity <= order["quantity"] - order["filled_qty"]:
                raise AgentError("Invalid remaining fill quantity")
            limit = dec(order["limit_price"])
            if (order["side"] == "BUY" and price > limit) or (order["side"] == "SELL" and price < limit):
                raise AgentError("Fill violates order limit")
            fee = self.costs.fee(order["side"], quantity, price)
            wallet = self.store.wallet(order["account"], "REMOTE")
            wallet.fill(order["symbol"], order["side"], quantity, price, fee)
            self.store.save_wallet(order["account"], "REMOTE", wallet)
            self.store.db.execute("INSERT INTO remote_fills VALUES(?,?,?,?,?)", (fill_id, broker_id, quantity, str(price), str(fee)))
            filled = order["filled_qty"] + quantity
            state = "FILLED" if filled == order["quantity"] else (
                "CANCEL_PENDING" if order["state"] == "CANCEL_PENDING" else "PARTIAL")
            self.store.db.execute("UPDATE remote_orders SET state=?,filled_qty=? WHERE broker_id=?", (state, filled, broker_id))
        return fill_id

    def request_cancel(self, broker_id: str) -> None:
        with self.store.transaction():
            order = self.get(broker_id)
            if order["state"] not in {"FILLED", "CANCELLED"}:
                self.store.db.execute("UPDATE remote_orders SET state='CANCEL_PENDING' WHERE broker_id=?", (broker_id,))

    def confirm_cancel(self, broker_id: str) -> None:
        with self.store.transaction():
            order = self.get(broker_id)
            if order["state"] == "CANCEL_PENDING":
                self.store.db.execute("UPDATE remote_orders SET state='CANCELLED' WHERE broker_id=?", (broker_id,))


class OMS:
    def __init__(self, store: Store, broker: Broker, policy: RiskPolicy, costs: Costs = Costs()) -> None:
        self.store, self.broker, self.policy, self.costs = store, broker, policy, costs

    def _risk_check(self, intent: OrderIntent, quotes: dict[str, Quote], now: datetime) -> None:
        now = aware(now)
        if self.store.halt_reason(intent.account):
            raise RiskRejected("Account is halted: " + self.store.halt_reason(intent.account))
        if not aware(intent.created_at) <= now < aware(intent.expires_at):
            raise RiskRejected("Order is not yet valid or expired")
        if intent.symbol not in self.policy.allowlist:
            raise RiskRejected("Symbol is outside the approved local allowlist")
        wallet = self.store.wallet(intent.account)
        needed = {intent.symbol} | {s for s, q in wallet.positions.items() if q}
        active = [dict(r) for r in self.store.db.execute("SELECT * FROM orders WHERE account=?", (intent.account,))
                  if r["state"] in OPEN_STATES and r["key"] != intent.key]
        needed |= {o["symbol"] for o in active}
        if not needed <= quotes.keys():
            raise RiskRejected("Missing price for portfolio or pending orders")
        marks = {}
        for symbol in needed:
            quote = quotes[symbol]
            age = (now - aware(quote.as_of)).total_seconds()
            if quote.symbol != symbol or not 0 <= age <= self.policy.max_quote_age_seconds:
                raise RiskRejected("Future or stale quote")
            marks[symbol] = quote.price
        equity = wallet.equity(marks)
        if equity <= 0:
            raise RiskRejected("Nonpositive account equity")
        exposure = {s: q * marks[s] for s, q in wallet.positions.items() if q}
        reserved_cash, reserved_sells = D(0), {}
        for order in active:
            remaining = order["quantity"] - order["filled_qty"]
            if order["side"] == "BUY":
                price = dec(order["limit_price"])
                reserved_cash += remaining * price + self.costs.reserve_fee("BUY", remaining, price)
                exposure[order["symbol"]] = exposure.get(order["symbol"], D(0)) + remaining * max(price, marks[order["symbol"]])
            else:
                reserved_sells[order["symbol"]] = reserved_sells.get(order["symbol"], 0) + remaining
        notional = intent.quantity * intent.limit_price
        if notional > self.policy.max_order_notional:
            raise RiskRejected("Order exceeds notional limit")
        if intent.side == "BUY":
            if notional + self.costs.reserve_fee("BUY", intent.quantity, intent.limit_price) > wallet.cash - reserved_cash:
                raise RiskRejected("Insufficient unreserved cash")
            exposure[intent.symbol] = exposure.get(intent.symbol, D(0)) + intent.quantity * max(intent.limit_price, marks[intent.symbol])
            if exposure[intent.symbol] > equity * self.policy.max_position:
                raise RiskRejected("Position limit including pending buys exceeded")
            if sum(exposure.values(), D(0)) > equity * self.policy.max_gross:
                raise RiskRejected("Gross limit including pending buys exceeded")
        elif intent.quantity > wallet.positions.get(intent.symbol, 0) - reserved_sells.get(intent.symbol, 0):
            raise RiskRejected("Insufficient unreserved sellable quantity")

    def reserve(self, intent: OrderIntent, quotes: dict[str, Quote], now: datetime) -> dict:
        with self.store.transaction():
            self.broker.assert_account(intent.account)
            row = self.store.db.execute("SELECT intent_json FROM orders WHERE key=?", (intent.key,)).fetchone()
            if row:
                if row[0] != canonical(intent):
                    raise AgentError("Idempotency key reused with a different order payload")
                return self.store.order(intent.key)
            self._risk_check(intent, quotes, now)
            self.store.db.execute("INSERT INTO orders(key,account,symbol,side,quantity,limit_price,state,intent_json) VALUES(?,?,?,?,?,?,?,?)",
                                  (intent.key, intent.account, intent.symbol, intent.side, intent.quantity,
                                   str(intent.limit_price), "RESERVED", canonical(intent)))
            self.store.event(intent.account, intent.key, "RESERVED", {"risk_policy": self.policy, "cost_model": self.costs})
        return self.store.order(intent.key)

    def submit(self, key: str, quotes: dict[str, Quote], now: datetime, lose_response: bool = False) -> dict:
        pending = self.store.order(key)
        if pending["state"] != "RESERVED":
            return pending
        # Read-only network preflight happens before the durable send marker. Its
        # failure leaves RESERVED; an unknown write result is handled separately.
        self.broker.preflight(intent_from_json(pending["intent_json"]), quotes, now)
        with self.store.transaction():
            order = self.store.order(key)
            if order["state"] != "RESERVED":
                return order  # UNKNOWN and SUBMITTING are never automatically retransmitted.
            intent = intent_from_json(order["intent_json"])
            try:
                self._risk_check(intent, quotes, now)
            except RiskRejected as exc:
                self.store.db.execute("UPDATE orders SET state='REJECTED' WHERE key=?", (key,))
                self.store.event(intent.account, key, "REJECTED_BEFORE_SEND", {"reason": str(exc)})
                return self.store.order(key)
            self.store.db.execute("UPDATE orders SET state='SUBMITTING' WHERE key=?", (key,))
            self.store.event(intent.account, key, "SUBMITTING")
        try:
            broker_id = self.broker.submit(intent, now, lose_response=lose_response)
        except BrokerRejected:
            with self.store.transaction():
                self.store.db.execute("UPDATE orders SET state='REJECTED' WHERE key=?", (key,))
                self.store.event(intent.account, key, "BROKER_REJECTED")
        except Exception:
            # Log classification, not arbitrary transport error text that might contain secrets.
            with self.store.transaction():
                self.store.db.execute("UPDATE orders SET state='UNKNOWN' WHERE key=?", (key,))
                if not self.store.halt_reason(intent.account):
                    self.store.halt(intent.account, "ambiguous_submission")
                self.store.event(intent.account, key, "UNKNOWN")
        else:
            with self.store.transaction():
                self.store.db.execute("UPDATE orders SET state='ACKED',broker_id=? WHERE key=?", (broker_id, key))
                self.store.event(intent.account, key, "ACKED", {"broker_id": broker_id})
        return self.store.order(key)

    def cancel(self, key: str) -> dict:
        with self.store.transaction():
            order = self.store.order(key)
            if order["state"] == "RESERVED":
                self.store.db.execute("UPDATE orders SET state='CANCELLED' WHERE key=?", (key,))
                self.store.event(order["account"], key, "CANCELLED_BEFORE_SEND")
                return self.store.order(key)
            if order["state"] not in {"ACKED", "PARTIAL", "CANCEL_PENDING"} or not order["broker_id"]:
                raise AgentError("Cannot request cancellation before resolving broker identity/state")
            self.store.db.execute("UPDATE orders SET state='CANCEL_PENDING' WHERE key=?", (key,))
            self.store.event(order["account"], key, "CANCEL_REQUESTED")
        self.broker.request_cancel(order["broker_id"])
        return self.store.order(key)

    def reconcile(self, account: str) -> dict:
        # A wrong backend is a caller error, not evidence of an account mismatch.
        self.broker.assert_account(account)
        try:
            return self._reconcile(account)
        except Exception:
            with self.store.transaction():
                self.store.halt(account, "reconciliation_error_requires_review")
                self.store.event(account, None, "RECONCILIATION_FAILED")
            raise

    def _reconcile(self, account: str) -> dict:
        unresolved = []
        orders = [dict(r) for r in self.store.db.execute("SELECT * FROM orders WHERE account=?", (account,))]
        for local in orders:
            if local["state"] in {"RESERVED", "REJECTED"} or (local["state"] == "CANCELLED" and not local["broker_id"]):
                continue
            matches = [self.broker.get(local["broker_id"])] if local["broker_id"] else self.broker.find(intent_from_json(local["intent_json"]))
            if len(matches) != 1:
                unresolved.append(local["key"])
                with self.store.transaction():
                    self.store.db.execute("UPDATE orders SET state='UNKNOWN' WHERE key=?", (local["key"],))
                continue
            remote = matches[0]
            # Validate correlation before touching the local ledger.
            if any(remote[f] != local[f] for f in ("account", "symbol", "side", "quantity")) or dec(remote["limit_price"]) != dec(local["limit_price"]):
                raise AgentError("Broker order correlation mismatch")
            with self.store.transaction():
                wallet = self.store.wallet(account)
                for fill in self.broker.fills(remote["broker_id"]):
                    prior = self.store.db.execute("SELECT * FROM fills WHERE fill_id=?", (fill["fill_id"],)).fetchone()
                    if prior:
                        if (prior["order_key"], prior["quantity"], prior["price"], prior["fee"]) != (local["key"], fill["quantity"], fill["price"], fill["fee"]):
                            raise AgentError("Conflicting repeated broker fill")
                        prior_amount = self.store.db.execute("SELECT notional FROM fill_amounts WHERE fill_id=? AND ledger='LOCAL'", (fill["fill_id"],)).fetchone()
                        if fill.get("notional") is not None and (prior_amount is None or dec(prior_amount[0]) != dec(fill["notional"])):
                            raise AgentError("Conflicting repeated fill notional")
                        continue
                    wallet.fill(local["symbol"], local["side"], fill["quantity"], dec(fill["price"]), dec(fill["fee"]),
                                dec(fill["notional"]) if fill.get("notional") is not None else None)
                    self.store.db.execute("INSERT INTO fills VALUES(?,?,?,?,?)", (fill["fill_id"], local["key"], fill["quantity"], fill["price"], fill["fee"]))
                    if fill.get("notional") is not None:
                        self.store.db.execute("INSERT INTO fill_amounts VALUES(?,'LOCAL',?)", (fill["fill_id"], str(fill["notional"])))
                    self.store.event(account, local["key"], "FILL", fill)
                total = self.store.db.execute("SELECT COALESCE(sum(quantity),0) FROM fills WHERE order_key=?", (local["key"],)).fetchone()[0]
                if total != remote["filled_qty"] or total > local["quantity"]:
                    raise AgentError("Broker cumulative fill quantity mismatch")
                self.store.save_wallet(account, "LOCAL", wallet)
                if local["state"] != remote["state"] or local["filled_qty"] != total:
                    self.store.event(account, local["key"], "RECONCILED", {"state": remote["state"], "filled_qty": total})
                self.store.db.execute("UPDATE orders SET state=?,broker_id=?,filled_qty=? WHERE key=?",
                                      (remote["state"], remote["broker_id"], total, local["key"]))
        local_wallet, remote_wallet = self.store.wallet(account), self.broker.wallet(account)
        broker_issues = self.broker.issues(account)
        matches = local_wallet.cash == remote_wallet.cash and local_wallet.positions == remote_wallet.positions
        with self.store.transaction():
            if not matches:
                self.store.halt(account, "account_mismatch_requires_review")
            elif broker_issues:
                self.store.halt(account, "broker_contract_requires_review")
            elif unresolved and not self.store.halt_reason(account):
                self.store.halt(account, "ambiguous_submission")
            elif not unresolved and self.store.halt_reason(account) == "ambiguous_submission":
                self.store.halt(account, "")
            self.store.event(account, None, "ACCOUNT_RECONCILED", {"matches": matches, "unresolved": unresolved})
        result = {"matches": matches, "unresolved": unresolved, "halt_reason": self.store.halt_reason(account)}
        if broker_issues:
            result["broker_issues"] = broker_issues
        return result
