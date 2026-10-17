"""Historical local paper replay through the durable OMS, never KIS orders."""

from __future__ import annotations

import json
from datetime import timedelta

from .contracts import (
    AgentError,
    Costs,
    D,
    OrderIntent,
    Quote,
    StrategySpec,
    canonical,
    dec,
    digest,
    session_time,
)
from .data import Snapshot
from .execution import OMS, RiskPolicy, SimulatedBroker, intent_from_json
from .store import Store
from .strategy import decide, fit_buy_quantity, rebalance_plan


def paper_replay(snapshot: Snapshot, spec: StrategySpec, costs: Costs, store: Store,
                 account: str = "demo", initial_cash=D("10000000"), last_only: bool = False) -> dict:
    store.create_account(account, initial_cash)
    broker = SimulatedBroker(store, costs)
    policy = RiskPolicy(snapshot.symbols)
    oms = OMS(store, broker, policy, costs)
    binding_key = "replay-binding:" + account
    binding = digest((snapshot.id, spec.version, costs, initial_cash, last_only))
    with store.transaction():
        previous = store.db.execute("SELECT value FROM meta WHERE key=?", (binding_key,)).fetchone()
        if previous and previous[0] != binding:
            raise AgentError("Replay account is bound to its snapshot/strategy/costs/mode; use a new account")
        store.db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (binding_key, binding))
    existing = store.db.execute("SELECT DISTINCT json_extract(intent_json,'$.snapshot_id') FROM orders WHERE account=?", (account,)).fetchall()
    if any(row[0] != snapshot.id for row in existing):
        raise AgentError("Historical replay account is bound to one snapshot; use a new account for another dataset")
    before = oms.reconcile(account)
    if before["halt_reason"]:
        raise AgentError("Resolve account halt before replay: " + before["halt_reason"])
    indices = [len(snapshot.sessions) - 1] if last_only else range(1, len(snapshot.sessions))
    rejections = []
    for i in indices:
        day, signal_day = snapshot.sessions[i], snapshot.sessions[i - 1]
        now = session_time(day, 9)
        # Persist completion markers, including cash-only sessions, for restart safety.
        marker = "replay:" + digest((account, snapshot.id, spec.version, costs, day))
        if store.db.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
            continue
        view = snapshot.view(session_time(signal_day, 16), signal_day)
        decision = decide(spec, view, now + timedelta(hours=1))
        marks = {s: b.open for s, b in snapshot.day(day).items()}
        quotes = {s: Quote(s, price, now) for s, price in marks.items()}
        prior_closes = {s: h[-1].close for s, h in view.histories.items() if h and h[-1].session == signal_day}
        caps = {s: int(view.histories[s][-1].volume * D("0.01")) for s in snapshot.symbols}
        plan_key = marker + ":plan"
        # Persist the initial plan before fills change equity or holdings. Recomputing
        # a partially completed plan can create a spurious reverse trade on restart.
        with store.transaction():
            saved_plan = store.db.execute("SELECT value FROM meta WHERE key=?", (plan_key,)).fetchone()
            if saved_plan:
                plan = [(s, side, q, dec(p)) for s, side, q, p in json.loads(saved_plan[0])]
            else:
                desired = rebalance_plan(store.wallet(account), decision, prior_closes, costs, caps)
                plan = [(s, side, q, costs.execution_price(side, marks[s]))
                        for s, side, q, _signal_price in desired if s in marks]
                store.db.execute("INSERT INTO meta VALUES(?,?)", (plan_key, canonical(plan)))
        for symbol, side, quantity, price in plan:
            key = digest((account, decision.strategy_version, snapshot.id, decision.as_of, symbol, side))
            prior = store.db.execute("SELECT intent_json FROM orders WHERE key=?", (key,)).fetchone()
            if prior:
                intent = intent_from_json(prior[0])
            else:
                if side == "BUY":
                    quantity = fit_buy_quantity(store.wallet(account), symbol, quantity, price, marks, costs,
                                                policy.max_gross, policy.max_position, policy.max_order_notional)
                if not quantity:
                    continue
                intent = OrderIntent(key, account, symbol, side, quantity, price, now, decision.valid_until,
                                     decision.strategy_version, snapshot.id)
            try:
                oms.reserve(intent, quotes, now)
            except AgentError as exc:
                rejections.append({"session": day.isoformat(), "symbol": symbol, "reason": str(exc)})
                continue
            order = oms.submit(key, quotes, now)
            if order["state"] in {"ACKED", "PARTIAL"}:
                remote = broker.get(order["broker_id"])
                remaining = remote["quantity"] - remote["filled_qty"]
                if remaining:
                    broker.fill(remote["broker_id"], remaining, intent.limit_price)
                result = oms.reconcile(account)
                if result["halt_reason"]:
                    raise AgentError("Account halt during replay")
            elif order["state"] not in {"FILLED", "CANCELLED", "REJECTED"}:
                raise AgentError("Unresolved replay order; rerun reconciliation before continuing")
        with store.transaction():
            store.db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (marker, day.isoformat()))
    reconciliation = oms.reconcile(account)
    marks = {s: b.close for s, b in snapshot.day(snapshot.sessions[-1]).items()}
    return {**store.status(account), "snapshot_id": snapshot.id, "strategy_version": spec.version,
            "final_equity": str(store.wallet(account).equity(marks)), "reconciliation": reconciliation,
            "rejections": rejections, "risk_policy": policy,
            "limitations": ["Historical replay with local simulated broker, not KIS paper-trading verification",
                            "SQLite single-host journal; no distributed writer lease or production failover",
                            "Order/exposure controls only; portfolio daily-loss/drawdown breakers are not yet implemented"]}
