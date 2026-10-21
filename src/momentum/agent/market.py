"""Market profiles: the single place for exchange clock, money and trading limits.

Research code asks the profile for "the open on day D" or "the observation
cut-off on day D" instead of writing hours, so another market needs a new
profile rather than edits scattered through the evaluator. Changing a value
here changes the evaluation contract: studies evaluated under the old clock
cannot continue and a new study must be started. LOOP-01b moved the KRX
observation from 16:00 to 18:00 for that reason (2026-10-21 user decision).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_agent.contracts import KST, AgentError


@dataclass(frozen=True)
class MarketProfile:
    market_id: str
    currency: str
    money_quantum: str
    timezone: ZoneInfo
    # Orders planned on the prior close fill at the next open.
    execution_open: time
    # A selection decision is valid until shortly after the open.
    decision_valid_until: time
    # Earliest moment a day's final bar can exist.
    session_close: time
    # When final bars are normally published by the feed.
    final_bar_available: time
    # Decisions observe the market as of this time on the prior session. It is
    # not earlier than historical_bar_available: a decision must see the prior
    # close under the same E1 assumption that dates historical bars.
    observation_time: time
    # Validity of an end-of-day selection report.
    report_valid_until: time
    # Filings carry a date only; they become usable after this time.
    disclosure_available: time
    # Preregistered E1 assumption for historical supplier bars: day t's bar is
    # treated as public at this time on day t (the supplier itself publishes
    # later; live evidence uses the actual receipt time instead).
    historical_bar_available: time
    # Exchange daily price limit; beyond it a move is not a normal session.
    daily_price_limit: Decimal
    # Maximum share of the prior session's volume an order may take.
    max_volume_participation: Decimal
    # Total research and paper-execution capital ceiling.
    capital_cap: Decimal

    def at(self, day: date, moment: time) -> datetime:
        return datetime.combine(day, moment, self.timezone).astimezone(UTC)

    def open_at(self, day: date) -> datetime:
        return self.at(day, self.execution_open)

    def decision_deadline(self, day: date) -> datetime:
        return self.at(day, self.decision_valid_until)

    def close_at(self, day: date) -> datetime:
        return self.at(day, self.session_close)

    def final_bar_at(self, day: date) -> datetime:
        return self.at(day, self.final_bar_available)

    def observed_at(self, day: date) -> datetime:
        return self.at(day, self.observation_time)

    def report_deadline(self, day: date) -> datetime:
        return self.at(day, self.report_valid_until)

    def disclosure_at(self, day: date) -> datetime:
        return self.at(day, self.disclosure_available)

    def historical_bar_at(self, day: date) -> datetime:
        return self.at(day, self.historical_bar_available)


KRX = MarketProfile(
    market_id="KRX",
    currency="KRW",
    money_quantum="1",
    timezone=KST,
    execution_open=time(9, 0),
    decision_valid_until=time(10, 0),
    session_close=time(15, 30),
    final_bar_available=time(15, 40),
    observation_time=time(18, 0),
    report_valid_until=time(19, 0),
    disclosure_available=time(18, 0),
    historical_bar_available=time(18, 0),
    daily_price_limit=Decimal("0.30"),
    max_volume_participation=Decimal("0.01"),
    capital_cap=Decimal(1000000),
)

PROFILES = {KRX.market_id: KRX}


def profile(market: str) -> MarketProfile:
    try:
        return PROFILES[market]
    except KeyError:
        raise AgentError(f"Unsupported market profile: {market}") from None
