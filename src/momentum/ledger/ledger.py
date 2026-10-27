"""Cent-exact accounting ledger.

Everything the project reports is arithmetic on this ledger's output, so the
authority for its behaviour is the hand-calculated fixture
``tests/fixtures/handcalc_ledger.csv`` — not this docstring.

Invariants, enforced every day by the golden tests:

* ``cash + positions_value + dividend_receivable == nav`` exactly (Decimal, cents)
* ``cash >= 0`` — no borrowing, ever
* no fill at a price that does not exist

``nav`` is NOT derived from the other three fields. It is tracked
independently, cash-flow by cash-flow (trade costs, realised gains/losses on
sells, dividend accruals, mark-to-market deltas), precisely so that the
accounting-identity test has a real chance to fail: if any mutation forgets to
book a cost, drops a receivable, or mis-marks a position, the independently
tracked ``nav`` and the recomputed ``cash + positions_value +
dividend_receivable`` diverge.

Fixed per-session processing order (``step`` / ``step_session``):
corporate actions -> sell fills -> recomputed available cash -> buy fills
(reduced when cash runs short) -> mark to market at the close.

Dividends are accrued as a receivable on the ex-date (where the raw close has
already dropped by the dividend) and only converted to cash on the pay date, so
net assets jump on neither day. That is the cheapest available detector for the
double-counting CLAUDE.md rule 4 warns about. A split multiplies the share
count and divides the per-share cost basis (and the stored mark), leaving net
assets unchanged.

Bonus issues and capital reductions follow policy B (user decision,
2026-10-20). A bonus issue (``split_ratio`` = new shares per old share)
entitles the holding at the ex-date open to ``floor(qty * ratio)`` shares that
cannot be sold until they list on ``pay_date``; until then they are pending
shares, marked to market like the holding and counted in ``positions_value``.
A capital reduction (``split_ratio`` = remaining shares per old share, below
one) takes effect on ``ex_date``, the relisting session. In both, a fractional
share is dropped at zero value (real issuers pay it in cash on rules that
vary), so net assets fall by exactly its value and never rise.

Calling contract for ``apply_corporate_actions``: a corporate-action row MUST
be presented to the ledger on BOTH its ex-date and its pay-date (the same
``CorporateAction`` tuple, passed again on the later date). The ex-date branch
only fires when ``ca.ex_date == d`` and the pay-date branch only when
``ca.pay_date == d``, so presenting the same action on both days is required,
not merely harmless -- omitting the pay-date presentation leaves the
receivable stuck forever, and omitting the ex-date presentation skips the
accrual entirely.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import NamedTuple

from momentum.config import ExecutionCfg, SpecUndefinedError
from momentum.execution.engine import Fill, execute
from momentum.portfolio.targets import TargetOrder

CENT = Decimal("0.01")


class CorporateAction(NamedTuple):
    security_id: str
    action_type: str  # "cash_dividend" | "split" | "bonus_issue" | "capital_reduction"
    ex_date: date
    pay_date: date | None
    split_ratio: Decimal | None
    cash_amount: Decimal | None


class Ledger:
    def __init__(self, starting_cash: Decimal, cfg: ExecutionCfg | None = None,
                 *, money_quantum: Decimal = CENT, fractional_split: str = "raise") -> None:
        # Existing USD callers retain cent-exact behavior. Other research
        # currencies must explicitly select a decimal power, never a tick size.
        if not money_quantum.is_finite() or money_quantum <= 0 or money_quantum.normalize().as_tuple().digits != (1,):
            raise ValueError("money_quantum must be a positive decimal power of ten")
        self.money_quantum = money_quantum.normalize()
        # A split that leaves a fraction of a share is undefined by default
        # (rule: never guess). KRX research opts into "drop": round down and
        # value the fraction at zero, the same rule as capital reductions
        # (policy B, 2026-10-21 user decision for reverse splits).
        if fractional_split not in ("raise", "drop"):
            raise ValueError("fractional_split must be 'raise' or 'drop'")
        self.fractional_split = fractional_split
        # (security, date, shares before, fraction of a share dropped at zero)
        self.fractions_dropped: list[tuple[str, date, int, Decimal]] = []
        self.cash: Decimal = Decimal(starting_cash).quantize(self.money_quantum, ROUND_HALF_UP)
        self.positions: dict[str, int] = {}
        self.cost_basis: dict[str, Decimal] = {}  # per share, includes trading cost
        self.marks: dict[str, Decimal] = {}  # per share, excludes trading cost
        # Last session date on which each held security's mark was refreshed
        # from an actual close (finding 8) -- lets callers tell "no close
        # today" (one missing day) apart from a persistent delisting (many).
        self.marks_asof: dict[str, date] = {}
        self.receivables: dict[tuple[str, date], Decimal] = {}
        # Holdings at the start of each processed session, before its fills.
        # A dividend whose amount is published after its ex-date is still owed
        # to whoever held the shares into that ex-date (see accrue_entitlement).
        self.holdings_at_open: dict[date, dict[str, int]] = {}
        # Bonus shares owed but not yet listed (policy B): (security, listing
        # date) -> shares, with the per-share cost basis they will carry.
        self.pending_shares: dict[tuple[str, date], int] = {}
        self.pending_basis: dict[tuple[str, date], Decimal] = {}
        # Positions written off at zero after a delisting: (security, date,
        # shares including pending bonus shares, last mark carried until then).
        self.written_off: list[tuple[str, date, int, Decimal]] = []
        self.fills: list[Fill] = []
        self.cfg: ExecutionCfg = cfg if cfg is not None else ExecutionCfg()
        self._nav: Decimal = self.cash

    # ---- state -------------------------------------------------------
    def pending_quantity(self, security_id: str) -> int:
        return sum(n for (sid, _), n in self.pending_shares.items() if sid == security_id)

    def _valued_quantities(self) -> dict[str, int]:
        held = {sid: qty for sid, qty in self.positions.items() if qty}
        for (sid, _), n in self.pending_shares.items():
            held[sid] = held.get(sid, 0) + n
        return held

    @property
    def positions_value(self) -> Decimal:
        total = Decimal("0.00")
        for sid, qty in self._valued_quantities().items():
            if qty == 0:
                continue
            mark = self.marks.get(sid)
            if mark is None:
                raise SpecUndefinedError(f"no mark price ever seen for held security {sid}")
            total += qty * mark
        return total.quantize(self.money_quantum)

    @property
    def dividend_receivable(self) -> Decimal:
        return sum(self.receivables.values(), Decimal("0.00")).quantize(self.money_quantum)

    @property
    def nav(self) -> Decimal:
        return self._nav

    def stale_positions_value(self, d: date) -> Decimal:
        """Portion of ``positions_value`` not refreshed by a close as of ``d``.

        A security whose ``marks_asof`` is behind ``d`` is being carried at a
        stale mark -- either a one-day feed gap (self-heals tomorrow) or a
        delisting (never heals). Tasks 12/13 can watch how long a security
        stays in this set to tell the two apart; this ledger does not guess.
        """
        total = Decimal("0.00")
        for sid, qty in self._valued_quantities().items():
            if qty == 0:
                continue
            if self.marks_asof.get(sid) != d:
                mark = self.marks.get(sid)
                if mark is not None:
                    total += qty * mark
        return total.quantize(self.money_quantum)

    # ---- the three phases --------------------------------------------
    def apply_corporate_actions(self, cas: list[CorporateAction], d: date) -> None:
        self.holdings_at_open.setdefault(d, {k: v for k, v in self.positions.items() if v})
        for ca in cas:
            if ca.action_type == "cash_dividend":
                if ca.pay_date is None:
                    raise SpecUndefinedError(f"dividend without pay_date: {ca}")
                if ca.ex_date == d:
                    if ca.cash_amount is None:
                        raise SpecUndefinedError(f"dividend without cash_amount: {ca}")
                    qty = self.positions.get(ca.security_id, 0)
                    if qty:
                        accrual = (qty * ca.cash_amount).quantize(self.money_quantum, ROUND_HALF_UP)
                        key = (ca.security_id, ca.pay_date)
                        self.receivables[key] = self.receivables.get(key, Decimal("0.00")) + accrual
                        self._nav += accrual
                if ca.pay_date == d:
                    key = (ca.security_id, ca.pay_date)
                    due = self.receivables.pop(key, Decimal("0.00"))
                    self.cash = (self.cash + due).quantize(self.money_quantum, ROUND_HALF_UP)
            elif ca.action_type == "split":
                if ca.ex_date != d:
                    continue
                if ca.split_ratio is None or ca.split_ratio <= 0:
                    raise SpecUndefinedError(f"split without a usable ratio: {ca}")
                qty = self.positions.get(ca.security_id, 0)
                if not qty:
                    continue
                new_qty = qty * ca.split_ratio
                if new_qty != new_qty.to_integral_value():
                    if self.fractional_split != "drop":
                        raise SpecUndefinedError(
                            f"split of {qty} shares by {ca.split_ratio} is not a whole "
                            f"number of shares for {ca.security_id}"
                        )
                    whole = new_qty.to_integral_value(ROUND_FLOOR)
                    fraction = new_qty - whole
                    mark = self.marks.get(ca.security_id)
                    if mark is not None:
                        # Marks rescale below; only the dropped fraction loses value.
                        self._nav -= fraction * mark / ca.split_ratio
                    self.fractions_dropped.append((ca.security_id, d, qty, fraction))
                    new_qty = whole
                    if not new_qty:
                        self.positions[ca.security_id] = 0
                        self.cost_basis.pop(ca.security_id, None)
                        self.marks.pop(ca.security_id, None)
                        self.marks_asof.pop(ca.security_id, None)
                        continue
                self.positions[ca.security_id] = int(new_qty)
                basis = self.cost_basis.get(ca.security_id)
                if basis is not None:
                    self.cost_basis[ca.security_id] = basis / ca.split_ratio
                mark = self.marks.get(ca.security_id)
                if mark is not None:
                    self.marks[ca.security_id] = mark / ca.split_ratio
            elif ca.action_type == "bonus_issue":
                self._bonus_issue(ca, d)
            elif ca.action_type == "capital_reduction":
                self._capital_reduction(ca, d)
            else:
                raise SpecUndefinedError(f"unknown corporate action type {ca.action_type!r}")

    def _bonus_issue(self, ca: CorporateAction, d: date) -> None:
        ratio, listing = ca.split_ratio, ca.pay_date
        if ratio is None or ratio <= 0 or listing is None or listing <= ca.ex_date:
            raise SpecUndefinedError(f"bonus issue needs a ratio and a later listing date: {ca}")
        key = (ca.security_id, listing)
        if ca.ex_date == d:
            qty = self.positions.get(ca.security_id, 0)
            if qty and self.pending_quantity(ca.security_id):
                raise SpecUndefinedError(
                    f"bonus issue on {ca.security_id} while earlier bonus shares are unlisted"
                )
            if qty:
                new = int((qty * ratio).to_integral_value(ROUND_FLOOR))
                factor = 1 + ratio
                mark = self.marks.get(ca.security_id)
                if mark is not None:
                    adjusted = mark / factor
                    self.marks[ca.security_id] = adjusted
                    self._nav += (qty + new) * adjusted - qty * mark
                basis = self.cost_basis.get(ca.security_id)
                if basis is not None:
                    self.cost_basis[ca.security_id] = basis * qty / (qty + new)
                if new:
                    self.pending_shares[key] = new
                    self.pending_basis[key] = self.cost_basis.get(ca.security_id, Decimal(0))
        if listing == d:
            new = self.pending_shares.pop(key, 0)
            basis = self.pending_basis.pop(key, Decimal(0))
            if new:
                held = self.positions.get(ca.security_id, 0)
                if not held:
                    self.cost_basis[ca.security_id] = basis
                self.positions[ca.security_id] = held + new

    def _capital_reduction(self, ca: CorporateAction, d: date) -> None:
        ratio = ca.split_ratio
        if ratio is None or not 0 < ratio < 1:
            raise SpecUndefinedError(f"capital reduction needs a ratio below one: {ca}")
        if ca.ex_date != d:
            return
        qty = self.positions.get(ca.security_id, 0)
        if not qty:
            return
        if self.pending_quantity(ca.security_id):
            raise SpecUndefinedError(
                f"capital reduction on {ca.security_id} while bonus shares are unlisted"
            )
        new = int((qty * ratio).to_integral_value(ROUND_FLOOR))
        mark = self.marks.get(ca.security_id)
        if mark is not None:
            adjusted = mark / ratio
            self._nav += new * adjusted - qty * mark
        if new == 0:
            self.positions[ca.security_id] = 0
            self.cost_basis.pop(ca.security_id, None)
            self.marks.pop(ca.security_id, None)
            self.marks_asof.pop(ca.security_id, None)
            return
        self.positions[ca.security_id] = new
        if mark is not None:
            self.marks[ca.security_id] = adjusted
        basis = self.cost_basis.get(ca.security_id)
        if basis is not None:
            self.cost_basis[ca.security_id] = basis * qty / new

    def accrue_entitlement(
        self, security_id: str, ex_date: date, cash_amount: Decimal, pay_date: date, d: date
    ) -> None:
        """Book a dividend announced after its ex-date, on the day it is known.

        The entitlement is the holding at the ex-date open (the same quantity
        an on-time dividend accrues on), but the receivable and NAV move only
        on ``d``, the first session after the amount became public, so no
        evaluation sees it earlier. Payment then follows the ordinary
        pay-date branch of ``apply_corporate_actions``.
        """
        if not d > ex_date or not pay_date > d:
            raise SpecUndefinedError(
                f"late dividend needs ex_date < recognition < pay_date: {ex_date}, {d}, {pay_date}"
            )
        if ex_date in self.holdings_at_open:
            qty = self.holdings_at_open[ex_date].get(security_id, 0)
        elif not self.holdings_at_open or ex_date < min(self.holdings_at_open):
            qty = 0  # the ledger did not exist on the ex-date: nothing was held
        else:
            raise SpecUndefinedError(f"ex-date {ex_date} was not processed; entitlement unknown")
        if not qty:
            return
        accrual = (qty * cash_amount).quantize(self.money_quantum, ROUND_HALF_UP)
        key = (security_id, pay_date)
        self.receivables[key] = self.receivables.get(key, Decimal("0.00")) + accrual
        self._nav += accrual

    def apply_fills(self, fills: list[Fill], d: date) -> None:
        for f in fills:
            self.fills.append(f)
            if f.shares == 0:
                continue
            notional = (f.shares * f.price).quantize(self.money_quantum, ROUND_HALF_UP)
            if f.side == "sell":
                held = self.positions.get(f.security_id, 0)
                if f.shares > held:
                    raise SpecUndefinedError(
                        f"sell of {f.shares} exceeds {held} held in {f.security_id}"
                    )
                old_mark = self.marks.get(f.security_id)
                if old_mark is None:
                    # Should not happen for a real holding -- every position
                    # gets a mark the moment it is bought -- but never invent
                    # a price silently (rule 3): fall back to the traded
                    # price itself, which books zero realised gain/loss
                    # rather than a fabricated one.
                    old_mark = f.price
                self._nav += f.shares * (f.price - old_mark) - f.cost
                remaining = held - f.shares
                self.positions[f.security_id] = remaining
                if remaining == 0:
                    self.cost_basis.pop(f.security_id, None)
                    if not self.pending_quantity(f.security_id):
                        # Unlisted bonus shares still need the mark.
                        self.marks.pop(f.security_id, None)
                        self.marks_asof.pop(f.security_id, None)
                self.cash = (self.cash + notional - f.cost).quantize(self.money_quantum, ROUND_HALF_UP)
            else:
                held = self.positions.get(f.security_id, 0)
                old_basis = self.cost_basis.get(f.security_id, Decimal(0))
                old_mark = self.marks.get(f.security_id, f.price)
                new_qty = held + f.shares
                self.cost_basis[f.security_id] = (
                    old_basis * held + notional + f.cost
                ) / new_qty
                # Blend the mark (cost-free valuation baseline) so that a
                # same-day mark-to-market delta prices only the genuine
                # move from acquisition to close, not the whole position
                # from a stale prior mark (see docstring / finding 1 math).
                # Unlisted bonus shares are valued at the same mark, so they
                # weigh in the blend too; otherwise NAV drifts by
                # pending x (price - mark) / valued (explore v3, 2026-10-23).
                valued = held + self.pending_quantity(f.security_id)
                self.marks[f.security_id] = (old_mark * valued + f.price * f.shares) / (
                    valued + f.shares
                )
                self.positions[f.security_id] = new_qty
                self._nav -= f.cost
                self.cash = (self.cash - notional - f.cost).quantize(self.money_quantum, ROUND_HALF_UP)
            if self.cash < 0:
                raise SpecUndefinedError(f"fill on {d} drove cash negative: {self.cash}")

    def mark_to_market(self, closes: dict[str, Decimal], d: date) -> None:
        # A missing close is not filled in (rule 3): the last known mark stands,
        # which is what a delisted position with no liquidation price must do.
        for sid, px in closes.items():
            qty = self.positions.get(sid, 0) + self.pending_quantity(sid)
            if qty:
                old_mark = self.marks.get(sid)
                if old_mark is not None:
                    self._nav += qty * (px - old_mark)
                self.marks_asof[sid] = d
            self.marks[sid] = px

    def write_off(self, security_id: str, d: date) -> tuple[int, Decimal] | None:
        """Value a delisted holding at zero; there is no price to sell it at.

        The last mark otherwise stands forever (mark_to_market), which would
        keep a delisted company at its final traded price. Pending bonus shares
        of the same security go with it. Returns (shares, last mark) or None.
        """
        shares = self.positions.get(security_id, 0) + self.pending_quantity(security_id)
        if not shares:
            return None
        mark = self.marks.get(security_id)
        if mark is None:
            raise SpecUndefinedError(f"no mark price ever seen for held security {security_id}")
        self._nav -= shares * mark
        self.positions[security_id] = 0
        for key in [k for k in self.pending_shares if k[0] == security_id]:
            del self.pending_shares[key]
            self.pending_basis.pop(key, None)
        self.marks[security_id] = Decimal(0)
        self.written_off.append((security_id, d, shares, mark))
        return shares, mark

    # ---- one session --------------------------------------------------
    def step_session(
        self,
        d: date,
        asof: date,
        actions: list[CorporateAction],
        orders: list[TargetOrder],
        opens: dict[str, Decimal | None],
        adv: dict[str, float],
        closes: dict[str, Decimal],
    ) -> None:
        """Typed entry point holding the fixed per-session pipeline.

        This is the form Task 10 should call directly with real ``Decimal``s,
        ``date``s and ``TargetOrder``s -- ``step(row)`` is a thin wrapper
        around this for the fixture's string-encoded rows and must stay
        public, but it was never required to be the only entry point.
        """
        self.apply_corporate_actions(actions, d)
        fills = execute(
            orders,
            opens,
            adv,
            self.cash,
            self.cfg,
            session_date=d,
            asof=asof,
            money_quantum=self.money_quantum,
        )
        self.apply_fills(fills, d)
        self.mark_to_market(closes, d)

    def step(self, row: dict[str, str]) -> None:
        from momentum.ledger._fixture_rows import parse_row

        d, asof, actions, orders, opens, adv, closes = parse_row(row)
        self.step_session(d, asof, actions, orders, opens, adv, closes)
