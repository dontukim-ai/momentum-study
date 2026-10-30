#!/usr/bin/env bash
# v20 B daily moving-average screen (docs/blueprint/ma-cross-v20.md). Mac, weekdays from 13:30 KST.
#
#   REPO=/path/to/momentum-study tools/ma_screen_daily.sh [YYYY-MM-DD]
#
# Screens the previous weekday (or the given day). FSC quotes from the day after the last saved one up to
# that day are first fetched into a temporary root; a day is moved into data/krx/raw/fsc/stock only if it
# has quotes or lies before the screened day. An empty screened day (holiday, or not yet published) is
# never saved, because a saved empty day is never fetched again and would harden into a holiday.
# Runs in its own worktree (../wt-ma-screen) so the main folder's branch never changes; writes one txt
# beside the checkout and adds it to the branch results/ma-screen (one file per day, never force-pushed).
# Read-only market data; no orders. A failed step is written to the txt as it is and not retried.
set -u
REPO=${REPO:?set REPO to the repository checkout}
DAY=${1:-$(TZ=Asia/Seoul python3 -c 'import datetime as d; t=d.date.today(); t-=d.timedelta(days=1)
while t.weekday()>4: t-=d.timedelta(days=1)
print(t)')}
WT="$REPO/../wt-ma-screen"
OUT="$REPO/../ma-screen-$DAY.txt"
BRANCH=results/ma-screen
STOCK="$REPO/data/krx/raw/fsc/stock"

cd "$REPO" || exit 1
git fetch -q origin main
if [ ! -d "$WT" ]; then
  git worktree add -q --detach "$WT" origin/main || exit 1
  ln -s "$REPO/data" "$WT/data"
fi
cd "$WT" || exit 1
git checkout -q --detach origin/main
TMP=$(mktemp -d "$REPO/../fsc-tmp.XXXXXX")

{
  echo "ma screen $DAY, run $(TZ=Asia/Seoul date '+%F %T') KST, main $(git rev-parse --short HEAD)"
  LAST=$(ls "$STOCK" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort | tail -1)
  START=$(python3 -c "import datetime as d; print(d.date.fromisoformat('$LAST') + d.timedelta(days=1))")
  if [[ "$START" > "$DAY" ]]; then
    echo "collect: nothing to fetch (last saved $LAST)"
  else
    uv run python tools/fsc_collect.py --kind stock --start "$START" --end "$DAY" --root "$TMP" --keep-going
    echo "collect_exit=$?"
    for dir in "$TMP"/stock/*/; do
      [ -d "$dir" ] || continue
      d=$(basename "$dir")
      total=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('total_count', 0))" "$dir/fetch.json")
      if [ -e "$STOCK/$d" ]; then
        echo "keep existing $d"
      elif [ "$d" = "$DAY" ] && [ "$total" = "0" ]; then
        echo "not saved $d: no quotes yet (holiday, or not yet published)"
      else
        mv "$dir" "$STOCK/$d" && echo "saved $d ($total rows)"
      fi
    done
  fi
  if [ -d "$STOCK/$DAY" ]; then
    uv run python tools/ma_screen.py --root data/krx/raw/fsc --day "$DAY"
    echo "screen_exit=$?"
  else
    echo "screen skipped: no quotes for $DAY"
  fi
} > "$OUT" 2>&1
rm -rf "$TMP"

if git fetch -q origin "$BRANCH" 2>/dev/null; then
  git checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git checkout -q -B "$BRANCH" origin/main
fi
mkdir -p results
cp "$OUT" results/
git add results && git commit -qm "results: ma screen $DAY" && git push -q origin "$BRANCH"
echo "push_exit=$?"
git checkout -q --detach origin/main
