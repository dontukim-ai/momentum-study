import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fixtures.logic_market import cyclic_logic_market, logic_market
from trading_agent.cli import main
from trading_agent.contracts import (
    AgentError,
    Costs,
    D,
    Wallet,
    canonical,
    session_time,
)
from trading_agent.data import Snapshot, read_csv, write_csv
from trading_agent.evaluation import backtest
from trading_agent.logic import (
    LogicProposer,
    LogicSettings,
    abandon_cycle,
    evaluate_logic,
    init_study,
    logic_development_context,
    recover_cycle,
    run_logic_cycle,
    study_status,
)
from trading_agent.proposals import response_artifact
from trading_agent.selection import (
    RankedStrategy,
    SecurityHistory,
    SecurityState,
    SelectionEngine,
    SelectionPolicy,
    read_security_history,
    select_candidates,
    write_security_history,
)
from trading_agent.store import Store
from trading_agent.strategy import fit_buy_quantity


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market, cls.securities = logic_market(220)

    def screen(self, day_index=120, market=None, securities=None, spec=None, policy=None, equity=None, at=None):
        market, securities = market or self.market, securities or self.securities
        day = market.sessions[day_index]
        at = at or session_time(day, 16)
        return select_candidates(spec or RankedStrategy(), market.view(at, day, allow_missing=True), securities,
                                 policy or SelectionPolicy(), Costs(), at + timedelta(hours=1), equity)

    def test_filter_reasons_and_integer_affordability(self):
        report = self.screen(219)
        rows = {row["symbol"]: row for row in report["candidates"]}
        for symbol, reason in {"TEST_MANAGED": "MANAGED_SECURITY", "TEST_ILLIQUID": "LOW_LIQUIDITY",
                               "TEST_EXPENSIVE": "UNAFFORDABLE_WITHIN_POSITION_BUDGET", "TEST_NEW": "INSUFFICIENT_HISTORY",
                               "TEST_SUSPENDED": "SUSPENDED"}.items():
            self.assertIn(reason, rows[symbol]["reasons"])
            self.assertNotIn(symbol, report["selected"])
        for symbol in report["selected"]:
            row = rows[symbol]
            self.assertIsInstance(row["features"]["affordable_quantity"], int)
            self.assertLessEqual(row["features"]["affordable_quantity"] * row["features"]["price"], report["position_budget"])
        self.assertEqual(report["orders_submitted"], 0)

    def test_future_prices_and_status_changes_cannot_change_the_past_report(self):
        day = self.market.sessions[120]
        market = Snapshot([replace(b, open=b.open*100, high=b.high*100, low=b.low*100, close=b.close*100)
                           if b.session > day else b for b in self.market.bars], require_complete=False)
        at = session_time(day, 16)
        securities = SecurityHistory([replace(r, managed=True, status="SUSPENDED") if r.effective_at > at else r for r in self.securities.records])
        self.assertEqual(canonical(self.screen()), canonical(self.screen(market=market, securities=securities)))

    def test_late_published_halt_is_not_backdated(self):
        day = self.market.sessions[120]
        original = self.securities.states(session_time(day, 16))["DEMO_A"]
        revised = replace(original, status="SUSPENDED", available_at=session_time(day, 16, 5))
        history = SecurityHistory([*self.securities.records, revised])
        self.assertEqual(history.states(session_time(day, 16))["DEMO_A"].status, "NORMAL")
        self.assertEqual(history.states(session_time(day, 16, 10))["DEMO_A"].status, "SUSPENDED")

    def test_older_revised_event_does_not_replace_a_newer_effective_state(self):
        old = self.securities.states(session_time(self.market.sessions[119], 16))["DEMO_A"]
        revision = replace(old, status="SUSPENDED", available_at=session_time(self.market.sessions[120], 15, 50))
        history = SecurityHistory([*self.securities.records, revision])
        self.assertEqual(history.states(session_time(self.market.sessions[120], 16))["DEMO_A"].status, "NORMAL")

    def test_unknown_stale_spread_flags_and_missing_metadata_fail_closed(self):
        day = self.market.sessions[120]
        at = session_time(day, 16)
        records = [r for r in self.securities.visible(at) if r.symbol != "DEMO_C"]
        state = self.securities.states(at)["DEMO_A"]
        records = [r for r in records if r.symbol != "DEMO_A"] + [replace(state, bid=None, ask=None)]
        records = [replace(r, bid=r.bid/2) if r.symbol == "DEMO_B" else r for r in records]
        rows = {r["symbol"]: r for r in self.screen(securities=SecurityHistory(records))["candidates"]}
        self.assertIn("SPREAD_UNAVAILABLE", rows["DEMO_A"]["reasons"])
        self.assertIn("WIDE_SPREAD", rows["DEMO_B"]["reasons"])
        self.assertIn("SECURITY_METADATA_UNAVAILABLE", rows["DEMO_C"]["reasons"])
        old = SecurityHistory([r for r in self.securities.records if r.available_at < session_time(self.market.sessions[118], 16)])
        self.assertTrue(all("STALE_SECURITY_STATE" in r["reasons"] for r in self.screen(securities=old)["candidates"]))

    def test_all_rejected_stays_cash_and_profits_do_not_raise_capital_ceiling(self):
        report = self.screen(spec=RankedStrategy(min_momentum=D(100)), equity=D(2000000))
        self.assertEqual(report["selected"], [])
        self.assertEqual(report["cash_weight"], D(1))
        self.assertEqual(report["position_budget"], D(400000))
        wallet = Wallet(D(2000000))
        quantity = fit_buy_quantity(wallet, "A", 100000, D(10000), {"A": D(10000)}, Costs(), capital_limit=D(1000000))
        self.assertLessEqual(quantity * D(10000), D(400000))

    def test_single_selected_slot_keeps_unused_allocation_as_cash(self):
        report = self.screen()
        eligible = [r["symbol"] for r in report["candidates"] if r["status"] in {"SELECTED", "NOT_SELECTED"}]
        self.assertTrue(eligible)
        history = SecurityHistory([replace(r, managed=True) if r.symbol != eligible[0] else r for r in self.securities.records])
        selected = self.screen(securities=history)
        self.assertEqual(selected["selected"], [eligible[0]])
        self.assertEqual(selected["cash_weight"], D(".6"))

    def test_sparse_ipo_inputs_round_trip_and_missing_held_price_is_an_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"market.csv"
            write_csv(self.market, path)
            with self.assertRaisesRegex(AgentError, "Incomplete"):
                read_csv(path)
            self.assertEqual(read_csv(path, require_complete=False).id, self.market.id)
        day = self.market.sessions[120]
        missing = Snapshot([b for b in self.market.bars if not (b.session == day and b.symbol == "DEMO_A")], require_complete=False)
        view = missing.view(session_time(day, 16), day, allow_missing=True)
        with self.assertRaisesRegex(AgentError, "Missing position price"):
            SelectionEngine(self.securities, SelectionPolicy(), Costs())(RankedStrategy(), view, session_time(day, 17), Wallet(D(1), {"DEMO_A": 1}))

    def test_gaps_in_recent_nonheld_history_are_rejected_instead_of_compressing_time(self):
        missing_day=self.market.sessions[110]
        market=Snapshot([b for b in self.market.bars if not (b.symbol=="DEMO_A" and b.session==missing_day)],require_complete=False)
        row=next(r for r in self.screen(market=market)["candidates"] if r["symbol"]=="DEMO_A")
        self.assertIn("MISSING_RECENT_SESSIONS",row["reasons"])

    def test_security_file_is_strict_and_cannot_assume_missing_flags_are_clean(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"securities.jsonl"
            history = SecurityHistory(list(self.securities.records[:2]))
            write_security_history(history, path)
            self.assertEqual(read_security_history(path).id, history.id)
            raw = json.loads(path.read_text().splitlines()[0])
            del raw["managed"]
            path.write_text(json.dumps(raw) + "\n")
            with self.assertRaisesRegex(AgentError, "line 1"):
                read_security_history(path)
        with self.assertRaisesRegex(AgentError, "booleans"):
            replace(self.securities.records[0], managed="false")

    def test_high_volatility_can_change_ranking_without_changing_risk_allocation(self):
        from trading_agent.contracts import Bar
        bars, records = [], []
        day = self.market.sessions[0]
        prices = {"SMOOTH": D(10000), "NOISY": D(10000)}
        for i in range(80):
            day = self.market.sessions[i]
            for symbol in prices:
                opening = prices[symbol]
                rate = D("1.003") if symbol == "SMOOTH" else D("1.06") if i % 2 else D(".95")
                closing = opening * rate
                prices[symbol] = closing
                bars.append(Bar(day, symbol, opening, max(opening, closing), min(opening, closing), closing, 1000000, session_time(day, 15, 40)))
                records.append(SecurityState(symbol, session_time(day, 15, 30), session_time(day, 15, 40), "NORMAL", "STOCK", "KOSPI", False, False, False, False, closing, closing, "TEST"))
        market, securities = Snapshot(bars), SecurityHistory(records)
        a = self.screen(79, market, securities, RankedStrategy(10, 1, D(".4"), ranking="momentum"))
        b = self.screen(79, market, securities, RankedStrategy(10, 1, D(".4"), ranking="risk_adjusted"))
        self.assertEqual(a["selected"], ["NOISY"])
        self.assertEqual(b["selected"], ["SMOOTH"])
        self.assertEqual(a["position_budget"], b["position_budget"])

    def test_suspended_holding_cannot_be_fictitiously_sold_at_the_next_open(self):
        market, securities = cyclic_logic_market(100)
        halt_day, resume_day = market.sessions[65], market.sessions[69]
        price = market.bars[64].close
        changed = Snapshot([replace(b, open=price, high=price, low=price, close=price, volume=0)
                            if halt_day <= b.session < resume_day else b for b in market.bars])
        records = [replace(r,status="SUSPENDED") if session_time(halt_day,0) <= r.effective_at < session_time(resume_day,0)
                   else r for r in securities.records]
        original = securities.states(session_time(halt_day,16))["DEMO_CYCLE"]
        records.append(replace(original,status="SUSPENDED",effective_at=session_time(halt_day,8,30),available_at=session_time(halt_day,8,30)))
        history=SecurityHistory(records)
        policy=SelectionPolicy()
        engine=SelectionEngine(history,policy,Costs())
        result=backtest(changed,RankedStrategy(2),Costs(),D(1000000),market.sessions[61],market.sessions[75],
                        decision_engine=engine,execution_filter=engine.execution_allowed,evaluation_binding={"test":"halt"},
                        risk_policy=policy.risk(changed.symbols),capital_limit=policy.capital_limit)
        self.assertTrue(any(row["positions"] for row in result.equity_curve if row["session"] < str(halt_day)))
        self.assertFalse(any(str(halt_day) <= fill["session"] < str(resume_day) for fill in result.trade_log))
        quantities=[row["positions"] for row in result.equity_curve if str(halt_day) <= row["session"] < str(resume_day)]
        self.assertTrue(quantities and all(q==quantities[0] and q for q in quantities))

    def test_custom_financial_limits_cannot_share_an_unbound_legacy_evaluation_identity(self):
        with self.assertRaisesRegex(AgentError,"binding"):
            backtest(self.market,RankedStrategy(),capital_limit=D(1000000))


class LogicWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market, cls.securities = cyclic_logic_market()
        cls.settings = LogicSettings(champion=RankedStrategy(20), lookbacks=(2,20), rankings=("momentum",))
        cls.random_market, cls.random_securities = logic_market()

    def setUp(self):
        self.store = Store(":memory:")
        init_study(self.store, "test", self.settings)

    def tearDown(self):
        self.store.close()

    def run_cycle(self, **kwargs):
        return run_logic_cycle(self.store, "test", self.market, self.securities, **kwargs)

    def test_success_updates_only_research_champion_and_rerun_is_idempotent(self):
        result = self.run_cycle()
        self.assertTrue(result["research_champion_updated"])
        self.assertEqual(result["revision_after"], 1)
        self.assertEqual(study_status(self.store, "test")["champion"]["lookback"], 2)
        self.assertTrue(self.run_cycle()["cached_cycle"])
        for table in ("orders", "accounts", "shadow_slots"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(len(study_status(self.store,"test")["cycles"]), 1)

    def test_nonimprovement_keeps_champion_instead_of_forcing_an_update(self):
        init_study(self.store, "random", LogicSettings())
        result = run_logic_cycle(self.store, "random", self.random_market, self.random_securities)
        self.assertEqual(result["verdict"], "KEEP_CHAMPION")
        self.assertFalse(result["research_champion_updated"])
        self.assertEqual(result["revision_after"], 0)

    def test_forward_cycle_keeps_frozen_history_and_requires_unused_final_window(self):
        self.run_cycle()
        market, securities = cyclic_logic_market(520)
        second = run_logic_cycle(self.store, "test", market, securities)
        self.assertEqual(second["revision_before"], 1)
        self.assertEqual(second["orders_submitted"], 0)
        market, securities = cyclic_logic_market(640)
        bad = Snapshot([replace(market.bars[0], high=market.bars[0].high+1), *market.bars[1:]])
        with self.assertRaisesRegex(AgentError, "frozen"):
            run_logic_cycle(self.store, "test", bad, securities)

    def test_new_data_cannot_reuse_a_consumed_holdout(self):
        self.run_cycle()
        market, securities = cyclic_logic_market(420)
        with self.assertRaisesRegex(AgentError, "consumed"):
            run_logic_cycle(self.store, "test", market, securities)
        self.assertEqual(study_status(self.store,"test")["revision"], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0], 1)

    def test_changing_policy_costs_or_budget_requires_review_and_cannot_reset_the_study(self):
        for settings in (replace(self.settings, costs=Costs(D(0),D(0),D(0))), replace(self.settings, candidate_budget=12)):
            with self.assertRaisesRegex(AgentError, "bound"):
                init_study(self.store, "test", settings)
        with self.assertRaises(AgentError):
            SelectionPolicy(capital_limit=D(100000001))  # research ceiling, 2026-10-23

    def test_model_context_does_not_contain_future_prices_or_security_changes(self):
        context = logic_development_context(self.market,self.securities,self.settings.champion,self.settings)
        boundary = self.market.sessions[-80]
        market = Snapshot([replace(b, open=b.open*10, high=b.high*10, low=b.low*10, close=b.close*10) if b.session >= boundary else b for b in self.market.bars])
        at = session_time(boundary, 0)
        securities = SecurityHistory([replace(r, managed=True) if r.effective_at >= at else r for r in self.securities.records])
        changed = logic_development_context(market,securities,self.settings.champion,self.settings)
        self.assertEqual(canonical(context), canonical(changed))

    def test_invalid_llm_bundle_and_wrong_context_fail_before_evaluation(self):
        logic_development_context(self.market,self.securities,self.settings.champion,self.settings)
        artifact = {"schema_version":1,"context_hash":"wrong","proposal":{"hypothesis":"test","falsification":"test","lookbacks":[2]}}
        with self.assertRaisesRegex(AgentError,"context"):
            self.run_cycle(proposal_artifact=artifact)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM experiments").fetchone()[0],0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0],0)
        self.assertEqual(study_status(self.store,"test")["cycles"][0]["status"],"FAILED")

    def test_recorded_llm_proposal_can_drive_real_evaluation_without_network(self):
        context = logic_development_context(self.market,self.securities,self.settings.champion,self.settings)
        response = {"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":json.dumps({"hypothesis":"engineered fixture only","falsification":"reject if independent gates fail","lookbacks":[2]})}]}]}
        artifact = response_artifact(context,"fixture",response)
        result = self.run_cycle(proposal_artifact=artifact)
        self.assertTrue(result["research_champion_updated"])
        saved=json.loads(self.store.db.execute("SELECT report_json FROM experiments").fetchone()[0])
        self.assertFalse(saved["configuration"]["proposal"]["structured"]["provenance"]["network_verified"])

    def test_proposer_cannot_change_allocation_or_unregistered_ranking(self):
        proposer=LogicProposer(self.settings)
        proposer.propose=lambda base:[replace(base,gross_weight=D(".9"))]
        with self.assertRaisesRegex(AgentError,"boundary"):
            evaluate_logic(self.market,self.securities,self.settings.champion,self.settings,self.store,proposer)
        proposer.propose=lambda base:[replace(base,ranking="risk_adjusted")]
        with self.assertRaisesRegex(AgentError,"boundary"):
            evaluate_logic(self.market,self.securities,self.settings.champion,self.settings,self.store,proposer)

    def test_crash_after_evaluation_recovers_stored_evidence_once_across_restart(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/"study.sqlite3"
            self.store.close()
            self.store=Store(path)
            init_study(self.store,"test",self.settings)
            with patch("trading_agent.logic._complete_cycle",side_effect=KeyboardInterrupt("after evaluation")):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_cycle()
            cycle=study_status(self.store,"test")["cycles"][0]
            self.assertEqual(cycle["status"],"RUNNING")
            self.store.close()
            self.store=Store(path)
            with patch("trading_agent.logic.evaluate_logic",side_effect=AssertionError("must not reevaluate")):
                result=recover_cycle(self.store,cycle["id"])
                self.assertTrue(result["research_champion_updated"])
                self.assertEqual(recover_cycle(self.store,cycle["id"]),result)
            self.assertEqual(study_status(self.store,"test")["revision"],1)

    def test_crash_during_holdout_does_not_reopen_it(self):
        with patch("trading_agent.research.paired_block_interval",side_effect=KeyboardInterrupt("during holdout")):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cycle()
        cycle=study_status(self.store,"test")["cycles"][0]
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM holdouts").fetchone()[0],1)
        with self.assertRaisesRegex(AgentError,"incomplete"):
            recover_cycle(self.store,cycle["id"])
        with self.assertRaisesRegex(AgentError,"interrupted"):
            self.run_cycle()
        self.assertEqual(study_status(self.store,"test")["revision"],0)
        abandoned=abandon_cycle(self.store,cycle["id"],"Stopped the fixture worker; continue only with new forward data")
        self.assertTrue(abandoned["holdout_claims_preserved"])
        market,securities=cyclic_logic_market(520)
        next_cycle=run_logic_cycle(self.store,"test",market,securities)
        self.assertEqual(next_cycle["revision_before"],0)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM holdouts WHERE run_id=?",(cycle["run_id"],)).fetchone())

    def test_recovery_rechecks_metrics_instead_of_trusting_a_verdict(self):
        with patch("trading_agent.logic._complete_cycle",side_effect=KeyboardInterrupt("crash")):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cycle()
        cycle=study_status(self.store,"test")["cycles"][0]
        report=json.loads(self.store.db.execute("SELECT report_json FROM experiments").fetchone()[0])
        report["holdout"]["cost_stress_improvement"]=-1
        self.store.db.execute("UPDATE experiments SET report_json=?",(canonical(report),))
        with self.assertRaisesRegex(AgentError,"promotion gates"):
            recover_cycle(self.store,cycle["id"])
        self.assertEqual(study_status(self.store,"test")["revision"],0)

    def test_completed_evidence_cannot_be_discarded_to_get_another_attempt(self):
        with patch("trading_agent.logic._complete_cycle",side_effect=KeyboardInterrupt("after evaluation")):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cycle()
        cycle=study_status(self.store,"test")["cycles"][0]
        with self.assertRaisesRegex(AgentError,"recover its evidence"):
            abandon_cycle(self.store,cycle["id"],"try again")
        self.assertTrue(recover_cycle(self.store,cycle["id"])["research_champion_updated"])

    def test_cached_cycle_detects_changed_evidence_without_reevaluation(self):
        self.run_cycle()
        report=json.loads(self.store.db.execute("SELECT report_json FROM experiments").fetchone()[0])
        report["total_trials"]=999
        self.store.db.execute("UPDATE experiments SET report_json=?",(canonical(report),))
        with self.assertRaisesRegex(AgentError,"evidence changed"):
            self.run_cycle()

    def test_fixed_activity_requirements_prevent_zero_activity_from_passing(self):
        strict=replace(self.settings,evaluation=replace(self.settings.evaluation,min_fold_trades=100000))
        init_study(self.store,"inactive",strict)
        result=run_logic_cycle(self.store,"inactive",self.market,self.securities)
        self.assertEqual(result["verdict"],"KEEP_CHAMPION")
        self.assertFalse(result["holdout"]["consumed"])

    def test_forward_metadata_cannot_rewrite_previously_known_security_history(self):
        self.run_cycle()
        market,securities=cyclic_logic_market(520)
        changed=SecurityHistory([replace(r,source="edited history") if r.effective_at < session_time(market.sessions[10],0) else r for r in securities.records])
        with self.assertRaisesRegex(AgentError,"frozen"):
            run_logic_cycle(self.store,"test",market,changed)

    def test_only_one_writer_can_own_a_study_cycle(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from trading_agent.logic import evaluate_logic as original
        with TemporaryDirectory() as directory:
            path=Path(directory)/"shared.sqlite3"
            self.store.close()
            self.store=Store(path)
            init_study(self.store,"test",self.settings)
            claimed,release=Event(),Event()
            def paused(*args,**kwargs):
                claimed.set()
                self.assertTrue(release.wait(5))
                return original(*args,**kwargs)
            def worker():
                store=Store(path)
                try:
                    return run_logic_cycle(store,"test",self.market,self.securities)
                finally:
                    store.close()
            with patch("trading_agent.logic.evaluate_logic",side_effect=paused),ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(worker)
                self.assertTrue(claimed.wait(5))
                try:
                    with self.assertRaisesRegex(AgentError,"running"):
                        self.run_cycle()
                finally:
                    release.set()
                self.assertTrue(future.result()["research_champion_updated"])
            self.assertEqual(len(study_status(self.store,"test")["cycles"]),1)

    def test_cli_init_run_screen_and_restart_need_no_keys(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ",{},clear=True):
            root=Path(directory)
            database, prices, securities, config=(root/name for name in ("logic.sqlite3","prices.csv","securities.jsonl","settings.json"))
            write_csv(self.market,prices)
            write_security_history(self.securities,securities)
            config.write_text(canonical(self.settings))
            base=["--db",str(database),"--study","test","--output",str(root/"reports")]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["logic","init",*base,"--config",str(config)]),0)
                run=["logic","run",*base,"--csv",str(prices),"--securities",str(securities)]
                self.assertEqual(main(run),0)
                self.assertEqual(main(run),0)
                self.assertEqual(main(["logic","screen",*base,"--csv",str(prices),"--securities",str(securities),"--as-of",session_time(self.market.sessions[-1],16).isoformat()]),0)
            report=json.loads((root/"reports/run.json").read_text())
            self.assertTrue(report["cached_cycle"])
            self.assertEqual(report["orders_submitted"],0)


if __name__ == "__main__":
    unittest.main()


class ResearchCapitalLimitTests(unittest.TestCase):
    def test_research_capital_ceiling_is_separate_from_real_order_limit(self):
        # User decision 2026-10-23: research may assume up to 100,000,000 KRW;
        # the real-order limit and the research default stay 1,000,000.
        from trading_agent.kis_real import REAL_CAPITAL_LIMIT
        from trading_agent.selection import SelectionPolicy

        self.assertEqual(SelectionPolicy().capital_limit, D("1000000"))
        self.assertEqual(SelectionPolicy(capital_limit=D("100000000")).capital_limit, D("100000000"))
        with self.assertRaisesRegex(AgentError, "at most 100000000"):
            SelectionPolicy(capital_limit=D("100000001"))
        self.assertEqual(REAL_CAPITAL_LIMIT, D("1000000"))
