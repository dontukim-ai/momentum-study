"""End-to-end backtest CLI: wires calendar, universe, signal, targets,
execution and ledger into one run and writes its output files.

``run_backtest`` is the programmatic entry point (``momentum.cli.run_backtest``);
``main`` is the ``momentum run ...`` CLI wrapper around it. Only
``phase=development`` actually runs a backtest today:

* ``phase=holdout`` always raises ``HoldoutLockedError`` -- there is no
  frozen manifest yet (Task 13 builds that machinery; CLAUDE.md rule 5
  forbids running holdout without one).
* ``phase=walkforward`` raises ``SpecUndefinedError``: Task 13 owns the
  walk-forward harness (parameter selection on 2013-2020, then a single
  locked evaluation), and fabricating a naive walk-forward loop now would
  produce numbers nobody has designed the selection rule for yet. Refusing
  is the "simplest honest thing."
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import polars as pl

from momentum.asof import AsOfFrame
from momentum.calendar import TradingCalendar
from momentum.config import Config, Phase, SpecUndefinedError, load_config
from momentum.evaluate.decide import HoldoutLockedError
from momentum.ledger.ledger import CorporateAction, Ledger
from momentum.portfolio.targets import compute_targets
from momentum.runs.registry import build_run_record, write_run_record
from momentum.signals import REGISTRY
from momentum.signals.base import RANK_TABLE_SCHEMA
from momentum.synth import SynthMarket, generate_market
from momentum.universe import build_universe

__all__ = ["HoldoutLockedError", "main", "run_backtest"]

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
DEFAULT_N_SECURITIES = 20


def _parse_data_spec(data: str | None, default_seed: int) -> tuple[int, int]:
    """Parse ``"synth:seed=7"`` (the only data source this task supports).

    Returns ``(seed, n_securities)``. There is no real-data adapter (out of
    scope for Task 10) -- any other prefix is ``SpecUndefinedError``, never
    silently ignored.
    """
    if data is None:
        return default_seed, DEFAULT_N_SECURITIES
    if not data.startswith("synth:"):
        raise SpecUndefinedError(f"unsupported --data spec: {data!r} (only 'synth:...' exists)")
    body = data[len("synth:") :]
    params: dict[str, str] = {}
    for kv in body.split(","):
        if not kv:
            continue
        key, _, value = kv.partition("=")
        params[key] = value
    seed = int(params.get("seed", default_seed))
    n_securities = int(params.get("n_securities", DEFAULT_N_SECURITIES))
    return seed, n_securities


#: Fixed order the five SynthMarket frames are hashed in -- part of the hash
#: contract, so reordering this tuple changes every data_hash ever produced.
_MARKET_FRAME_ORDER = (
    "security_master",
    "daily_prices",
    "corporate_actions",
    "shares_history",
    "calendar",
)


def _data_hash(market: SynthMarket) -> str:
    """Hash the actual generated market content, not the parameters that made it.

    A hash of ``(seed, n_securities, start, end)`` cannot detect a changed
    RNG implementation, a numpy/polars version bump, or a bug fix to
    ``generate_market`` between freezing a manifest and opening the holdout
    sample -- exactly the tampering vector a freeze manifest exists to
    catch (see Task 3's ``config_hash``, which hashes raw config bytes for
    the same reason). Each frame is sorted by every one of its columns (a
    total order, so row order can never silently vary) and serialized in a
    fixed column order before hashing; the frame's own name is folded into
    the hash stream too, so dropping or reordering a frame changes the
    result as surely as changing its content does.
    """
    hasher = hashlib.sha256()
    for name in _MARKET_FRAME_ORDER:
        frame: pl.DataFrame = getattr(market, name)
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        ordered = frame.select(sorted(frame.columns))
        if ordered.height:
            ordered = ordered.sort(sorted(frame.columns))
        hasher.update(str(ordered.schema).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(ordered.write_csv().encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _decimal_map(df: pl.DataFrame, key_col: str, val_col: str) -> dict[str, Decimal]:
    return {
        row[key_col]: Decimal(str(row[val_col]))
        for row in df.select([key_col, val_col]).iter_rows(named=True)
        if row[val_col] is not None
    }


def run_backtest(
    *,
    hypothesis: str = "H4",
    phase: Phase = "development",
    data: str | None = None,
    out: Path,
    seed: int = 7,
) -> Path:
    """Run one complete backtest and write its output files under ``out``.

    Writes ``orders.csv, fills.csv, positions.csv, cash.csv, equity.csv,
    signals.csv, run.json`` and returns ``out``.
    """
    if phase == "holdout":
        raise HoldoutLockedError(
            "phase=holdout refused: no frozen evaluation manifest exists yet "
            "(Task 13 builds it). Results are not to be looked at before the "
            "rules are locked (CLAUDE.md rule 5)."
        )
    if phase == "walkforward":
        raise SpecUndefinedError(
            "phase=walkforward has no runner yet -- Task 13 owns the "
            "walk-forward parameter-selection harness. Refusing rather than "
            "fabricating a naive loop nobody has designed a selection rule for."
        )
    if phase != "development":
        raise SpecUndefinedError(f"unknown phase: {phase!r}")

    if hypothesis not in REGISTRY:
        raise SpecUndefinedError(f"unknown hypothesis: {hypothesis!r}; known: {sorted(REGISTRY)}")

    cfg: Config = load_config(CONFIG_DIR)
    window = cfg.validation.windows[phase]
    seed, n_securities = _parse_data_spec(data, seed)

    market: SynthMarket = generate_market(
        seed=seed, n_securities=n_securities, start=window.start, end=window.end
    )

    # The shared "prices" AsOfFrame carries point-in-time shares outstanding
    # for EVERY hypothesis, unconditionally -- not just for whichever one is
    # being run today. This is deliberate: CLAUDE.md rule 7 forbids a
    # hypothesis-specific data path, so H3's need for point-in-time share
    # counts is met by enriching the one frame every Signal already
    # receives, which H1/H2/H4 simply never read the extra columns of.
    #
    # The merged row's own ``available_at`` -- the column ``AsOfFrame`` gates
    # ALL visibility on -- is left exactly as ``daily_prices`` set it. It is
    # NOT widened to the later of the price's and the shares' publish dates:
    # doing that would couple H1/H2/H4's price visibility to how promptly
    # shares data happens to be published, even though those three signals
    # never read a shares column at all (an earlier version of this
    # function did exactly that, which is a real coupling, not a harmless
    # simplification -- see task-11-report.md Finding 3). Instead
    # ``shares_available_at`` is carried as its OWN column, and it is H3's
    # job (not this shared frame's) to apply an as-of check against it
    # before trusting a share count -- see ``momentum.signals.h3``.
    shares = market.shares_history.select(
        pl.col("security_id"),
        pl.col("effective_date").alias("session_date"),
        pl.col("shares_outstanding"),
        pl.col("available_at").alias("shares_available_at"),
    )
    merged_prices = market.daily_prices.join(shares, on=["security_id", "session_date"], how="left")
    prices_asof = AsOfFrame(merged_prices)
    master_asof = AsOfFrame(market.security_master)
    cal = TradingCalendar(market.calendar["session_date"].to_list())
    sessions = cal.sessions(window.start, window.end)

    signal = REGISTRY[hypothesis]
    ledger = Ledger(starting_cash=Decimal(cfg.execution.account_equity_usd), cfg=cfg.execution)

    hyp_cfg = next((h for h in cfg.hypotheses.hypotheses if h.id == hypothesis), None)
    if hyp_cfg is None:
        raise SpecUndefinedError(
            f"hypothesis {hypothesis!r} has no entry in config/hypotheses.yaml"
        )
    rebalance_freq = hyp_cfg.rebalance

    # Signal observation dates are this hypothesis's rebalance-frequency
    # boundaries (every session for "daily", week-ends for "weekly",
    # month-ends for "monthly" -- see config/hypotheses.yaml); the trade
    # happens at the open of the NEXT session (never the observation day
    # itself). A boundary at the very end of the sample commonly has no
    # next session at all -- TradingCalendar.is_month_end()/is_week_end()
    # correctly return False for it, so it never even appears in
    # rebalance_dates(). Where it does appear but the calendar still runs
    # out immediately after, next_session(obs) is None and that rebalance is
    # skipped: a signal with no executable open cannot be traded. This means
    # len(orders scheduled) can be one less than the number of calendar
    # boundaries in [start, end] -- that is expected, not a bug, and this
    # loop does not assert the two counts are equal (it records the
    # difference in skipped_observations instead).
    obs_dates = cal.rebalance_dates(rebalance_freq, window.start, window.end)
    obs_to_exec: dict[date, date] = {}
    skipped_observations: list[dict[str, str]] = []
    for obs in obs_dates:
        exec_date = cal.next_session(obs)
        if exec_date is None:
            # No session exists after this observation to trade at -- see
            # docstring note above. Recorded (not asserted away) in run.json
            # so the vanishing last rebalance is visible as data.
            skipped_observations.append({"asof": obs.isoformat(), "reason": "no_next_session"})
            continue
        obs_to_exec[obs] = exec_date

    # Targets must be computed against the ledger's ACTUAL holdings at the
    # moment of observation -- which only reflects fills the ledger has
    # already executed. That means signal/target computation and daily
    # execution cannot run as two separate passes (a "plan everything, then
    # execute everything" split would compute every month's targets against
    # an always-empty ledger, since nothing would have traded yet): they
    # must interleave in one chronological walk over sessions, one session
    # at a time.
    scheduled: dict[date, tuple[list, date]] = {}
    signal_frames: list[pl.DataFrame] = []

    orders_rows: list[dict] = []
    fills_rows: list[dict] = []
    positions_rows: list[dict] = []
    cash_rows: list[dict] = []
    equity_rows: list[dict] = []

    ca_all = market.corporate_actions
    lookback = cfg.execution.participation_lookback_days

    for idx, d in enumerate(sessions):
        orders, obs = scheduled.get(d, ([], d))

        px_today = market.daily_prices.filter(pl.col("session_date") == d)
        opens: dict[str, Decimal | None] = dict(_decimal_map(px_today, "security_id", "open_raw"))
        closes = _decimal_map(px_today, "security_id", "close_raw")

        # ADV here is share volume (execution.costs.apply_caps expects
        # shares, not dollars -- the universe's dollar-ADV filter is a
        # separate, unrelated computation). By construction every security
        # that reaches an order has already cleared build_universe's
        # min_history_days=252 gate, which is well past the
        # participation_lookback_days=20 window this needs, so the ADV
        # entry required by momentum.execution.engine.execute for any live
        # order is always present -- see Task 10 report for the full
        # argument.
        needed = {o.security_id for o in orders}
        adv: dict[str, float] = {}
        if needed:
            lo = max(0, idx - lookback + 1)
            window_days = sessions[lo : idx + 1]
            adv_df = (
                market.daily_prices.filter(
                    pl.col("security_id").is_in(list(needed))
                    & pl.col("session_date").is_in(window_days)
                )
                .group_by("security_id")
                .agg(pl.col("volume").mean().alias("adv_shares"))
            )
            adv = dict(zip(adv_df["security_id"].to_list(), adv_df["adv_shares"].to_list()))

        actions = [
            CorporateAction(
                row["security_id"],
                row["action_type"],
                row["ex_date"],
                row["pay_date"],
                Decimal(str(row["split_ratio"])) if row["split_ratio"] is not None else None,
                Decimal(str(row["cash_amount"])) if row["cash_amount"] is not None else None,
            )
            for row in ca_all.filter(
                (pl.col("ex_date") == d) | (pl.col("pay_date") == d)
            ).iter_rows(named=True)
        ]

        for o in orders:
            orders_rows.append(
                {
                    "session_date": d,
                    "asof": obs,
                    "security_id": o.security_id,
                    "side": o.side,
                    "shares": o.shares,
                    "rank": o.rank,
                }
            )

        n_before = len(ledger.fills)
        ledger.step_session(d, obs, actions, orders, opens, adv, closes)
        for f in ledger.fills[n_before:]:
            fills_rows.append(
                {
                    "session_date": f.session_date,
                    "asof": f.asof,
                    "security_id": f.security_id,
                    "side": f.side,
                    "shares": f.shares,
                    "price": str(f.price),
                    "cost": str(f.cost),
                    "status": f.status,
                    "price_source": f.price_source,
                    "reduction_reasons": "|".join(f.reduction_reasons),
                }
            )

        # Observation happens AFTER today's own fills are booked, not
        # before. Under a daily rebalance cadence, today's OBSERVATION day
        # is also the EXECUTION day for yesterday's observation
        # (exec_date(obs) == next_session(obs), which for "daily" is simply
        # tomorrow -- so exec_date(d) == the very next d in this loop, i.e.
        # today's own scheduled orders were queued from yesterday's
        # iteration and must be executed above before this security's
        # holdings are read again here). Reading ``ledger.positions``
        # before today's ``step_session`` -- as an earlier version of this
        # loop did -- computes new targets against STALE holdings and can
        # schedule a sell for shares that are about to be sold again (or
        # were already sold) once today's own fills land, eventually
        # raising ``SpecUndefinedError: sell of N exceeds 0 held``. Monthly
        # cadences (H3, H4) never exposed this because an observation day's
        # exec_date is never itself another observation day; H1's daily
        # cadence always is.
        if d in obs_to_exec:
            obs_today = d
            exec_date = obs_to_exec[obs_today]

            universe = build_universe(prices_asof, master_asof, cal, cfg.universe, obs_today)
            rank_table = signal.rank(obs_today, universe, prices_asof)
            if rank_table.frame.height:
                signal_frames.append(rank_table.frame)

            ranked_ids = rank_table.frame.sort("rank")["security_id"].to_list()

            # Sizing uses ONLY the prior close (CLAUDE.md rule 1): the close
            # on the observation date itself, never the next day's open.
            px_obs = prices_asof.visible_at(obs_today).filter(pl.col("session_date") == obs_today)
            prev_close = _decimal_map(px_obs, "security_id", "close_raw")

            # The ledger's CURRENT positions -- i.e. as of everything
            # executed through TODAY's own session (including the fills
            # just booked above) -- not a snapshot from some earlier plan.
            holdings = dict(ledger.positions)

            # signals.csv records exactly what the signal produced -- the
            # RankTable -- and nothing else. A held security that falls out
            # of the top decile still generates a sell order this cycle
            # even though it is absent from THIS asof's rank_table; that
            # sell's provenance is the EARLIER asof at which the signal
            # selected it, not a row invented here. See test_cli.py's
            # test_every_sell_fill_was_selected_by_the_signal_at_some_earlier_asof.
            targets = compute_targets(ranked_ids, ledger.nav, prev_close, holdings, cfg.execution)
            scheduled[exec_date] = (targets, obs_today)

        for sid in sorted(ledger.positions):
            qty = ledger.positions[sid]
            if qty:
                positions_rows.append(
                    {
                        "session_date": d,
                        "security_id": sid,
                        "shares": qty,
                        "mark": str(ledger.marks.get(sid, "")),
                    }
                )

        cash_rows.append({"session_date": d, "cash": str(ledger.cash)})
        equity_rows.append({"session_date": d, "nav": str(ledger.nav)})

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    pl.DataFrame(
        orders_rows,
        schema={
            "session_date": pl.Date,
            "asof": pl.Date,
            "security_id": pl.Utf8,
            "side": pl.Utf8,
            "shares": pl.Int64,
            "rank": pl.Int64,
        },
    ).write_csv(out / "orders.csv")

    pl.DataFrame(
        fills_rows,
        schema={
            "session_date": pl.Date,
            "asof": pl.Date,
            "security_id": pl.Utf8,
            "side": pl.Utf8,
            "shares": pl.Int64,
            "price": pl.Utf8,
            "cost": pl.Utf8,
            "status": pl.Utf8,
            "price_source": pl.Utf8,
            "reduction_reasons": pl.Utf8,
        },
    ).write_csv(out / "fills.csv")

    pl.DataFrame(
        positions_rows,
        schema={
            "session_date": pl.Date,
            "security_id": pl.Utf8,
            "shares": pl.Int64,
            "mark": pl.Utf8,
        },
    ).write_csv(out / "positions.csv")

    pl.DataFrame(cash_rows, schema={"session_date": pl.Date, "cash": pl.Utf8}).write_csv(
        out / "cash.csv"
    )
    pl.DataFrame(equity_rows, schema={"session_date": pl.Date, "nav": pl.Utf8}).write_csv(
        out / "equity.csv"
    )

    if signal_frames:
        pl.concat(signal_frames).write_csv(out / "signals.csv")
    else:
        pl.DataFrame(schema=RANK_TABLE_SCHEMA).write_csv(out / "signals.csv")

    data_hash = _data_hash(market)
    record = build_run_record(
        phase=phase,
        config_hash=cfg.hash,
        data_hash=data_hash,
        seed=seed,
        accessed_holdout=False,
    )
    # The real observed/executed/skipped relationship, as data rather than a
    # bare assert (which would be stripped under -O and, worse, could never
    # actually fail given how it was previously written): a run whose sample
    # ends on a true month boundary silently drops its last rebalance
    # (TradingCalendar.is_month_end is False for the last known session),
    # and this makes that visible in run.json instead of hiding it.
    record["observation_dates"] = len(obs_dates)
    record["rebalances_executed"] = len(scheduled)
    record["skipped_observations"] = skipped_observations
    write_run_record(out, record)

    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="momentum")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run one complete backtest")
    run_p.add_argument("--hypothesis", required=True)
    run_p.add_argument("--phase", required=True, choices=["development", "walkforward", "holdout"])
    run_p.add_argument("--data", default=None)
    run_p.add_argument("--out", required=True)
    run_p.add_argument("--seed", type=int, default=7)

    args = parser.parse_args(argv)

    if args.command == "run":
        run_backtest(
            hypothesis=args.hypothesis,
            phase=args.phase,
            data=args.data,
            out=Path(args.out),
            seed=args.seed,
        )
