"""Immutable input publication and worker version isolation regressions."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_agent import market
from trading_agent.contracts import AgentError
from trading_agent.data import write_csv
from trading_agent.selection import write_security_history

from momentum.agent import cli
from momentum.agent.bundle import open_bundle, publish_bundle

BASIS = "raw-unadjusted; supplied-actions-complete"


def inputs(path, count=70):
    path.mkdir()
    data = market(count)
    write_csv(data.snapshot, path / "prices.csv")
    write_security_history(data.securities, path / "securities.jsonl")
    return {name: path / name for name in ("prices.csv", "securities.jsonl")}


def publish(root, files):
    return publish_bundle(
        root,
        files,
        basis=BASIS,
        sources={"fixture": True},
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


def fingerprint(data):
    return data.fingerprint(data.sessions[-1])


def test_round_trip_and_identical_republication(tmp_path):
    files = inputs(tmp_path / "source")
    root = tmp_path / "root"
    first = publish(root, files)
    directory = root / "bundles" / first["bundle_id"]
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()
    }
    data, manifest = open_bundle(root)
    assert manifest == first
    assert fingerprint(data) == fingerprint(cli.read_input(*files.values()))
    second = publish_bundle(
        root,
        files,
        basis=BASIS,
        sources={"fixture": True},
        clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
    )
    assert second == first
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()
    }


def test_current_changes_after_single_read_keep_one_version(tmp_path, monkeypatch):
    root = tmp_path / "root"
    v1 = publish(root, inputs(tmp_path / "one"))
    files2 = inputs(tmp_path / "two", 71)
    expected1 = fingerprint(open_bundle(root)[0])
    original = Path.read_text
    reads = []

    def interleave(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == root / "CURRENT":
            reads.append(value)
            publish(root, files2)
        return value

    monkeypatch.setattr(Path, "read_text", interleave)
    data, manifest = open_bundle(root)
    assert len(reads) == 1
    assert manifest["bundle_id"] == v1["bundle_id"]
    assert fingerprint(data) == expected1
    monkeypatch.setattr(Path, "read_text", original)
    next_data, next_manifest = open_bundle(root)
    assert next_manifest["bundle_id"] != v1["bundle_id"]
    assert fingerprint(next_data) != expected1


def test_invalid_publication_preserves_current_and_cleans_staging(tmp_path):
    root = tmp_path / "root"
    files = inputs(tmp_path / "source")
    publish(root, files)
    before = (root / "CURRENT").read_bytes()
    directories = list((root / "bundles").iterdir())
    files["prices.csv"].write_text("broken\n")
    with pytest.raises(AgentError):
        publish(root, files)
    assert (root / "CURRENT").read_bytes() == before
    assert list((root / "bundles").iterdir()) == directories
    assert not list((root / "staging").iterdir())


@pytest.mark.parametrize("damage", ["modify", "delete", "extra", "manifest"])
def test_integrity_failure(tmp_path, damage):
    root = tmp_path / "root"
    manifest = publish(root, inputs(tmp_path / "source"))
    directory = root / "bundles" / manifest["bundle_id"]
    if damage == "modify":
        path = directory / "prices.csv"
        content = path.read_bytes()
        path.write_bytes(b"X" + content[1:])
    elif damage == "delete":
        (directory / "prices.csv").unlink()
    elif damage == "extra":
        (directory / "extra").write_text("unexpected")
    else:
        path = directory / "manifest.json"
        path.write_text(path.read_text().replace(BASIS, "different"))
    with pytest.raises(AgentError, match="^Input bundle integrity failure$"):
        open_bundle(root)


def test_cli_bundle_publish_run_and_conflict(tmp_path, monkeypatch):
    class FixedClockAgent(cli.ResearchAgent):
        def __init__(self, store, **kwargs):
            super().__init__(store, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    monkeypatch.setattr(cli, "ResearchAgent", FixedClockAgent)
    files = inputs(tmp_path / "source", 230)
    root = tmp_path / "root"
    common = ["--output", str(tmp_path / "output")]
    paths = [
        "--csv",
        str(files["prices.csv"]),
        "--securities",
        str(files["securities.jsonl"]),
    ]
    manifest = cli.run(
        cli.parser().parse_args(
            ["bundle-publish", "--bundle", str(root), *paths, *common]
        )
    )
    results = []
    for name, flags in (("direct", paths), ("bundle", ["--bundle", str(root)])):
        db = ["--db", str(tmp_path / (name + ".sqlite3"))]
        cli.run(cli.parser().parse_args(["init", *db, *common]))
        results.append(cli.run(cli.parser().parse_args(["run", *db, *flags, *common])))
    assert results[1]["input_bundle"]["bundle_id"] == manifest["bundle_id"]
    assert results[0]["cycles"] == results[1]["cycles"]
    assert results[0]["status"]["champion"] == results[1]["status"]["champion"]
    with pytest.raises(AgentError, match="bundle"):
        cli.run(
            cli.parser().parse_args(["run", "--bundle", str(root), *paths, *common])
        )


def test_optional_files_and_explicit_version(tmp_path):
    root = tmp_path / "root"
    files = inputs(tmp_path / "source")
    direct = cli.read_input(*files.values())
    for name, value in (
        ("actions.json", []),
        ("sessions.json", [d.isoformat() for d in direct.sessions]),
        ("calendar.json", [d.isoformat() for d in direct.sessions]),
    ):
        files[name] = tmp_path / name
        files[name].write_text(json.dumps(value))
    manifest = publish(root, files)
    (root / "CURRENT").write_text("invalid")
    opened, _ = open_bundle(root, manifest["bundle_id"])
    expected = cli.read_input(
        files["prices.csv"],
        files["securities.jsonl"],
        files["actions.json"],
        files["sessions.json"],
        BASIS,
        files["calendar.json"],
    )
    assert fingerprint(opened) == fingerprint(expected)


def test_watch_resolves_bundle_each_tick(tmp_path, monkeypatch):
    root = tmp_path / "root"
    first = publish(root, inputs(tmp_path / "one"))
    second_files = inputs(tmp_path / "two", 71)
    common = ["--db", str(tmp_path / "db.sqlite3"), "--output", str(tmp_path / "out")]
    cli.run(cli.parser().parse_args(["init", *common]))
    original_open = cli.open_bundle
    original_save = cli.save_json
    observed = []
    reports = []

    def interleave(path):
        data, manifest = original_open(path)
        observed.append(manifest["bundle_id"])
        if len(observed) == 1:
            publish(root, second_files)
        return data, manifest

    def save(path, value):
        if path.name == "agent.json":
            reports.append(value["input_bundle"]["bundle_id"])
        original_save(path, value)

    monkeypatch.setattr(cli, "open_bundle", interleave)
    monkeypatch.setattr(cli, "save_json", save)
    # Preserve the public watch contract while making its pause deterministic.
    original_watch = cli.watch
    monkeypatch.setattr(
        cli, "watch", lambda *a, **kw: original_watch(*a, **kw, pause=lambda _: None)
    )
    result = cli.run(
        cli.parser().parse_args(
            [
                "run",
                "--bundle",
                str(root),
                "--watch",
                "--max-ticks",
                "2",
                *common,
            ]
        )
    )
    assert observed[0] == first["bundle_id"]
    assert observed[1] != observed[0]
    assert reports == observed
    assert result["input_bundle"]["bundle_id"] == observed[1]


def test_source_metadata_requires_object_before_publication(tmp_path):
    root = tmp_path / "root"
    files = inputs(tmp_path / "source")
    with pytest.raises(AgentError):
        publish_bundle(
            root,
            files,
            basis=BASIS,
            sources=[],
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
    assert not (root / "CURRENT").exists()


def test_existing_empty_bundle_directory_is_never_replaced(tmp_path):
    files = inputs(tmp_path / "source")
    manifest = publish(tmp_path / "reference", files)
    root = tmp_path / "root"
    target = root / "bundles" / manifest["bundle_id"]
    target.mkdir(parents=True)
    before = target.stat().st_ino
    with pytest.raises(AgentError, match="Input bundle integrity failure"):
        publish(root, files)
    assert target.stat().st_ino == before
    assert not list(target.iterdir())
    assert not (root / "CURRENT").exists()


def test_invalid_clock_preserves_current_and_bundle_directories(tmp_path):
    root = tmp_path / "root"
    files = inputs(tmp_path / "source")
    publish(root, files)
    before = (root / "CURRENT").read_bytes()
    directories = set((root / "bundles").iterdir())
    with pytest.raises(AgentError):
        publish_bundle(
            root,
            files,
            basis=BASIS,
            sources={"new": True},
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        )
    assert (root / "CURRENT").read_bytes() == before
    assert set((root / "bundles").iterdir()) == directories
    assert not list((root / "staging").iterdir())
