"""Monthly forward-record input update (forward-record-v9 §3), read-only collection.

Rebuilds the full 2020-01-02..<end> research input with the same steps as the
explore v6 forward build (FSC quotes -> calendar -> corporate actions ->
security states -> market ETF -> assembly -> quality) plus the v7 shares and
financials, then runs tools/fwd_record.py. Run it on the Mac (keys from the
environment, never printed):

    uv run --extra agent python tools/forward_update.py --end 2026-11-30 \
        --kind data/krx/kind/2026-11-30

--end is the day the KRX KIND files in --kind (관리종목·매매거래정지 현황 and
상장폐지 목록) were downloaded. Delisting lists of earlier downloads come from
--kind-history; their managed and halted lists are not used. Everything goes
to a new <out-root>/<end>/ directory: an existing one is refused, and no
earlier input or artifact is touched (the raw caches only gain new days).

Guards that stop the run:
- before 14:00 KST of the next weekday after --end (FSC publishes at 13:00; an
  empty day saved early would later read as a holiday);
- --end without FSC stock and ETF rows, or a calendar that stops before it;
- any old session whose prices or calendar differ from --previous.
Empty weekdays after the previous end are listed for a holiday check by hand.
Steps that end with code 1 but print their summary and write their output
(dividends and capital actions with unhandled filings, explore v6 §5) are
accepted, as in the v6 build.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from momentum.config import SpecUndefinedError
from momentum.krx.securities import read_lists

KST = ZoneInfo("Asia/Seoul")
FIRST = date(2020, 1, 2)
ETF, INDEX = "069500", "코스피 200"
BASIS = ("raw-unadjusted; actions partial (unresolved listed in FIN-02 reports); "
         "securities policy-1 managed/caution unverified")
TOOLS = Path(__file__).resolve().parent


def next_weekday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def published(end: date, now: datetime) -> bool:
    """FSC quotes for ``end`` are public by 13:00 KST of the next weekday."""
    return now.astimezone(KST) >= datetime.combine(next_weekday(end), time(14), KST)


def dart_start(raw: Path, kind: str, default: date) -> date:
    """The day after the last saved list period, so periods never overlap."""
    ends = [date.fromisoformat(p.name.split("_")[1]) for p in (raw / "dart_list" / kind).glob("*_*")
            if (p / "fetch.json").exists()]
    return max(ends) + timedelta(days=1) if ends else default


def day_total(fsc: Path, kind: str, day: date) -> int | None:
    record = fsc / kind / day.isoformat() / "fetch.json"
    return int(json.loads(record.read_text(encoding="utf-8"))["total_count"]) if record.exists() else None


def empty_weekdays(fsc: Path, after: date, end: date) -> dict[str, list[str]]:
    days, day = [], next_weekday(after)
    while day <= end:
        days.append(day)
        day = next_weekday(day)
    return {kind: [d.isoformat() for d in days if day_total(fsc, kind, d) == 0] for kind in ("stock", "etf")}


def kind_files(new: Path, history: Path) -> dict[str, list[Path]]:
    """The new download's three lists plus earlier delisting lists, by header."""
    def classify(path: Path) -> str:
        lists = read_lists([path])
        found = [name for name, hit in (("managed", lists.managed or lists.managed_held),
                                        ("halted", lists.halted), ("delisted", lists.delisted)) if hit]
        if len(found) != 1:
            raise SpecUndefinedError(f"{path}: cannot tell which KRX list this is")
        return found[0]

    def lists(folder: Path) -> list[Path]:  # skips hidden files such as .DS_Store
        return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))

    files: dict[str, list[Path]] = {"managed": [], "halted": [], "delisted": []}
    for path in lists(new):
        files[classify(path)].append(path)
    if any(len(v) != 1 for v in files.values()):
        raise SpecUndefinedError(f"{new} needs exactly one managed, halted and delisting list: "
                                 f"{ {k: len(v) for k, v in files.items()} }")
    for path in lists(history) if history.is_dir() else []:
        if classify(path) == "delisted":
            files["delisted"].append(path)
    return files


