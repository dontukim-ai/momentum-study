"""Versioned statistical budgets must never silently reinterpret old studies."""

import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from trading_agent.contracts import AgentError, canonical, digest
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, policy_from_dict
from momentum.agent.service import ResearchAgent, confidence_interval, promotion_gates


@pytest.mark.parametrize(
    "values,expected",
    [
        (
            [((i * 7) % 17 - 8) / 1000 for i in range(40)],
            ("-0x1.758e219652bd3p-10", "0x1.a36e2eb1c432dp-11"),
        ),
        (
            [0.001 + 0.005 * (i % 9 - 4) for i in range(40)],
            ("-0x1.cac083126e97ap-8", "0x1.89374bc6a7efap-8"),
        ),
        ([0.001] * 40, ("0x1.0624dd2f1a9fcp-10", "0x1.0624dd2f1a9fcp-10")),
    ],
)
def test_v1_confidence_interval_is_bit_identical(values, expected):
    p = AgentPolicy()
    assert p.statistical_policy_version == "v1"
    assert tuple(x.hex() for x in confidence_interval(values, p)) == expected


def test_v2_rejects_unstable_tail_and_accepts_explicit_larger_budget():
    with pytest.raises(AgentError, match="tail"):
        AgentPolicy(statistical_policy_version="v2")
    p = AgentPolicy(statistical_policy_version="v2", bootstrap_samples=10000)
    assert p.bootstrap_samples * p.family_alpha / p.max_validation_cycles == 62.5
    lower, upper = confidence_interval([0.001] * 40, p)
    assert lower == 0.001
    assert upper is None


def test_missing_version_in_legacy_policy_json_defaults_to_v1():
    raw = json.loads(canonical(AgentPolicy()))
    raw.pop("statistical_policy_version", None)
    raw.pop("min_tail_draws", None)
    p = policy_from_dict(raw)
    assert p.statistical_policy_version == "v1"
    assert p.min_tail_draws == 50


@pytest.mark.parametrize(
    "updates",
    [
        {"statistical_policy_version": "v3"},
        {"min_tail_draws": 0},
        {"min_tail_draws": True},
        {"min_tail_draws": 1.5},
    ],
)
def test_statistical_policy_fields_are_strict(updates):
    with pytest.raises(AgentError):
        AgentPolicy(**updates)


def test_policy_version_changes_manifest_and_cycle_identity(tmp_path):
    from test_agent import market, policy

    manifests = []
    for version in ("v1", "v2"):
        store = Store(tmp_path / f"{version}.sqlite3")
        try:
            p = replace(
                policy(), statistical_policy_version=version, bootstrap_samples=10000
            )
            agent = ResearchAgent(store)
            agent.init("versioned", p)
            agent.tick("versioned", market())
            row = store.db.execute(
                "SELECT id,manifest_json,evidence_json FROM agent_cycles"
            ).fetchone()
            manifest, evidence = json.loads(row[1]), json.loads(row[2])
            assert row[0] == digest(manifest)
            assert evidence["statistical_policy_version"] == version
            alpha = (
                p.family_alpha / p.max_validation_cycles / (2 if version == "v1" else 1)
            )
            assert evidence["nominal_one_sided_alpha_per_cycle"] == alpha
            assert evidence["tail_draws"] == 10000 * alpha
            manifests.append((row[0], manifest))
            if version == "v2":
                assert evidence["holdout"]["interval"][1] is None
                assert evidence["gates"]["confidence"]
                v1_evidence = {
                    **evidence,
                    "holdout": {**evidence["holdout"], "interval": [0.001, 0.002]},
                }
                assert {
                    k: v
                    for k, v in promotion_gates(
                        v1_evidence, replace(p, statistical_policy_version="v1")
                    ).items()
                    if k != "confidence"
                } == {k: v for k, v in evidence["gates"].items() if k != "confidence"}
        finally:
            store.close()
    assert manifests[0][0] != manifests[1][0]
    assert manifests[0][1]["policy"] != manifests[1][1]["policy"]


def test_legacy_policy_json_can_initialize_and_finish_without_rewriting_history(
    tmp_path,
):
    from test_agent import market, policy

    p = policy()
    store = Store(tmp_path / "legacy-policy.sqlite3")
    try:
        agent = ResearchAgent(store)
        agent.init("legacy", p)
        raw = asdict(p)
        raw.pop("statistical_policy_version", None)
        raw.pop("min_tail_draws", None)
        legacy = canonical(raw)
        store.db.execute("UPDATE agent_studies SET policy_json=?", (legacy,))
        agent.init("legacy", p)
        assert agent.tick("legacy", market())["cycles"][0]["research_champion_updated"]
        assert (
            store.db.execute("SELECT policy_json FROM agent_studies").fetchone()[0]
            == legacy
        )
        with pytest.raises(AgentError, match="Preserve"):
            agent.init(
                "legacy",
                replace(p, statistical_policy_version="v2", bootstrap_samples=10000),
            )
    finally:
        store.close()


def test_offline_stat_check_emits_json_for_explicit_development_window():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools/stat_policy_check.py"),
            "--null-trials",
            "2",
            "--seeds",
            "0,1",
            "--blocks",
            "2,5",
            "--development-start",
            "2024-01-02",
            "--development-end",
            "2024-03-01",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    report = json.loads(result.stdout)
    assert report["mode"] == "SYNTHETIC_STATISTICAL_DIAGNOSTICS"
    assert set(report["null_simulation"]["policies"]) == {"v1", "v2"}
    assert set(report["quantile_stability"]) == {"v1", "v2"}
    assert report["block_sensitivity"]["input_role"] == "development"
    assert report["development_window"]["start"] == "2024-01-02"
    assert report["policy_modified"] is False
