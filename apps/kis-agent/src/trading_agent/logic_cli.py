"""CLI application service for the entirely order-free logic workflow."""

import json
from datetime import datetime, timedelta

from .contracts import UTC, AgentError, implementation_id, session_time
from .data import read_csv
from .logic import (
    abandon_cycle,
    init_study,
    load_logic_settings,
    logic_development_context,
    ranked_spec,
    recover_cycle,
    run_logic_cycle,
    settings_from_dict,
    study_status,
)
from .selection import read_security_history, select_candidates
from .store import Store


def run_logic_command(args):
    from .cli import save_json
    if args.action != "init" and not args.db.exists():
        raise AgentError("Initialize the persistent logic study database first")
    if args.config and args.action != "init":
        raise AgentError("Policy is fixed at study initialization; --config is only valid with init")
    if args.action != "run" and (args.llm or args.proposal_file):
        raise AgentError("Structured proposals are only valid with logic run")
    store = Store(args.db)
    try:
        if args.action == "init":
            result = init_study(store, args.study, load_logic_settings(args.config))
        elif args.action == "status":
            result = study_status(store, args.study)
        elif args.action in {"recover", "abandon"}:
            if not args.cycle_id:
                raise AgentError("Recover requires --cycle-id")
            result = (recover_cycle(store, args.cycle_id) if args.action == "recover" else
                      abandon_cycle(store, args.cycle_id, args.reason))
        else:
            if not args.csv or not args.securities:
                raise AgentError("Screen and run require --csv and --securities")
            snapshot = read_csv(args.csv, require_complete=False)
            securities = read_security_history(args.securities)
            status = study_status(store, args.study)
            if status["implementation_id"] != implementation_id():
                raise AgentError("Study build changed; preserve its recorded version before screening")
            settings = settings_from_dict(status["settings"])
            champion = ranked_spec(status["champion"])
            if args.action == "screen":
                day = args.session or snapshot.sessions[-1]
                at = args.as_of or datetime.now(UTC)
                if day not in snapshot.sessions or session_time(day, 0) > at:
                    raise AgentError("Invalid screening session or future session")
                view = snapshot.view(at, day, allow_missing=True)
                result = select_candidates(champion, view, securities, settings.selection, settings.costs, at + timedelta(hours=1))
            else:
                if args.session or args.as_of:
                    raise AgentError("Research split controls its own clock; session/as-of are only valid with screen")
                artifact = None
                if args.proposal_file:
                    if args.proposal_file.stat().st_size > 65536:
                        raise AgentError("Logic proposal exceeds input budget")
                    try:
                        artifact = json.loads(args.proposal_file.read_text(encoding="utf-8"))
                    except (ValueError, UnicodeError):
                        raise AgentError("Invalid logic proposal JSON") from None
                context = logic_development_context(snapshot, securities, champion, settings)
                save_json(args.output / "development-context.json", context)
                result = run_logic_cycle(store, args.study, snapshot, securities, proposal_artifact=artifact, llm=args.llm, model=args.model)
                experiment = store.db.execute("SELECT report_json FROM experiments WHERE run_id=?", (result["run_id"],)).fetchone()
                save_json(args.output / "research.json", json.loads(experiment[0]))
                after = study_status(store, args.study)
                spec = ranked_spec(after["champion"])
                day = snapshot.sessions[-1]
                view = snapshot.view(session_time(day, 16), day, allow_missing=True)
                selection = select_candidates(spec, view, securities, settings.selection, settings.costs, session_time(day, 17))
                save_json(args.output / "selection.json", selection)
                save_json(args.output / "study.json", after)
        save_json(args.output / (args.action + ".json"), result)
        return result
    finally:
        store.close()
