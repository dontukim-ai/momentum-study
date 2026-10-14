from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError, load_config

CFG = Path("config")

VALID_UNIVERSE_YAML = """\
min_close_usd: 5.0
min_adv_usd: 20000000
adv_lookback_days: 60
min_history_days: 252
max_names: 1000
"""

VALID_EXECUTION_YAML = """\
base_cost_bps: 10.0
stress_cost_bps: 25.0
optimistic_cost_bps: 5.0
participation_limit: 0.001
participation_lookback_days: 20
account_equity_usd: 30000
max_positions: 50
max_weight: 0.05
min_positions_for_full_investment: 20
cash_buffer: 0.005
cash_interest_rate: 0.0
"""

VALID_VALIDATION_YAML = """\
windows:
  development: {start: 2000-01-03, end: 2012-12-31}
  walkforward: {start: 2013-01-02, end: 2020-12-31}
  holdout:     {start: 2021-01-04, end: 2025-12-31}
bootstrap_block_days: 63
bootstrap_block_sensitivity: [21, 126]
bootstrap_iterations: 10000
seed: 20260916
alpha: 0.05
"""

VALID_HYPOTHESES_YAML = """\
hypotheses:
  - {id: H1, top_pct: 0.10, require_positive: true,  rebalance: daily,   lookback: 1d}
  - {id: H2, top_pct: 0.10, require_positive: true,  rebalance: weekly,  lookback: 1w}
  - {id: H3, top_pct: 0.10, require_positive: true,  rebalance: monthly, lookback: 1mo,
     secondary_filter: turnover_top_30pct}
  - {id: H4, top_pct: 0.10, require_positive: false, rebalance: monthly, lookback: 12mo, skip: 1mo}
"""


@pytest.fixture
def valid_config_dir(tmp_path):
    (tmp_path / "universe.yaml").write_text(VALID_UNIVERSE_YAML)
    (tmp_path / "execution.yaml").write_text(VALID_EXECUTION_YAML)
    (tmp_path / "validation.yaml").write_text(VALID_VALIDATION_YAML)
    (tmp_path / "hypotheses.yaml").write_text(VALID_HYPOTHESES_YAML)
    return tmp_path


def test_defaults_match_spec():
    c = load_config(CFG)
    assert c.execution.base_cost_bps == 10.0
    assert c.execution.account_equity_usd == 30000
    assert c.execution.max_positions == 50
    assert c.execution.max_weight == 0.05
    assert c.universe.min_close_usd == 5.0
    assert c.universe.min_adv_usd == 20_000_000
    assert c.validation.bootstrap_block_days == 63
    assert c.validation.bootstrap_iterations == 10_000


def test_phase_windows_are_disjoint_and_ordered():
    w = load_config(CFG).validation.windows
    assert w["development"].end < w["walkforward"].start
    assert w["walkforward"].end < w["holdout"].start


def test_hash_is_stable_and_is_a_sha256():
    assert load_config(CFG).hash == load_config(CFG).hash
    assert len(load_config(CFG).hash) == 64


def test_valid_fixture_loads_successfully(valid_config_dir):
    c = load_config(valid_config_dir)
    assert c.execution.base_cost_bps == 10.0


def test_unknown_key_is_an_error_not_a_silent_default(valid_config_dir):
    (valid_config_dir / "execution.yaml").write_text(
        VALID_EXECUTION_YAML + "mystery_knob: 3\n"
    )
    with pytest.raises(SpecUndefinedError, match="mystery_knob"):
        load_config(valid_config_dir)


def test_missing_required_file_is_an_error(valid_config_dir):
    (valid_config_dir / "validation.yaml").unlink()
    with pytest.raises(SpecUndefinedError):
        load_config(valid_config_dir)


def test_textually_different_but_semantically_equal_yaml_changes_hash(valid_config_dir):
    baseline = load_config(valid_config_dir).hash
    (valid_config_dir / "execution.yaml").write_text(
        VALID_EXECUTION_YAML.replace("max_weight: 0.05", "max_weight: .05")
    )
    reformatted = load_config(valid_config_dir).hash
    assert reformatted != baseline


def test_added_comment_changes_hash(valid_config_dir):
    baseline = load_config(valid_config_dir).hash
    (valid_config_dir / "execution.yaml").write_text(
        "# a harmless comment\n" + VALID_EXECUTION_YAML
    )
    commented = load_config(valid_config_dir).hash
    assert commented != baseline


def test_non_mapping_top_level_document_is_an_error(valid_config_dir):
    (valid_config_dir / "execution.yaml").write_text("- 1\n- 2\n")
    with pytest.raises(SpecUndefinedError, match="execution.yaml"):
        load_config(valid_config_dir)
