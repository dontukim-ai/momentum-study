"""tools/kis_intraday_probe.py and kis_intraday_report.py on a fake KIS day (no network)."""

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe_tool, report_tool = load("kis_intraday_probe"), load("kis_intraday_report")
KST = probe_tool.KST
DAY = datetime(2026, 10, 28, tzinfo=KST)
SYMBOLS = ("069500", "005930")


class Clock:
    def __init__(self, start):
        self.at = start

    def now(self):
        return self.at

    def sleep(self, seconds):
        self.at += timedelta(seconds=seconds)

    def monotonic(self):
        return self.at.timestamp()


class FakeKis:
    """Ask 35005 / bid 35000 with 1,000 shares a side; a bar for every minute 09:00~15:30."""

    def __init__(self, clock, *, fail=False, leak=""):
        self.clock, self.fail, self.leak, self.calls = clock, fail, leak, []

    def asking(self, symbol):
        self.calls.append(("asking", symbol, self.clock.now()))
        if self.fail:
            raise RuntimeError("HTTP 500")
        before_open = self.clock.now().hour < 9
        return {"rt_cd": "0", "_response_headers": {"tr_cont": ""}, "msg1": self.leak, "output1": {
            "askp1": "0" if before_open else "35005", "bidp1": "35000", "askp_rsqn1": "1000",
            "bidp_rsqn1": "1000", "aspr_acpt_hour": self.clock.now().strftime("%H%M%S")}}

    def minute_bars(self, symbol, hour):
        self.calls.append(("minute", symbol, hour))
        end = datetime.combine(DAY.date(), report_tool.hhmmss(hour))
        rows = []
        for k in range(30):
            at = end - timedelta(minutes=k)
            if at.time() < report_tool.hhmmss("090000"):
                break
            rows.append({"stck_bsop_date": "20261028", "stck_cntg_hour": at.strftime("%H%M%S"), "stck_prpr": "35000"})
        return {"rt_cd": "0", "output1": {}, "output2": rows}


def run(tmp_path, *, start="08:20", interval=60, **fake):
    clock = Clock(DAY.replace(hour=int(start[:2]), minute=int(start[3:])))
    client = FakeKis(clock, **fake)
    out = tmp_path / DAY.date().isoformat()
    out.mkdir()
    probe = probe_tool.Probe(client, out, SYMBOLS, now=clock.now, monotonic=clock.monotonic,
                             environ={"KIS_APP_KEY": "secret-app-key"})
    return probe_tool.run_day(probe, interval, clock.sleep), client, out


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_one_day_reads_books_hourly_bars_and_the_whole_day(tmp_path):
    summary, client, out = run(tmp_path)
    assert summary == {"book_rounds": 420}  # 08:30..15:29 once a minute
    for symbol in SYMBOLS:
        books = lines(out / f"asking-{symbol}.jsonl")
        assert len(books) == 420 and all("_response_headers" not in b["response"] for b in books)
        bars = lines(out / f"minute-{symbol}.jsonl")
        assert [b["hour"] for b in bars if b["pull"] == "hourly"] == [
            "100000", "110000", "120000", "130000", "140000", "150000"]
        close = [b["hour"] for b in bars if b["pull"] == "close"]
        assert close[0] == "153000" and len(close) == 14  # 391 minutes, 30 per read
        assert min(r["stck_cntg_hour"] for b in bars for r in b["response"]["output2"]) == "090000"
    assert not (out / "errors.jsonl").exists()
    assert client.calls[0][2] == DAY.replace(hour=8, minute=30)


def test_a_response_with_a_credential_stops_without_writing(tmp_path):
    with pytest.raises(probe_tool.StopProbe, match="credential"):
        run(tmp_path, start="09:00", leak="echo secret-app-key")
    assert not list((tmp_path / DAY.date().isoformat()).glob("asking-*"))


def test_failures_are_logged_and_thirty_in_a_row_stop_the_day(tmp_path):
    with pytest.raises(probe_tool.StopProbe, match="30 failures"):
        run(tmp_path, start="09:00", fail=True)
    errors = lines(tmp_path / DAY.date().isoformat() / "errors.jsonl")
    assert len(errors) == 30 and errors[0]["error"] == "RuntimeError: HTTP 500"


def test_report_prices_the_round_trip_and_makes_the_stage_call(tmp_path, capsys):
    _, _, out = run(tmp_path)
    result = report_tool.report([out], commission_bps=1.5)
    etf, stock = result["symbols"]["069500"], result["symbols"]["005930"]
    spread = 5 / 35002.5 * 1e4
    assert etf["mid_session_spread_bp_median"] == pytest.approx(spread)
    assert etf["round_trip_bp"] == pytest.approx(spread + 3)
    assert stock["round_trip_bp"] == pytest.approx(spread + 3 + 20)
    # 08:30..08:59 books have an empty ask side; 15:20..15:29 count as auction.
    assert etf["empty_snapshots"] == 30 and etf["auction_snapshots"] == 10
    assert etf["fits_best_level"] == {"1000000": 1.0, "10000000": 1.0, "100000000": 0.0}
    assert etf["minute_bars_per_day"] == [391]
    assert etf["minute_delay_seconds_median"] == 0
    # Only two of the four ETFs were collected, so §6 cannot be called yet.
    assert result["decision"].startswith("INCOMPLETE")
    assert report_tool.main([str(out)]) == 0
    assert "decision (§6): INCOMPLETE" in capsys.readouterr().out


def test_stage_call_thresholds():
    def call(*costs):
        return report_tool.stage_call(dict(zip(report_tool.ETFS, costs, strict=True))).split(":")[0]
    assert call(5.0, 20, 20, 20) == "NEXT"
    assert call(5.01, 15, 30, 30) == "USER"
    assert call(15.01, 16, 17, 18) == "STOP"
    assert call(1.0, None, 2.0, 3.0) == "INCOMPLETE"
