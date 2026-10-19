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


def run_probe(tmp_path, http, *options, environ=None):
    return probe.main(
        ["--date", "2024-06-03", "--out", str(tmp_path), *options],
        http=http,
        sleep=lambda _: None,
        environ=environ if environ is not None else {"OPENDART_API_KEY": "fake-secret"},
    )


def test_http_get_preserves_http_error_body(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    def denied(*args, **kwargs):
        raise HTTPError("https://example.test", 403, "denied", {}, BytesIO(b"denied"))

    monkeypatch.setattr(probe, "urlopen", denied)
    assert probe.http_get("https://example.test", None) == (403, b"denied")


def test_http_error_raw_manifest_and_message(tmp_path):
    body = b"<error>  permission\n denied </error>"
    assert (
        run_probe(
            tmp_path,
            lambda u, d, body=body: (403, body),
            "--source",
            "fsc_stock",
            environ={"DATA_GO_KR_SERVICE_KEY": "fake-secret"},
        )
        == 1
    )
    assert (tmp_path / "fsc_stock/2024-06-03/error-1.xml").read_bytes() == body
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["files"][0]["http_status"] == 403
    assert manifest["requests"][0]["http_status"] == 403
    result = json.loads((tmp_path / "summary.json").read_text())["fsc_stock"]
    assert result["message"] == "HTTP status 403: <error> permission denied </error>"


def test_error_credentials_never_saved(tmp_path):
    from urllib.parse import quote_plus

    for index, body in enumerate(
        (b"fake-secret", b'"fake\\u002dsecret"', quote_plus("fake-secret+/%").encode())
    ):
        folder = tmp_path / str(index)
        key = "fake-secret+/%" if index == 2 else "fake-secret"
        run_probe(
            folder,
            lambda u, d, body=body: (403, body),
            "--source",
            "dart",
            environ={"OPENDART_API_KEY": key},
        )
        assert not list(folder.rglob("error-*"))
        assert key not in (folder / "summary.json").read_text()
        assert json.loads((folder / "manifest.json").read_text())["files"] == []


def test_dart_range_params_and_path(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    calls = []

    def http(url, data):
        calls.append(url)
        return 200, b'{"status":"013"}'

    run_probe(
        tmp_path,
        http,
        "--source",
        "dart",
        "--dart-bgn",
        "2024-04-01",
        "--dart-end",
        "2024-07-01",
    )
    params = parse_qs(urlsplit(calls[0]).query)
    assert params["bgn_de"] == ["20240401"]
    assert params["end_de"] == ["20240701"]
    assert (tmp_path / "dart/2024-04-01_2024-07-01/page-1.json").exists()


def test_dart_range_and_detail_limit_reject_invalid_arguments(tmp_path):
    import pytest

    for options in (
        ["--dart-bgn", "2024-04-01"],
        ["--dart-end", "2024-04-01"],
        ["--dart-bgn", "2024-04-02", "--dart-end", "2024-04-01"],
        ["--dart-bgn", "2024-04-01", "--dart-end", "2024-07-03"],
        ["--max-detail", "0"],
    ):
        with pytest.raises(SystemExit) as exc:
            run_probe(
                tmp_path, lambda u, d: (_ for _ in ()).throw(AssertionError()), *options
            )
        assert exc.value.code == 2


def detail_transport(filings, responses, calls):
    def http(url, data):
        from urllib.parse import urlsplit

        calls.append(url)
        name = Path(urlsplit(url).path).stem
        if name == "list":
            return 200, json.dumps({"status": "000", "list": filings}).encode()
        if name == "corpCode":
            return 200, b"zip"
        if "getStockPriceInfo" in url or "getETFPriceInfo" in url:
            return 200, fsc([], 0)
        response = responses.get(name, {"status": "000", "list": [{"ratio": "1.5"}]})
        return 200, json.dumps(response).encode()

    return http


def filings_for_details():
    return [
        {
            "stock_code": stock,
            "corp_code": corp,
            "rcept_dt": "20240603",
            "report_nm": name,
        }
        for stock, corp, name in (
            ("111111", "00000001", "무상증자결정"),
            ("222222", "00000002", "[기재정정]유상증자결정"),
            ("333333", "00000003", "유무상증자결정"),
            ("111111", "00000001", "[기재정정]무상증자결정"),
            ("444444", "00000004", "감자결정"),
            ("555555", "00000005", "사업보고서"),
            ("", "00000006", "무상증자결정"),
        )
    ]


def test_detail_matching_dedup_limit_and_summary(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    for limit, expected in (
        (10, ["fricDecsn", "piicDecsn", "pifricDecsn", "crDecsn"]),
        (2, ["fricDecsn", "piicDecsn"]),
    ):
        folder, calls = tmp_path / str(limit), []
        run_probe(
            folder,
            detail_transport(filings_for_details(), {}, calls),
            "--source",
            "dart_detail",
            "--max-detail",
            str(limit),
        )
        detail_calls = [u for u in calls if "Decsn.json" in u]
        assert [Path(urlsplit(u).path).stem for u in detail_calls] == expected
        for index, url in enumerate(detail_calls, 1):
            params = parse_qs(urlsplit(url).query)
            assert params["corp_code"] == [f"{index:08d}"]
            assert params["bgn_de"] == params["end_de"] == ["20240603"]
        summary = json.loads((folder / "summary.json").read_text())["dart_detail"]
        assert summary["status"] == "ok"
        for name in expected:
            endpoint = summary["endpoints"][name]
            assert endpoint["calls"] == endpoint["total_rows"] == 1
            assert endpoint["samples"] == {"ratio": ["1.5"]}
            assert endpoint["types"] == {"ratio": "decimal"}
        files = json.loads((folder / "manifest.json").read_text())["files"]
        assert sum(item["path"].startswith("dart_detail/") for item in files) == len(
            expected
        )
        assert (
            folder
            / "dart_detail/2024-06-03_2024-06-03/fricDecsn-00000001-20240603.json"
        ).exists()


def test_detail_errors_isolated_and_all_last_line(tmp_path, capsys):
    calls = []
    responses = {"fricDecsn": {"status": "020"}, "piicDecsn": {"status": "013"}}
    assert (
        run_probe(
            tmp_path,
            detail_transport(filings_for_details(), responses, calls),
            "--with-dart-detail",
            environ={
                "OPENDART_API_KEY": "fake-secret",
                "DATA_GO_KR_SERVICE_KEY": "other-secret",
            },
        )
        == 1
    )
    summary = json.loads((tmp_path / "summary.json").read_text())
    detail = summary["dart_detail"]
    assert detail["status"] == detail["endpoints"]["fricDecsn"]["status"] == "error"
    assert detail["endpoints"]["piicDecsn"]["status"] == "ok"
    assert detail["endpoints"]["piicDecsn"]["total_rows"] == 0
    assert summary["fsc_stock"]["status"] == summary["dart"]["status"] == "ok"
    assert (
        capsys.readouterr().out.splitlines()[-1]
        == "probe: 3 sources ok, 1 skipped, 1 errors"
    )


def test_detail_missing_key_failed_dart_and_no_matches(tmp_path):
    for case in ("missing", "failed", "empty"):
        folder = tmp_path / case
        run_probe(
            folder,
            (lambda u, d: (403, b"denied"))
            if case == "failed"
            else detail_transport([], {}, []),
            "--source",
            "dart_detail",
            environ={} if case == "missing" else {"OPENDART_API_KEY": "fake-secret"},
        )
        summary = json.loads((folder / "summary.json").read_text())
        assert summary["dart_detail"]["status"] == (
            "ok" if case == "empty" else "skipped"
        )
        if case == "missing":
            assert summary["dart"]["status"] == "skipped"
        if case == "empty":
            assert summary["dart_detail"]["endpoints"] == {}
            assert summary["dart_detail"]["message"] == "no matching filings"


def test_detail_credential_echo_not_saved_or_summarized(tmp_path):
    calls = []
    run_probe(
        tmp_path,
        detail_transport(
            filings_for_details(),
            {"fricDecsn": {"status": "000", "list": [{"x": "fake-secret"}]}},
            calls,
        ),
        "--source",
        "dart_detail",
    )
    assert not list(tmp_path.rglob("fricDecsn-*.json"))
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"fake-secret" not in path.read_bytes()
    result = json.loads((tmp_path / "summary.json").read_text())["dart_detail"]
    assert result["status"] == "error"
    assert result["endpoints"]["piicDecsn"]["status"] == "ok"


def test_detail_aggregates_calls_and_preserves_error(tmp_path):
    filings = [
        filings_for_details()[0],
        {**filings_for_details()[0], "rcept_dt": "20240604"},
    ]
    calls, waits = [], []
    base = detail_transport(filings, {}, calls)
    count = 0

    def http(url, data):
        nonlocal count
        if "fricDecsn.json" in url:
            count += 1
            if count == 1:
                calls.append(url)
                return 200, b'{"status":"020"}'
        return base(url, data)

    probe.main(
        [
            "--date",
            "2024-06-03",
            "--out",
            str(tmp_path),
            "--source",
            "dart_detail",
            "--dart-bgn",
            "2024-06-03",
            "--dart-end",
            "2024-06-04",
        ],
        http=http,
        sleep=waits.append,
        environ={"OPENDART_API_KEY": "fake-secret"},
    )
    detail = json.loads((tmp_path / "summary.json").read_text())["dart_detail"]
    assert detail["status"] == "error"
    assert detail["endpoints"]["fricDecsn"]["calls"] == 2
    assert detail["endpoints"]["fricDecsn"]["total_rows"] == 1
    assert waits == [1.0, 1.0, 1.0]
    assert (
        len(list((tmp_path / "dart_detail/2024-06-03_2024-06-04").glob("*.json"))) == 2
    )


def test_detail_json_escaped_and_url_encoded_credentials(tmp_path):
    from urllib.parse import quote

    key = "fake-secret+/%"
    for index, value in enumerate((key, quote(key, safe=""))):
        folder = tmp_path / str(index)
        base = detail_transport(filings_for_details(), {}, [])

        def http(url, data, value=value, base=base):
            if "fricDecsn.json" in url:
                body = json.dumps({"status": "000", "list": [{"x": value}]})
                return 200, body.replace("fake-secret", "fake\\u002dsecret").encode()
            return base(url, data)

        run_probe(
            folder, http, "--source", "dart_detail", environ={"OPENDART_API_KEY": key}
        )
        assert not list(folder.rglob("fricDecsn-*.json"))
        detail = json.loads((folder / "summary.json").read_text())["dart_detail"]
        assert detail["endpoints"]["fricDecsn"]["status"] == "error"
        for path in folder.rglob("*"):
            if path.is_file():
                assert key.encode() not in path.read_bytes()
                assert quote(key, safe="").encode() not in path.read_bytes()


def test_http_error_empty_body_and_excerpt_bound(tmp_path):
    for index, body in enumerate((b"", b"a" * 250)):
        folder = tmp_path / str(index)
        run_probe(folder, lambda u, d, body=body: (403, body), "--source", "dart")
        result = json.loads((folder / "summary.json").read_text())["dart"]
        assert result["message"] == "HTTP status 403" + (
            ": " + "a" * 200 if body else ""
        )
        assert len(list(folder.rglob("error-*"))) == 1


def test_detail_not_requested_absent_from_summary_and_counts(tmp_path, capsys):
    for source, expected in (
        ("all", "probe: 3 sources ok, 1 skipped, 0 errors"),
        ("dart", "probe: 1 sources ok, 0 skipped, 0 errors"),
    ):
        folder, calls = tmp_path / source, []
        assert (
            run_probe(
                folder,
                detail_transport(filings_for_details(), {}, calls),
                "--source",
                source,
                environ={
                    "OPENDART_API_KEY": "fake-secret",
                    "DATA_GO_KR_SERVICE_KEY": "other-secret",
                },
            )
            == 0
        )
        summary = json.loads((folder / "summary.json").read_text())
        assert "dart_detail" not in summary
        assert not any("Decsn.json" in url for url in calls)
        assert not (folder / "dart_detail").exists()
        assert capsys.readouterr().out.splitlines()[-1] == expected


def test_detail_flag_runs_dart_dependency_once(tmp_path):
    for source in ("dart", "dart_detail", "fsc_stock"):
        folder, calls = tmp_path / source, []
        assert (
            run_probe(
                folder,
                detail_transport([], {}, calls),
                "--source",
                source,
                "--with-dart-detail",
                environ={
                    "OPENDART_API_KEY": "fake-secret",
                    "DATA_GO_KR_SERVICE_KEY": "other-secret",
                },
            )
            == 0
        )
        summary = json.loads((folder / "summary.json").read_text())
        assert summary["dart"]["status"] == summary["dart_detail"]["status"] == "ok"
        assert sum("/list.json?" in url for url in calls) == 1
        assert list(summary).index("dart") < list(summary).index("dart_detail")


def test_http_get_abandons_stalled_request(monkeypatch):
    import threading
    import time

    release = threading.Event()

    def stalled(*args, **kwargs):
        release.wait(5)
        raise AssertionError("never reached in time")

    monkeypatch.setattr(probe, "urlopen", stalled)
    started = time.monotonic()
    try:
        probe.http_get("https://example.test", None, deadline=0.2)
    except TimeoutError:
        pass
    else:
        raise AssertionError("stalled request was not abandoned")
    finally:
        release.set()
    assert time.monotonic() - started < 2


def test_stalled_source_is_retried_then_skipped_with_progress(tmp_path, capsys):
    waits, calls = [], []

    def http(url, data):
        calls.append(url)
        if "getStockPriceInfo" in url:
            raise TimeoutError("request deadline exceeded")
        return 200, fsc([], 0)

    result = probe.main(
        ["--date", "2024-06-03", "--out", str(tmp_path)],
        http=http,
        sleep=waits.append,
        environ={"DATA_GO_KR_SERVICE_KEY": "fake-secret"},
    )
    assert result == 1
    captured = capsys.readouterr()
    assert captured.err.count("timeout after") == 3
    assert "HTTP 200" in captured.err
    assert "fake-secret" not in captured.err + captured.out
    assert captured.out.splitlines()[-1].startswith("probe: ")
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["fsc_stock"]["message"] == "network retries exhausted"
    assert summary["fsc_etf"]["status"] == "ok"
