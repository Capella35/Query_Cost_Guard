#!/usr/bin/env bash
# Cost Guard live demo: five checks on BigQuery. Press Enter between steps.
# Run from anywhere: scripts/demo.sh. Nothing executes a query (BigQuery dry runs;
# the rewrite check reads a small sample, ~30 MiB billed).
# Only some steps: scripts/demo.sh 1 3 5
cd "$(dirname "$0")/.." || exit 1
STEPS=" $* "

show() {
  [ "$STEPS" = "  " ] || case "$STEPS" in *" ${1%%.*} "*) ;; *) return ;; esac
  echo
  printf '\033[1m%s\033[0m\n' "$1"
  echo "  $2"
  read -r -p "  [Enter to check]" _
  PYTHONPATH=src .venv/bin/python -m cost_guard check "$2" 2>/dev/null | .venv/bin/python -c '
import json, sys
v = json.load(sys.stdin)
d = v["decision"]
c = {"allow": "32", "warn": "33", "rewrite": "34", "block": "31"}[d]
print("\n  \033[1;%sm%s\033[0m  %s" % (c, d.upper(), v["reason"]))
if v.get("rewritten_sql"):
    print("\n  rewritten: %s\n  verified:  %s" % (v["rewritten_sql"], v["verification"]))
for h in (v.get("fix_hints") or [])[:2]:
    print("\n  hint: %s" % h["text"])
'
}

show "1. Revenue by month, 2023 only" \
  "SELECT TIMESTAMP_TRUNC(created_at, MONTH) AS month, SUM(sale_price) AS revenue FROM order_items WHERE created_at >= TIMESTAMP '2023-01-01' AND created_at < TIMESTAMP '2024-01-01' AND status = 'Complete' GROUP BY month ORDER BY month"
show "2. Orders by status, no date filter" \
  "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
show "3. Purchases in March 2025, date wrapped in a function" \
  "SELECT COUNT(*) AS purchases FROM events WHERE FORMAT_TIMESTAMP('%Y-%m', created_at) = '2025-03' AND event_type = 'purchase'"
show "4. Users joined to events on state" \
  "SELECT u.id, COUNT(*) AS n FROM users u JOIN events e ON e.state = u.state GROUP BY u.id"
show "5. A write statement" \
  "DELETE FROM orders WHERE status = 'Cancelled'"
echo
