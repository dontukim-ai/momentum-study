"""Offline contracts for the standalone, read-only supplier probe."""

import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import URLError

SPEC = importlib.util.spec_from_file_location(
    "data_probe", Path(__file__).resolve().parents[2] / "tools/data_probe.py"
)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def fsc(rows, total):
    return json.dumps(
        {
            "response": {
                "header": {"resultCode": "00"},
                "body": {"totalCount": total, "items": {"item": rows}},
            }
        }
    ).encode()


def test_fsc_pages_raw_summary_and_secrets(tmp_path, capsys):
    bodies = [
        fsc([{"basDt": "20240603", "price": "1.25", "n": "2", "x": ""}], 1001),
        fsc([{"basDt": "20240603", "price": "2.50", "n": "3"}], 1001),
    ]
    calls, waits = [], []

    def http(url, data):
        calls.append((url, data))
        return 200, bodies[len(calls) - 1]

    result = probe.main(
        ["--date", "2024-06-03", "--out", str(tmp_path), "--source", "fsc_stock"],
        http=http,
        sleep=waits.append,
        environ={"DATA_GO_KR_SERVICE_KEY": "fake-secret+/%"},
    )
    assert result == 0
    assert len(calls) == 2 and "pageNo=2" in calls[1][0]
    assert waits == [1.0]
    for n, body in enumerate(bodies, 1):
        assert (
            tmp_path / "fsc_stock/2024-06-03" / f"page-{n}.json"
        ).read_bytes() == body
    summary = json.loads((tmp_path / "summary.json").read_text())["fsc_stock"]
    assert summary["total_rows"] == 2
    assert summary["fields"] == ["basDt", "n", "price", "x"]
    assert summary["empty_counts"] == {"basDt": 0, "n": 0, "price": 0, "x": 2}
    assert summary["types"] == {
        "basDt": "date YYYYMMDD",
        "n": "integer",
        "price": "decimal",
        "x": "string",
    }
    assert summary["samples"]["n"] == ["2", "3"]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["files"]) == 2
    import hashlib

    assert manifest["files"][0]["sha256"] == hashlib.sha256(bodies[0]).hexdigest()
    for path in tmp_path.rglob("*"):
        assert "fake-secret" not in str(path)
        if path.is_file():
            assert b"fake-secret" not in path.read_bytes()
    assert (
        capsys.readouterr().out.splitlines()[-1]
        == "probe: 1 sources ok, 0 skipped, 0 errors"
    )


def test_missing_keys_and_kind_continue(tmp_path, capsys):
    def http(url, data):
        assert "getETFPriceInfo" in url
        return 200, fsc([], 0)

    probe.main(["--date", "2024-06-03", "--out", str(tmp_path)], http=http, environ={})
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["fsc_stock"]["message"] == "missing key"
    assert summary["dart"]["status"] == "skipped"
    assert summary["kind"]["status"] == "skipped"
    assert (
        capsys.readouterr().out.splitlines()[-1]
        == "probe: 0 sources ok, 4 skipped, 0 errors"
    )


def test_network_retry_exhaustion_continues_and_hides_exception(tmp_path, capsys):
    waits, calls = [], []

    def http(url, data):
        calls.append(url)
        if "getStockPriceInfo" in url:
            raise URLError("fake-secret")
        return 200, fsc([], 0)

    assert (
        probe.main(
            ["--date", "2024-06-03", "--out", str(tmp_path)],
            http=http,
            sleep=waits.append,
            environ={"DATA_GO_KR_SERVICE_KEY": "fake-secret"},
        )
        == 1
    )
    assert len(calls) == 4
    assert waits == [2.0, 4.0, 1.0]
    assert "fake-secret" not in capsys.readouterr().out
    assert "fake-secret" not in (tmp_path / "summary.json").read_text()


def test_http_errors_not_retried_and_echoed_key_not_saved(tmp_path):
    calls = []

    def http(url, data):
        calls.append(url)
        return 200, b"fake-secret"

    probe.main(
        ["--date", "2024-06-03", "--out", str(tmp_path), "--source", "dart"],
        http=http,
        environ={"OPENDART_API_KEY": "fake-secret"},
    )
    assert len(calls) == 1
    assert not list(tmp_path.rglob("page-*"))
    assert "fake-secret" not in (tmp_path / "summary.json").read_text()


