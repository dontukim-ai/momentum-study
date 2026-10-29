#!/usr/bin/env bash
# v19b daily forward update (docs/blueprint/pattern-lab-v19.md §10.5). Mac, after the close (16:10 KST or later).
#
#   REPO=~/dev/quant tools/v19b_daily.sh [YYYY-MM-DD]
#
# Runs in its own worktree (../wt-v19b-daily, data linked to the main checkout) so it never switches the
# main folder's branch. Collects 069500 minute bars from 2026-10-26 to the day (finished days are not
# fetched again), re-runs the online record from the development seed through every forward day, writes
# one summary txt beside the checkout and adds it to the branch results/v19b-daily (one file per day, never force-pushed).
# Read-only market data; no orders. A failed step is written to the txt as it is and not retried.
set -u
REPO=${REPO:?set REPO to the repository checkout}
DAY=${1:-$(TZ=Asia/Seoul date +%F)}
WT="$REPO/../wt-v19b-daily"
OUT="$REPO/../v19b-daily-$DAY.txt"   # beside the checkout, like the other result txts (never inside it)
BRANCH=results/v19b-daily

cd "$REPO" || exit 1
git fetch -q origin main
if [ ! -d "$WT" ]; then
  git worktree add -q --detach "$WT" origin/main || exit 1
  ln -s "$REPO/data" "$WT/data"
fi
cd "$WT" || exit 1
git checkout -q --detach origin/main
UNIVERSE=data/kis/universe-069500.json
[ -f "$UNIVERSE" ] || echo '{"symbols": [{"symbol": "069500"}]}' > "$UNIVERSE"

{
  echo "v19b daily $DAY, run $(TZ=Asia/Seoul date '+%F %T') KST, main $(git rev-parse --short HEAD)"
  uv run --extra agent python tools/kis_minute_history.py --universe "$UNIVERSE" --start 2026-10-26 --end "$DAY"
  echo "collect_exit=$?"
  uv run python tools/pattern_lab.py online-score --bars data/kis/minute-history
  echo "score_exit=$?"
} > "$OUT" 2>&1

if git fetch -q origin "$BRANCH" 2>/dev/null; then
  git checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git checkout -q -B "$BRANCH" origin/main
fi
mkdir -p results
cp "$OUT" results/
git add results && git commit -qm "results: v19b daily $DAY" && git push -q origin "$BRANCH"
echo "push_exit=$?"
git checkout -q --detach origin/main
