"""CLI: python -m cost_guard {check,run,investigate} ...

  check "SQL"      Guard verdict only (nothing executes).
  run "SQL"        Guard, then execute under the watchdog; logs to runs/query_log.jsonl.
  investigate      Baselines from the query log -> threshold proposals.
  investigate-anomalies
                   Report-only investigator agent over flagged runs in the log
                   that have no report yet (runs/investigations/); changes nothing.
  report           Workload report from the query log: warn follow-through,
                   cost by query shape, result-cache opportunity, spend ledger.

The warehouse is the DuckDB file from customer.yaml (warehouse.duckdb.db_path)
unless --db is given. --llm claude enables the LLM explainer/rewrite proposer
(needs Anthropic credentials); the default is no LLM at all.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict


def _backend(args, customer):
    from .backends.duckdb_backend import DuckDBWarehouse

    db = args.db or customer["warehouse"]["duckdb"]["db_path"]
    return DuckDBWarehouse(db)


def _llm(args, backend):
    if args.llm == "claude":
        from .llm import ClaudeLLM

        return ClaudeLLM(backend=backend)
    return None


def main(argv: list[str] | None = None) -> int:
    from .config import caps_for, load_customer, load_dotenv

    load_dotenv()  # .env settings (e.g. COST_GUARD_LOG_KEY), shell variables win

    parser = argparse.ArgumentParser(prog="cost_guard", description="Cost Guard + Investigator CLI")
    parser.add_argument("--customer", help="path to customer.yaml (default: repo root)")
    parser.add_argument("--db", help="DuckDB file (default: from customer.yaml)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "run"):
        p = sub.add_parser(name)
        p.add_argument("sql")
        p.add_argument("--workload", default="interactive")
        p.add_argument("--llm", choices=["none", "claude"], default="none")
        if name == "run":
            p.add_argument("--user", help="opaque caller id (pseudonymize upstream)")
            p.add_argument("--bypass-reason", help="run a cost-blocked query anyway "
                           "(only when customer.yaml guard.allow_bypass is true)")
    p = sub.add_parser("report")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--top", type=int, default=10)
    p = sub.add_parser("investigate")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--workload", default="interactive")
    p = sub.add_parser("investigate-anomalies")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--traces", default="runs/traces.jsonl")
    p.add_argument("--llm", choices=["none", "claude"], default="none",
                   help="optional summary, within customer.yaml investigator.llm_max_per_day")
    args = parser.parse_args(argv)
    customer = load_customer(args.customer)

    if args.cmd == "check":
        from .guard import decide

        backend = _backend(args, customer)
        v = decide(args.sql, args.workload, customer=customer, backend=backend,
                   llm=_llm(args, backend))
        print(json.dumps(asdict(v), indent=2, default=str))
        return 2 if v.decision == "block" else 0

    if args.cmd == "run":
        from .runtime import run_query

        backend = _backend(args, customer)
        out = run_query(args.sql, backend, args.workload, customer=customer,
                        user=args.user, bypass_reason=args.bypass_reason, llm=_llm(args, backend))
        print(json.dumps({
            "decision": out.verdict.decision,
            "reason": out.verdict.reason,
            "execution": asdict(out.execution) if out.execution else None,
            "timeout_sec": out.timeout_sec,
        }, indent=2, default=str))
        return 2 if out.verdict.decision == "block" else 0

    if args.cmd == "report":
        from .ledger import build_report
        from .runtime import read_log

        print(json.dumps(build_report(read_log(args.log), customer, args.top), indent=2))
        return 0

    if args.cmd == "investigate-anomalies":
        from .investigator_agent import events_from_log
        from .mcp_server import CostGuardService
        from .runtime import read_log

        backend = _backend(args, customer)
        svc = CostGuardService(backend, customer, args.log, args.traces, investigations=True,
                               agent_llm=_llm(args, backend))
        done = skipped = 0
        for ev in events_from_log(read_log(args.log)):
            rep = svc.agent.investigate(ev)
            if rep is None:
                skipped += 1
                continue
            done += 1
            print(f"{rep.id}: {rep.anomaly_kind} — {rep.likely_causes[0] if rep.likely_causes else '?'}")
        print(f"{done} report(s) written to {svc.agent.out_dir}, {skipped} skipped "
              "(already reported, cooldown or daily cap)")
        return 0

    if args.cmd == "investigate":
        from .investigator import investigate
        from .memory import EpisodicMemory
        from .tools.get_query_history import baseline_store_from_log

        store = baseline_store_from_log(args.log)
        hard_cap = caps_for(customer, args.workload)["max_runtime_sec"]
        rep = investigate(store, EpisodicMemory(), hard_cap_sec=hard_cap)
        for s in rep.steps:
            print("step:", s)
        for prop in rep.proposals:
            print(json.dumps(asdict(prop), indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
