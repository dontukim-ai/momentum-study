"""Explicit KIS paper commands; local replay remains a separate command/backend."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .contracts import (
    UTC,
    AgentError,
    Costs,
    OrderIntent,
    canonical,
    dec,
    digest,
    implementation_id,
)
from .execution import OMS, RiskPolicy, intent_from_json
from .kis_paper import KisPaperBroker, PaperSession
from .store import Store


def load_policy(path: Path) -> tuple[RiskPolicy, dict]:
    try:
        if path.stat().st_size > 32768:
            raise AgentError("Paper policy exceeds size limit")
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or set(raw) != {"risk", "tick_sizes"}:
            raise AgentError("Policy requires exactly risk and tick_sizes")
        risk = dict(raw["risk"])
        risk["allowlist"] = tuple(risk["allowlist"])
        if any(not re.fullmatch(r"[0-9]{6}", s) for s in risk["allowlist"]):
            raise AgentError("Only six-digit instrument contracts are supported")
        policy = RiskPolicy(**risk)
        ticks = {s: dec(t) for s, t in raw["tick_sizes"].items()}
        if set(ticks) != set(policy.allowlist) or any(t <= 0 for t in ticks.values()):
            raise AgentError("Each allowed instrument requires an explicit positive tick size")
        return policy, ticks
    except (KeyError, TypeError, ValueError):
        raise AgentError("Invalid paper-only policy JSON") from None


def run_paper_command(args, session_factory=PaperSession) -> dict:
    if args.action == "prepare":
        if not all((args.symbol, args.side, args.quantity, args.price, args.output)):
            raise AgentError("prepare requires symbol, side, quantity, price and output")
        if not re.fullmatch(r"[0-9]{6}", args.symbol) or not 1 <= args.ttl <= 300:
            raise AgentError("Use a six-digit symbol and intent TTL 1..300 seconds")
        now = datetime.now(UTC)
        intent = OrderIntent(digest((args.account, args.symbol, args.side, now)), args.account,
                             args.symbol, args.side, args.quantity, dec(args.price), now,
                             now + timedelta(seconds=args.ttl), "manual-paper:" + implementation_id(), "manual-paper-intent")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise AgentError("Intent file already exists; reuse it or choose a new path for a new decision")
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(canonical(intent) + "\n")
        return {"mode": "KIS_PAPER_INTENT_ONLY", "sent": False, "key": intent.key, "output": str(args.output.resolve())}
    store = Store(args.db)
    try:
        if args.action == "status":
            return store.status(args.account)
        session = session_factory(token_cache_dir=args.db.parent / ".secrets") if session_factory is PaperSession else session_factory()
        policy_key = "kis-paper-policy:" + args.account
        if args.action == "init":
            if not args.policy:
                raise AgentError("init requires a reviewed paper-only policy file")
            policy, ticks = load_policy(args.policy)
            value = canonical({"risk": policy, "tick_sizes": ticks})
            prior = store.db.execute("SELECT value FROM meta WHERE key=?", (policy_key,)).fetchone()
            if prior and prior[0] != value:
                raise AgentError("Initialized paper policy differs; changing it requires a separately reviewed migration")
            broker = KisPaperBroker(store, session, args.account, ticks)
            result = broker.bootstrap()
            with store.transaction():
                store.db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (policy_key, value))
            return result
        prior = store.db.execute("SELECT value FROM meta WHERE key=?", (policy_key,)).fetchone()
        if not prior:
            raise AgentError("Initialize this KIS paper alias and policy first")
        raw = json.loads(prior[0])
        risk = raw["risk"]
        risk["allowlist"] = tuple(risk["allowlist"])
        policy = RiskPolicy(**risk)
        broker = KisPaperBroker(store, session, args.account, raw["tick_sizes"])
        broker.assert_account(args.account)
        oms = OMS(store, broker, policy, Costs())
        if args.action == "quote":
            if not args.symbol:
                raise AgentError("quote requires a symbol")
            return {"mode": "KIS_PAPER", "quote": broker.quote(args.symbol), "timestamp_basis": "HTTP_RECEIPT_NOT_EXCHANGE_EVENT"}
        if args.action == "orders":
            if args.day is None:
                raise AgentError("orders requires a query day")
            fields = ("ord_dt", "ord_gno_brno", "odno", "pdno", "sll_buy_dvsn_cd", "ord_qty", "ord_unpr", "tot_ccld_qty", "rmn_qty")
            return {"mode": "KIS_PAPER", "orders": [{k: row.get(k) for k in fields} for row in broker.orders(args.day)]}
        if args.action == "reconcile":
            return oms.reconcile(args.account)
        if args.action == "resume":
            report = oms.reconcile(args.account)
            if not report["matches"] or report["unresolved"] or report.get("broker_issues"):
                raise AgentError("Cannot resume: reconciliation or broker contracts remain unresolved")
            with store.transaction():
                store.event(args.account, None, "OPERATOR_RESUMED_AFTER_REVIEW", {"previous_halt": store.halt_reason(args.account)})
                store.halt(args.account, "")
            return store.status(args.account)
        if args.action == "adopt":
            if not all((args.order_key, args.day, args.orgno, args.odno)):
                raise AgentError("adopt requires order-key, day, orgno and odno verified by the operator")
            local = store.order(args.order_key)
            if local["account"] != args.account or local["state"] not in {"UNKNOWN", "SUBMITTING"}:
                raise AgentError("Only this account's unresolved submission can be adopted")
            broker_id = broker.adopt(intent_from_json(local["intent_json"]), args.day, args.orgno, args.odno)
            with store.transaction():
                store.db.execute("UPDATE orders SET broker_id=? WHERE key=?", (broker_id, args.order_key))
            return oms.reconcile(args.account)
        if args.action == "cancel":
            if not args.order_key or store.order(args.order_key)["account"] != args.account:
                raise AgentError("cancel requires this account's order-key")
            oms.cancel(args.order_key)
            return oms.reconcile(args.account)
        if args.action == "submit":
            if not args.intent or args.intent.stat().st_size > 65536:
                raise AgentError("submit requires an intent file no larger than 64 KiB")
            try:
                intent = intent_from_json(args.intent.read_text())
            except (KeyError, ValueError, TypeError):
                raise AgentError("Invalid paper intent file") from None
            if intent.account != args.account:
                raise AgentError("Intent account alias mismatch")
            result = oms.reconcile(args.account)
            if result["halt_reason"]:
                raise AgentError("Resolve the paper account halt before submitting a new order")
            symbols = set(store.wallet(args.account).positions) | {intent.symbol}
            symbols |= {r[0] for r in store.db.execute("SELECT DISTINCT symbol FROM orders WHERE account=? AND state IN ('RESERVED','ACKED','PARTIAL','CANCEL_PENDING','UNKNOWN','SUBMITTING')", (args.account,))}
            quotes = {symbol: broker.quote(symbol) for symbol in sorted(symbols)}
            now = datetime.now(UTC)
            oms.reserve(intent, quotes, now)
            order = oms.submit(intent.key, quotes, now)
            return {"mode": "KIS_PAPER", "order": order, "reconciliation": oms.reconcile(args.account)}
        raise AgentError("Unknown paper action")
    finally:
        store.close()