def test_dart_pages_and_corp_size_only(tmp_path):
    calls = []

    def http(url, data):
        calls.append(url)
        if "corpCode" in url:
            return 200, b"zip-content"
        return 200, json.dumps(
            {"status": "000", "total_page": 2, "list": [{"rcept_dt": "20240603"}]}
        ).encode()

    probe.main(
        ["--date", "2024-06-03", "--out", str(tmp_path), "--source", "dart"],
        http=http,
        sleep=lambda _: None,
        environ={"OPENDART_API_KEY": "secret"},
    )
    summary = json.loads((tmp_path / "summary.json").read_text())["dart"]
    assert summary["total_rows"] == 2
    assert summary["corp_code_bytes"] == 11
    assert len(list(tmp_path.rglob("page-*"))) == 2


def test_kind_post_raw_and_page_limit(tmp_path):
    def http(url, data):
        assert data == b"method=list"
        return 200, b"<html>raw</html>"

    probe.main(
        [
            "--date",
            "2024-06-03",
            "--out",
            str(tmp_path),
            "--source",
            "kind",
            "--kind-url",
            "https://kind.krx.co.kr/list",
            "--kind-form",
            '{"method":"list"}',
        ],
        http=http,
        environ={},
    )
    assert (
        tmp_path / "kind/2024-06-03/page-1.html"
    ).read_bytes() == b"<html>raw</html>"
    probe.main(
        [
            "--date",
            "2024-06-03",
            "--out",
            str(tmp_path / "limit"),
            "--source",
            "fsc_stock",
            "--max-pages",
            "1",
        ],
        http=lambda u, d: (200, fsc([{"n": "1"}], 1001)),
        environ={"DATA_GO_KR_SERVICE_KEY": "secret"},
    )
    assert (
        json.loads((tmp_path / "limit/summary.json").read_text())["fsc_stock"]["status"]
        == "error"
    )


def test_json_escaped_credential_never_enters_summary(tmp_path):
    body = b'{"status":"000","total_page":1,"list":[{"name":"fake\\u002dsecret"}]}'
    probe.main(
        ["--date", "2024-06-03", "--out", str(tmp_path), "--source", "dart"],
        http=lambda u, d: (200, body),
        sleep=lambda _: None,
        environ={"OPENDART_API_KEY": "fake-secret"},
    )
    assert "fake-secret" not in (tmp_path / "summary.json").read_text()
    assert (
        json.loads((tmp_path / "summary.json").read_text())["dart"]["status"] == "error"
    )


def test_http_status_error_is_not_retried(tmp_path):
    calls, waits = [], []

    def http(url, data):
        calls.append(url)
        return 403, b"forbidden"

    probe.main(
        ["--date", "2024-06-03", "--out", str(tmp_path), "--source", "fsc_stock"],
        http=http,
        sleep=waits.append,
        environ={"DATA_GO_KR_SERVICE_KEY": "secret"},
    )
    assert len(calls) == 1 and waits == []
    assert (
        json.loads((tmp_path / "summary.json").read_text())["fsc_stock"]["status"]
        == "error"
    )


def test_transient_network_failure_recovers(tmp_path):
    calls, waits = [], []

    def http(url, data):
        calls.append(url)
        if len(calls) == 1:
            raise URLError("transient")
        return 200, fsc([], 0)

    assert (
        probe.main(
            ["--date", "2024-06-03", "--out", str(tmp_path), "--source", "fsc_etf"],
            http=http,
            sleep=waits.append,
            environ={"DATA_GO_KR_SERVICE_KEY": "secret"},
        )
        == 0
    )
    assert len(calls) == 2 and waits == [2.0]


def test_kind_url_without_form_is_skipped(tmp_path):
    def http(url, data):
        raise AssertionError("missing form must not send a request")

    probe.main(
        [
            "--date",
            "2024-06-03",
            "--out",
            str(tmp_path),
            "--source",
            "kind",
            "--kind-url",
            "https://kind.krx.co.kr/list",
        ],
        http=http,
        environ={},
    )
    assert (
        json.loads((tmp_path / "summary.json").read_text())["kind"]["status"]
        == "skipped"
    )
