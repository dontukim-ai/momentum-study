import json
import sys


def should_deny(cmd: str) -> bool:
    """Decide whether a Bash command must be denied by the holdout guard.

    Denies any command that touches ``--phase holdout`` unless it goes
    through the explicit ``release-holdout`` path.
    """
    return "--phase holdout" in cmd and "release-holdout" not in cmd


def build_deny_payload() -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "holdout 구간은 release-holdout 명령과 동결 manifest로만 실행한다.",
        }
    }


def main() -> None:
    payload = json.load(sys.stdin)
    cmd = payload.get("tool_input", {}).get("command", "")
    if should_deny(cmd):
        print(json.dumps(build_deny_payload()))
    sys.exit(0)


if __name__ == "__main__":
    main()
