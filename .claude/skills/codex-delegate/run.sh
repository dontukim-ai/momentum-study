#!/usr/bin/env bash
# Codex 위임 실행기. 사용법:
#   run.sh <ID> <기준 브랜치> <작업 브랜치>     새 worktree에서 작업 파일로 실행
#   run.sh <ID> --resume "<수정 지시>"          같은 worktree·같은 Codex 세션에 수정 지시
# 작업 파일: <repo>/.codex-tasks/<ID>.md (Git 제외). 보고서·로그도 같은 곳에 남긴다.
set -euo pipefail

ID="${1:?task id}"
REPO="$(git rev-parse --show-toplevel)"
SKILL="$REPO/.claude/skills/codex-delegate"
TASKS="$REPO/.codex-tasks"
TASK="$TASKS/$ID.md"
WT="$(dirname "$REPO")/momentum-study-wt/$ID"
mkdir -p "$TASKS"

# Codex sandbox cannot write the default uv cache; keep it inside /tmp.
export UV_CACHE_DIR=/tmp/momentum-uv-cache

run_codex() {  # $1: attempt label, rest: codex args
  local label="$1"; shift
  local log="$TASKS/$ID.$label.jsonl"
  (cd "$WT" && codex "$@" --json -o "$TASKS/$ID.report.md") >"$log" 2>"$TASKS/$ID.$label.stderr" || {
    echo "codex exited non-zero; see $log and $TASKS/$ID.$label.stderr" >&2
  }
  # Keep the thread id so a follow-up can continue with the same context.
  python3 - "$log" "$TASKS/$ID.thread" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    tid = event.get("thread_id") or (event.get("thread") or {}).get("id")
    if tid:
        open(sys.argv[2], "w").write(tid)
        break
PY
}

if [[ "${2:-}" == "--resume" ]]; then
  MSG="${3:?수정 지시}"
  [[ -d "$WT" && -s "$TASKS/$ID.thread" ]] || { echo "no previous run for $ID" >&2; exit 2; }
  # No earlier retry is the normal first case; a failed glob must not abort under -e.
  n=$(find "$TASKS" -maxdepth 1 -name "$ID.retry*.jsonl" | wc -l)
  run_codex "retry$((n + 1))" exec resume "$(cat "$TASKS/$ID.thread")" \
    -c sandbox_mode='"workspace-write"' "$MSG"$'\n\n'"끝나면 같은 완료 보고 형식으로 다시 보고한다."
else
  BASE="${2:?base branch}"; BRANCH="${3:?work branch}"
  [[ -s "$TASK" ]] || { echo "missing task file $TASK" >&2; exit 2; }
  git -C "$REPO" fetch -q origin "+refs/heads/$BASE:refs/remotes/origin/$BASE"
  if [[ ! -d "$WT" ]]; then
    git -C "$REPO" worktree add -q -B "$BRANCH" "$WT" "origin/$BASE"
  fi
  # The Codex sandbox has no network: install both environments before handing over.
  (cd "$WT" && uv sync --frozen -q && uv sync --frozen -q --directory apps/kis-agent)
  cat "$SKILL/common-header.md" "$TASK" | run_codex run exec -C "$WT" --sandbox workspace-write -
fi

echo "== report: $TASKS/$ID.report.md"
echo "== changed files"
git -C "$WT" status --short
# Lines under "## 허용 경로" are "- <path or dir prefix>"; anything else changed is out of scope.
python3 - "$TASK" "$WT" <<'PY'
import re, subprocess, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^## 허용 경로\n(.*?)(?=^## |\Z)", text, re.M | re.S)
allowed = [l[2:].strip().split()[0] for l in (m.group(1) if m else "").splitlines() if l.startswith("- ")]
out = subprocess.run(["git", "-C", sys.argv[2], "status", "--porcelain", "-uall"],
                     capture_output=True, text=True, check=True).stdout.splitlines()
bad = [p[3:] for p in out if not any(p[3:] == a or p[3:].startswith(a.rstrip("*").rstrip("/") + "/")
                                       or (a.endswith("*") and p[3:].startswith(a.rstrip("*"))) for a in allowed)]
print("== out of scope:" if bad else "== out of scope: none")
for p in bad:
    print("  " + p)
PY