def session_digest(prices: Path, through: date) -> tuple[int, str]:
    """Order-free digest of the price rows on or before ``through``."""
    count, acc, cut = 0, 0, through.isoformat()
    with prices.open(encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            if line[:10] <= cut:
                count += 1
                acc ^= int.from_bytes(hashlib.sha256(line.rstrip("\n").encode()).digest()[:16], "big")
    return count, f"{acc:032x}"


def merge_actions(parts: list[Path], out: Path) -> Counter:
    merged: list[dict] = []
    seen: dict[tuple[str, str, str], Path] = {}
    for path in parts:
        for action in json.loads(path.read_text(encoding="utf-8")):
            key = (action["security_id"], action["action_type"], action["ex_date"])
            if key in seen:
                raise SpecUndefinedError(f"duplicate action {key} in {path} and {seen[key]}")
            seen[key] = path
            merged.append({k: v for k, v in action.items() if k != "evidence"})
    merged.sort(key=lambda a: (a["ex_date"], a["security_id"], a["action_type"]))
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return Counter(a["action_type"] for a in merged)


def action_keys(path: Path, through: date) -> set[tuple[str, str, str]]:
    cut = through.isoformat()
    return {(a["security_id"], a["action_type"], a["ex_date"])
            for a in json.loads(path.read_text(encoding="utf-8")) if a["ex_date"] <= cut}


def etf_index_names(fsc: Path, day: date) -> list[str]:
    names = []
    for page in sorted((fsc / "etf" / day.isoformat()).glob("page-*.json")):
        items = (json.loads(page.read_text(encoding="utf-8"))["response"]["body"]["items"] or {}).get("item", [])
        names += [i.get("bssIdxIdxNm") for i in items if i.get("srtnCd") == ETF]
    return names


@dataclass
class Runner:
    out: Path
    dry_run: bool = False

    def log(self, text: str) -> None:
        print(text, flush=True)
        if not self.dry_run:
            with (self.out / "steps.log").open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    def step(self, name: str, command: list[str], accept: tuple[str, Path] | None = None) -> None:
        """Run one step; a code-1 exit passes only with its summary line and output."""
        shown = " ".join(command)
        self.log(f"== {name} {shown if len(shown) < 400 else shown[:400] + ' ...'}")
        if self.dry_run:
            return
        logs = self.out / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / f"{name}.out").open("w") as out, (logs / f"{name}.err").open("w") as err:
            rc = subprocess.run(command, stdout=out, stderr=err, check=False).returncode
        lines = (logs / f"{name}.out").read_text(encoding="utf-8").strip().splitlines()
        last = lines[-1] if lines else ""
        self.log(f"   rc={rc} last: {last[:300]}")
        if rc == 0:
            return
        text = (logs / f"{name}.out").read_text() + (logs / f"{name}.err").read_text()
        if (accept and rc == 1 and last.startswith(accept[0]) and accept[1].exists()
                and "Traceback" not in text and "retries exhausted" not in text):
            self.log("   (rc=1 accepted: summary line and output present)")
            return
        raise SpecUndefinedError(f"step {name} failed (rc={rc}); see {logs / name}.err")


def tool(name: str, *args: object) -> list[str]:
    return [sys.executable, str(TOOLS / name), *map(str, args)]


def agent(*args: object) -> list[str]:
    return [sys.executable, "-m", "momentum.agent.cli", *map(str, args)]


