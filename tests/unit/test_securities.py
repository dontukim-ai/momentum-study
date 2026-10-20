"""Security states from quotes plus hand-downloaded KRX lists (policy 1)."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from test_fsc_adjustments import write_day
from test_fsc_states import A, B, row
from trading_agent.selection import read_security_history

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError
from momentum.krx.securities import read_lists

DAYS = ["2024-06-03", "2024-06-04", "2024-06-05"]


def krx_files(folder):
    folder.mkdir()
    (folder / "data_4333.csv").write_bytes(
        "번호,종목코드,종목명,시장구분,최초지정일,지정사유,상장일,업종,액면가,상장주식수\n"
        "1,000001,가회사,코스닥,2024/06/04,자본잠식,2010/01/04,제조,500,10\n"
        "2,022220,티케이지애강,코스닥,,사유,2010/01/04,제조,500,10\n".encode("cp949")
    )
    (folder / "data_4400.csv").write_bytes(
        "번호,종목코드,종목명,시장구분,정지일,정지사유\n"
        "1,000001,가회사,코스닥,2024/06/05,사유\n".encode("cp949")
    )
    header = (
        "번호,종목코드,종목명,시장구분,증권구분,주식종류,상장일,폐지일,폐지사유,"
        "정리매매기간_시작일,정리매매기간_종료일,업종,액면가,상장주식수,"
        "이전상장후_종목코드,이전상장후_종목명\n"
    )
    stock = "코스닥,주권,보통주,2010/01/04,2024/06/05,사유,,,제조,500,10,,\n"
    (folder / "data_4525.csv").write_bytes(
        (
            header
            + "1,000002,나회사,"
            + stock
            + "2,000009,워런트,코스닥,신주인수권증서,,,2024/06/05,,,,,,,,\n"
        ).encode("cp949")
    )
    (folder / "data_4555.csv").write_bytes(
        (header + "1,000002,나회사," + stock).encode("cp949")
    )


def test_lists_are_read_by_header_and_cleaned(tmp_path):
    krx_files(tmp_path / "kind")
    lists = read_lists((tmp_path / "kind").iterdir())
    assert lists.managed == {"000001": date(2024, 6, 4)}
    assert lists.managed_held == ["022220"]
    assert lists.halted == {"000001": date(2024, 6, 5)}
    assert lists.delisted == {
        "000002": date(2024, 6, 5)
    }  # 주권 only, duplicate dropped
    conflict = tmp_path / "kind" / "data_4647.csv"
    conflict.write_bytes(
        (tmp_path / "kind" / "data_4555.csv")
        .read_bytes()
        .replace("2024/06/05".encode("cp949"), "2024/06/07".encode("cp949"), 1)
    )
    with pytest.raises(SpecUndefinedError, match="conflicting delisting"):
        read_lists((tmp_path / "kind").iterdir())


def test_tool_writes_readable_security_history(tmp_path, capsys):
    raw = tmp_path / "raw"
    write_day(raw, DAYS[0], [row(A, "000001"), row(B, "000002")])
    write_day(raw, DAYS[1], [row(A, "000001"), row(B, "000002")])
    write_day(raw, DAYS[2], [row(A, "000001", halted=True)])
    calendar = tmp_path / "calendar"
    calendar.mkdir()
    (calendar / "calendar.json").write_text(json.dumps(DAYS))
    krx_files(tmp_path / "kind")
    spec = importlib.util.spec_from_file_location(
        "securities_tool", Path(__file__).resolve().parents[2] / "tools/securities.py"
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)
    out = tmp_path / "out"
    args = ["--fsc-root", str(raw), "--calendar", str(calendar)]
    args += ["--kind-dir", str(tmp_path / "kind"), "--snapshot", "2026-10-20"]
    assert tool.main(args + ["--out", str(out)]) == 0
    assert capsys.readouterr().out.strip() == (
        "securities: 2 symbols, 5 records, delisting both 1 / date mismatch 0 / "
        "krx only 0 / quotes only 0, managed held 1"
    )
    history = read_security_history(out / "securities.jsonl")
    before = history.states(KRX.historical_bar_at(date(2024, 6, 3)))
    assert before["000001"].managed is False and before["000001"].status == "NORMAL"
    after = history.states(KRX.historical_bar_at(date(2024, 6, 5)))
    assert after["000001"].managed is True and after["000001"].status == "SUSPENDED"
    assert after["000002"].status == "DELISTED"
    assert "unverified" in after["000001"].source
    report = json.loads((out / "securities-report.json").read_text())
    assert report["halted_now_but_not_suspended_at_end"] == []
