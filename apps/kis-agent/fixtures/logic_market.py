"""Synthetic full screening inputs. No exchange or historical security-master claim."""

from dataclasses import replace

from trading_agent.contracts import D, session_time
from trading_agent.data import Snapshot, synthetic_snapshot
from trading_agent.selection import SecurityHistory, SecurityState


def logic_market(count=220):
    original = synthetic_snapshot(count)
    rows = list(original.bars)
    for bar in original.bars:
        if bar.symbol != "DEMO_A":
            continue
        rows.extend([replace(bar, symbol="TEST_MANAGED"), replace(bar, symbol="TEST_ILLIQUID", volume=10),
                     replace(bar, symbol="TEST_EXPENSIVE", open=bar.open*100, high=bar.high*100, low=bar.low*100, close=bar.close*100),
                     replace(bar, symbol="TEST_SUSPENDED")])
        if bar.session in original.sessions[-10:]:
            rows.append(replace(bar, symbol="TEST_NEW"))
    snapshot = Snapshot(rows, require_complete=False)
    records = [SecurityState(bar.symbol, session_time(bar.session, 15, 30), session_time(bar.session, 15, 40),
                             "SUSPENDED" if bar.symbol == "TEST_SUSPENDED" else "NORMAL",
                             "ETF" if bar.symbol == "DEMO_C" else "STOCK", "KOSPI",
                             bar.symbol == "TEST_MANAGED", False, False, False,
                             bar.close * D(".9995"), bar.close * D("1.0005"), "SYNTHETIC_SECURITY_FIXTURE") for bar in snapshot.bars]
    return snapshot, SecurityHistory(records)


def cyclic_logic_market(count=400):
    from fixtures.markets import release_market
    original = release_market(count)
    snapshot = Snapshot([replace(b, open=b.open*100, high=b.high*100, low=b.low*100, close=b.close*100) for b in original.bars])
    records = [SecurityState(b.symbol, session_time(b.session, 15, 30), session_time(b.session, 15, 40), "NORMAL", "STOCK", "KOSPI",
                             False, False, False, False, b.close*D(".9995"), b.close*D("1.0005"), "ENGINEERED_CYCLE_FIXTURE") for b in snapshot.bars]
    return snapshot, SecurityHistory(records)