def update(args: argparse.Namespace, now: datetime) -> Path:
    end, raw = args.end, args.raw
    fsc, out = raw / "fsc", args.out_root / args.end.isoformat()
    previous = [date.fromisoformat(d) for d in json.loads((args.previous / "sessions.json").read_text())]
    prev_end = previous[-1]
    if end.weekday() >= 5 or end <= prev_end:
        raise SpecUndefinedError(f"--end {end} must be a weekday after the previous end {prev_end}")
    if out.exists():
        raise SpecUndefinedError(f"{out} exists; outputs are never overwritten")
    if not published(end, now):
        raise SpecUndefinedError(f"FSC quotes for {end} are public only from 14:00 KST {next_weekday(end)}")
    kind = kind_files(args.kind, args.kind_history)
    run = Runner(out, args.dry_run)
    if not args.dry_run:
        out.mkdir(parents=True)
        (out / "kind").mkdir()
        for path in sorted({p for paths in kind.values() for p in paths}):
            shutil.copy2(path, out / "kind" / path.name)
    run.log(f"forward update {prev_end} -> {end} at {now.astimezone(KST):%Y-%m-%d %H:%M} KST; "
            f"kind {[p.name for paths in kind.values() for p in paths]}")

    for k in ("stock", "etf"):
        run.step(f"1-fsc-{k}", tool("fsc_collect.py", "--kind", k, "--start", next_weekday(prev_end),
                                    "--end", end, "--root", fsc))
    for t in ("I", "B", "A"):
        start = dart_start(raw, t, prev_end + timedelta(days=1))
        if start <= end:
            run.step(f"2-dart-{t}", tool("dart_collect.py", "--type", t, "--start", start, "--end", end,
                                         "--root", raw))
    if not args.dry_run:
        missing = [k for k in ("stock", "etf") if not day_total(fsc, k, end)]
        if missing:
            raise SpecUndefinedError(f"no FSC {missing} rows for {end}")
        empty = empty_weekdays(fsc, prev_end, end)
        (out / "empty-weekdays.json").write_text(json.dumps(empty, indent=1) + "\n")
        run.log(f"   empty weekdays (check against the KRX holiday list): {empty}")

    cal, research = out / "calendar", out / "research"
    run.step("3a-calendar", tool("fsc_calendar.py", "--start", FIRST, "--end", end, "--out", cal))
    if not args.dry_run:
        sessions = [date.fromisoformat(d) for d in json.loads((cal / "calendar.json").read_text())]
        if sessions[:len(previous)] != previous or sessions[-1] != end:
            raise SpecUndefinedError("calendar does not extend the previous sessions through --end")
        run.log(f"   calendar {len(previous)} -> {len(sessions)} sessions, prefix same")
    run.step("3c-adjust", tool("fsc_adjustments.py", "--root", fsc, "--start", FIRST, "--end", end,
                               "--out", out / "adjustments.jsonl"))
    run.step("3d-split", tool("split_actions.py", "--fsc-root", fsc, "--dart-root", raw, "--start", FIRST,
                              "--end", end, "--out", out / "actions-split"))
    run.step("4a-dividends", tool("dart_dividends.py", "--dart-root", raw, "--out", out / "dividends"),
             accept=("dart-dividends: ", out / "dividends" / "dividends.jsonl"))
    run.step("4b-companies", tool("dart_companies.py", "--dividends", out / "dividends" / "dividends.jsonl",
                                  "--dart-root", raw))
    run.step("4c-capital", tool("capital_actions.py", "--dart-root", raw, "--adjustments", out / "adjustments.jsonl",
                                "--calendar", cal, "--split-actions", out / "actions-split" / "actions.json",
                                "--out", out / "actions-capital"),
             accept=("capital-actions: ", out / "actions-capital" / "capital-report.json"))
    run.step("4d-divactions", tool("dividend_actions.py", "--dividends", out / "dividends" / "dividends.jsonl",
                                   "--calendar", cal, "--out", out / "actions-dividends", "--dart-root", raw))
    run.step("5-securities", tool("securities.py", "--fsc-root", fsc, "--calendar", cal, "--kind-dir",
                                  out / "kind", "--snapshot", end, "--out", out / "securities"))
    pages = sorted(glob.glob(str(fsc / "stock" / "20*" / "page-*.json")))
    run.step("6a-prices", tool("fsc_prices.py", "--kind", "stock", "--out", research / "prices-src",
                               "--pages", *pages))
    run.step("6b-screen", agent("quality-screen", "--csv", research / "prices-src" / "prices.csv",
                                "--output", research / "screen"))
    if not args.dry_run:
        parts = [out / d / "actions.json" for d in ("actions-split", "actions-capital", "actions-dividends")]
        counts = merge_actions(parts, research / "actions.json")
        old, new = action_keys(args.previous / "actions.json", prev_end), action_keys(research / "actions.json", prev_end)
        run.log(f"   actions {dict(counts)}; through {prev_end}: only previous {len(old - new)}, "
                f"only new {len(new - old)}")
        names = etf_index_names(fsc, end)
        if not names or any(n != INDEX for n in names):
            raise SpecUndefinedError(f"{ETF} index on {end} is {names}, not {INDEX}")
    run.step("7b-benchmark", tool("benchmark_etf.py", "--fsc-root", fsc, "--calendar", cal, "--symbol", ETF,
                                  "--expect-index", INDEX, "--out", out / "benchmark-etf"))
    if not args.dry_run:
        shutil.copy2(cal / "calendar.json", research / "sessions.json")
        with (research / "prices.csv").open("w", encoding="utf-8") as target:
            target.write((research / "screen" / "prices.accepted.csv").read_text(encoding="utf-8"))
            target.writelines((out / "benchmark-etf" / "prices.csv").read_text(encoding="utf-8").splitlines(True)[1:])
        with (research / "securities.jsonl").open("w", encoding="utf-8") as target:
            for part in (out / "securities" / "securities.jsonl", out / "benchmark-etf" / "securities.jsonl"):
                target.write(part.read_text(encoding="utf-8"))
        before, after = (session_digest(p / "prices.csv", prev_end) for p in (args.previous, research))
        if before != after:
            raise SpecUndefinedError(f"prices through {prev_end} changed: {before} -> {after}")
        run.log(f"   assembled; {before[0]} price rows through {prev_end} unchanged")
    run.step("8-quality", agent("quality-report", "--csv", research / "prices.csv", "--securities",
                                research / "securities.jsonl", "--actions", research / "actions.json",
                                "--sessions", research / "sessions.json", "--corporate-action-basis", BASIS,
                                "--output", research / "quality"))
    fin = out / "financials"
    run.step("9a-shares", tool("fsc_shares.py", "--root", fsc, "--sessions", research / "sessions.json",
                               "--out", fin / "shares.csv"))
    # Annual reports: FY N is filed by March N+1 and expires after 15 months.
    run.step("9b-fin-collect", tool("dart_financials.py", "collect", "--years", "2018", str(end.year - 1),
                                    "--root", raw))
    run.step("9c-fin-build", tool("dart_financials.py", "build", "--root", raw, "--out", fin))
    run.step("10-record", tool("fwd_record.py", "--input", research, "--financials", fin / "financials.jsonl",
                               "--shares", fin / "shares.csv"))
    run.log(f"-- done: {out}; record in {out / 'logs' / '10-record.out'}")
    return out


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--kind", type=Path, required=True)
    parser.add_argument("--kind-history", type=Path, default=Path("data/krx/kind"))
    parser.add_argument("--previous", type=Path, default=Path("data/krx/research-fwd-full"))
    parser.add_argument("--raw", type=Path, default=Path("data/krx/raw"))
    parser.add_argument("--out-root", type=Path, default=Path("data/krx/fwd9"))
    parser.add_argument("--dry-run", action="store_true", help="check the guards and print the steps only")
    args = parser.parse_args(argv)
    try:
        update(args, now or datetime.now(KST))
    except SpecUndefinedError as exc:
        print(f"forward-update: STOP: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
