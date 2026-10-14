import importlib.util
import pathlib

_MODULE_PATH = pathlib.Path(__file__).resolve().parents[2] / ".claude" / "guard_holdout.py"
_spec = importlib.util.spec_from_file_location("guard_holdout", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
guard_holdout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard_holdout)


def test_denies_bare_holdout_phase():
    assert guard_holdout.should_deny("run --phase holdout") is True


def test_allows_release_holdout():
    assert guard_holdout.should_deny("run --phase holdout release-holdout") is False


def test_allows_unrelated_command():
    assert guard_holdout.should_deny("run --phase train") is False


def test_deny_payload_shape():
    payload = guard_holdout.build_deny_payload()
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert "release-holdout" in out["permissionDecisionReason"]
