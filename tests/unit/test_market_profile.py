"""MarketProfile reproduces the KRX behaviour that used to be code constants."""

import re
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from trading_agent.contracts import AgentError, session_time

from momentum.agent.contracts import AgentPolicy
from momentum.agent.market import KRX, profile


def test_krx_clock_matches_previous_constants_exactly():
    day = date(2024, 6, 3)
    assert KRX.open_at(day) == session_time(day, 9)
    assert KRX.decision_deadline(day) == session_time(day, 10)
    assert KRX.close_at(day) == session_time(day, 15, 30)
    assert KRX.final_bar_at(day) == session_time(day, 15, 40)
    assert KRX.observed_at(day) == session_time(day, 16)
    assert KRX.report_deadline(day) == session_time(day, 17)
    assert KRX.disclosure_at(day) == session_time(day, 18)


def test_krx_limits_and_money():
    assert (KRX.market_id, KRX.currency, KRX.money_quantum) == ("KRX", "KRW", "1")
    assert KRX.daily_price_limit == D("0.30")
    assert KRX.max_volume_participation == D("0.01")
    assert KRX.capital_cap == D(1000000)


def test_profile_lookup_and_policy_binding():
    assert profile("KRX") is KRX
    with pytest.raises(AgentError, match="Unsupported market profile"):
        profile("NYSE")
    with pytest.raises(AgentError, match="KRX/KRW only"):
        AgentPolicy(market="NYSE")
    with pytest.raises(AgentError, match="KRX/KRW only"):
        AgentPolicy(currency="USD")


def test_agent_code_has_no_market_clock_literals():
    # Market hours, limits and caps live in market.py only.
    pattern = re.compile(r"session_time\([^()]*,\s*\d+(\s*,\s*\d+)?\)")
    offenders = []
    for path in sorted(Path("src/momentum/agent").glob("*.py")) + [
        Path("src/momentum/krx/dart_events.py")
    ]:
        if path.name == "market.py":
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text) or 'D(".01")' in text or "Decimal(1000000)" in text:
            offenders.append(path.name)
        if path.name != "market.py" and re.search(r"time\(18,\s*0\)", text):
            offenders.append(path.name)
    assert offenders == []
