"""tools/kis_minute_history.py and tools/etf_universe.py on fakes (no network)."""

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
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


history, universe = load("kis_minute_history"), load("etf_universe")
MONDAY, HOLIDAY = date(2026, 1, 5), date(2026, 1, 6)


class FakeKis:
    """120 bars per read, newest first; earlier days follow once a day runs out (as with incu=Y)."""

    def __init__(self, *, fail=False, leak=""):
        self.fail, self.leak, self.reads = fail, leak, []

    def minute_bars_on(self, symbol, day, hour):
        self.reads.append((symbol, day, hour))
        if self.fail:
            raise RuntimeError("HTTP 500")
        rows, at = [], datetime.combine(day, datetime.strptime(hour, "%H%M%S").time())  # noqa: DTZ007
        while len(rows) < 120:
            if day == HOLIDAY or at.time() < datetime.strptime("090000", "%H%M%S").time():  # noqa: DTZ007
                day, at = day - timedelta(days=1), datetime.combine(day - timedelta(days=1), datetime.min.time()).replace(
                    hour=15, minute=30)
                if day < MONDAY:
                    break
                continue
            rows.append({"stck_bsop_date": day.strftime("%Y%m%d"), "stck_cntg_hour": at.strftime("%H%M%S"),
                         "stck_prpr": "35000", "cntg_vol": "10", "note": self.leak})
            at -= timedelta(minutes=1)
        return {"rt_cd": "0", "output2": rows}


def collector(tmp_path, **fake):
    clock = datetime(2026, 10, 27, 1, tzinfo=history.KST)
    return history.Collector(FakeKis(**fake), tmp_path, environ={"KIS_APP_SECRET": "very-secret"}, now=lambda: clock)


def test_a_day_is_read_back_to_the_open_and_saved_once(tmp_path):
    c = collector(tmp_path)
    assert c.day("069500", MONDAY) == "fetched"
    saved = json.loads((tmp_path / "069500" / "2026-01-05.json").read_text())
    times = [b["stck_cntg_hour"] for b in saved["bars"]]
    assert (len(times), times[0], times[-1], saved["reads"]) == (391, "090000", "153000", 4)
    assert [h for _, _, h in c.client.reads] == ["153000", "133000", "113000", "093000"]
    assert c.day("069500", MONDAY) == "skipped" and len(c.client.reads) == 4


def test_other_days_rows_are_dropped_and_a_holiday_is_saved_empty(tmp_path):
    c = collector(tmp_path)
    assert c.day("069500", HOLIDAY) == "empty"
    assert json.loads((tmp_path / "069500" / "2026-01-06.json").read_text())["bars"] == []


def test_a_credential_stops_without_writing(tmp_path):
    with pytest.raises(history.StopCollection, match="credential"):
        collector(tmp_path, leak="echo very-secret").day("069500", MONDAY)
    assert not (tmp_path / "069500" / "2026-01-05.json").exists()


def test_failures_leave_the_day_unfinished_and_thirty_stop(tmp_path):
    c = collector(tmp_path, fail=True)
    for k in range(29):
        assert c.day("069500", MONDAY + timedelta(days=k)) == "error"
    with pytest.raises(history.StopCollection, match="30 failures"):
        c.day("069500", MONDAY + timedelta(days=29))
    assert not list((tmp_path / "069500").glob("*.json"))
    assert len((tmp_path / "errors.jsonl").read_text().splitlines()) == 30


def test_development_reader_never_returns_holdout_days(tmp_path):
    for day in (history.DEVELOPMENT_END, history.HOLDOUT_START):
        (tmp_path / "069500").mkdir(exist_ok=True)
        (tmp_path / "069500" / f"{day}.json").write_text(json.dumps({"bars": [{"stck_cntg_hour": "090000"}]}))
    assert list(history.development_bars(tmp_path, "069500")) == ["2026-06-30"]
    assert history.HOLDOUT_START == history.DEVELOPMENT_END + timedelta(days=1)


def test_weekdays_and_previous_minute():
    assert history.weekdays(date(2026, 1, 2), date(2026, 1, 6)) == [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    assert (history.previous_minute("100000"), history.previous_minute("090100")) == ("095900", "090000")


def page(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"response": {"body": {"items": {"item": items}}}}, ensure_ascii=False))


def test_universe_keeps_liquid_domestic_equity_etfs_and_1x_inverse(tmp_path):
    rows = [("069500", "KODEX 200", "코스피 200", 900), ("114800", "KODEX 인버스", "코스피 200 선물지수", 800),
            ("252670", "KODEX 200선물인버스2X", "코스피 200 선물지수", 2000), ("379800", "KODEX 미국S&P500", "S&P 500", 950),
            ("153130", "KODEX 단기채권", "KIS 단기채", 990), ("091160", "KODEX 반도체", "KRX 반도체", 300),
            ("229200", "KODEX 코스닥150", "코스닥 150", 500)]
    pages = {}
    for k, day in enumerate(("2026-10-21", "2026-10-22", "2026-10-23")):
        items = [{"srtnCd": s, "itmsNm": n, "bssIdxIdxNm": i, "trPrc": str(v * 1e8)} for s, n, i, v in rows
                 if not (s == "229200" and k == 0)]  # listed late: not on every session
        page(tmp_path / day / "page-1.json", items)
        pages[day] = [tmp_path / day / "page-1.json"]
    result = universe.select(pages, top=2)
    assert [r["symbol"] for r in result["symbols"]] == ["069500", "114800"]
    assert result["eligible"] == 3
    assert result["dropped"] == {"2X": 1, "not quoted every session": 1, "미국": 1, "채권": 1}


def test_universe_can_require_quotes_over_the_whole_collection_window(tmp_path):
    older = {"069500": 900, "102110": 500}
    young = {"0167A0": 800}  # listed inside the collection window
    pages, presence = {}, {}
    for day in ("2026-01-05", "2026-01-06"):
        page(tmp_path / day / "page-1.json", [{"srtnCd": s, "itmsNm": s, "bssIdxIdxNm": "코스피 200", "trPrc": "1"}
                                             for s in older])
        presence[day] = [tmp_path / day / "page-1.json"]
    for day in ("2026-10-22", "2026-10-23"):
        page(tmp_path / day / "page-1.json", [{"srtnCd": s, "itmsNm": s, "bssIdxIdxNm": "코스피 200", "trPrc": str(v)}
                                             for s, v in {**older, **young}.items()])
        pages[day] = presence[day] = [tmp_path / day / "page-1.json"]
    assert [r["symbol"] for r in universe.select(pages, top=2)["symbols"]] == ["069500", "0167A0"]
    result = universe.select(pages, top=2, presence=presence)
    assert [r["symbol"] for r in result["symbols"]] == ["069500", "102110"]
    assert result["dropped"]["not quoted since the collection start"] == 1


def test_today_is_collectable_only_after_the_close_settles():
    today = date(2026, 10, 29)
    before = datetime(2026, 10, 29, 15, 39, tzinfo=history.KST)
    after = datetime(2026, 10, 29, 15, 40, tzinfo=history.KST)
    assert history.last_collectable(before) == date(2026, 10, 28)
    assert history.last_collectable(after) == today
