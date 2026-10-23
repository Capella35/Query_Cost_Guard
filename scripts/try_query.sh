#!/usr/bin/env bash
# Type a SQL query at the prompt and see Query Preflight's verdict. Nothing runs:
# BigQuery dry runs (free); a rewrite check reads a small sample.
# Run from anywhere: scripts/try_query.sh. Empty line or "quit" exits.
cd "$(dirname "$0")/.." || exit 1
if [ -f .env ]; then set -a; . ./.env; set +a; fi

while true; do
  echo
  read -r -e -p "SQL> " q || break
  case "$q" in ""|quit|exit) break ;; esac
  out=$(PYTHONPATH=src .venv/bin/python -m query_preflight check "$q" 2>&1)
  printf '%s' "$out" | .venv/bin/python -c '
import json, re, sys
raw = sys.stdin.read()
start = raw.find("{\n")
try:
    v = json.loads(raw[start:]) if start >= 0 else None
except ValueError:
    v = None
if v is None:
    m = re.findall(r"(?:BadRequest|NotFound|Error|Exception)[^:]*: (?:\d{3} \S+ \S+: )?(.+)", raw)
    print("  \033[1;35mINVALID\033[0m  %s" % (m[-1].strip() if m else raw.strip().splitlines()[-1]))
    sys.exit()
d = v["decision"]
c = {"allow": "32", "warn": "33", "rewrite": "34", "block": "31"}[d]
print("  \033[1;%sm%s\033[0m  %s" % (c, d.upper(), v["reason"]))
if v.get("rewritten_sql"):
    print("  rewritten: %s\n  verified:  %s" % (v["rewritten_sql"], v["verification"]))
for h in (v.get("fix_hints") or [])[:2]:
    print("  hint: %s" % h["text"])
'
done
