"""Complete order-free logic workflow on explicitly synthetic test markets."""

import argparse
import json
from pathlib import Path

from fixtures.logic_market import cyclic_logic_market, logic_market
from trading_agent.cli import parser as command_parser
from trading_agent.cli import run, save_json
from trading_agent.contracts import canonical, implementation_id
from trading_agent.data import write_csv
from trading_agent.logic import LogicSettings, init_study, run_logic_cycle
from trading_agent.selection import RankedStrategy, write_security_history
from trading_agent.store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("artifacts/logic-v5"))
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    market,securities=logic_market()
    prices,security_file=args.output/"prices.csv",args.output/"securities.jsonl"
    write_csv(market,prices)
    write_security_history(securities,security_file)
    database=args.output/"research.sqlite3"
    common=["--db",str(database),"--study","main","--output",str(args.output)]
    run(command_parser().parse_args(["logic","init",*common,"--config","configs/logic.json"]))
    result=run(command_parser().parse_args(["logic","run",*common,"--csv",str(prices),"--securities",str(security_file)]))
    repeated=run(command_parser().parse_args(["logic","run",*common,"--csv",str(prices),"--securities",str(security_file)]))
    selection=json.loads((args.output/"selection.json").read_text())
    reasons={reason for row in selection["candidates"] for reason in row["reasons"]}
    assert {"MANAGED_SECURITY","LOW_LIQUIDITY","SUSPENDED","INSUFFICIENT_HISTORY","UNAFFORDABLE_WITHIN_POSITION_BUDGET"} <= reasons
    assert repeated["cached_cycle"] and result["verdict"]=="KEEP_CHAMPION"
    store=Store(database)
    try:
        # A separate engineered study proves the promotion/repeated-forward-cycle
        # branches. It cannot promote or replace the ordinary synthetic study.
        fixed=LogicSettings(champion=RankedStrategy(20),lookbacks=(2,20),rankings=("momentum",))
        init_study(store,"engineered-positive-path",fixed)
        engineered,states=cyclic_logic_market()
        accepted=run_logic_cycle(store,"engineered-positive-path",engineered,states)
        forward,forward_states=cyclic_logic_market(520)
        continued=run_logic_cycle(store,"engineered-positive-path",forward,forward_states)
        assert accepted["research_champion_updated"] and continued["revision_before"]==1
        counts={table:store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("orders","accounts","shadow_slots")}
        assert not any(counts.values())
        verification={"version":"0.5.0","mode":"SYNTHETIC_LOGIC_SOFTWARE_VALIDATION","implementation_id":implementation_id(),
                      "market_evidence":False,"external_calls":0,"capital_limit_krw":"1000000",
                      "ordinary_study_verdict":result["verdict"],"ordinary_study_trials":result["trials"],
                      "same_input_reused":True,"filter_reasons_verified":sorted(reasons),
                      "engineered_research_champion_updated":accepted["research_champion_updated"],
                      "new_forward_cycle_completed":True,"journal_objects_created":counts,
                      "live_strategy_changed":False,"orders_submitted":0}
        save_json(args.output/"engineered-promotion.json",accepted)
        save_json(args.output/"engineered-forward.json",continued)
        save_json(args.output/"verification.json",verification)
        print(canonical(verification))
    finally:
        store.close()


if __name__=="__main__":
    main()
