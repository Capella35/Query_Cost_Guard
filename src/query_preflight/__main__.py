"""CLI: python -m query_preflight {check,run,investigate} ...

  check "SQL"      Guard verdict only (nothing executes).
  run "SQL"        Guard, then execute under the watchdog; logs to runs/query_log.jsonl.
  investigate      Baselines from the query log -> threshold proposals.
  accept-normal ID Accept a query shape's flagged runs so far as its new normal
                   (anomaly option B); later spikes are still flagged. --list shows
                   acceptances. A person's decision: not offered through MCP.
  investigate-anomalies
                   Report-only investigator agent over flagged runs in the log
                   that have no report yet (runs/investigations/); changes nothing.
  report           Workload report from the query log: warn follow-through,
                   cost by query shape, result-cache opportunity, spend ledger.
  registry status  Column registry freshness per table + the last refresh run
                   (reads the local file; no warehouse query).
  registry refresh Rebuild the column registry now (--mode stale|missing|all).
                   A REQUIRED, scheduled job in a deployment (DESIGN.md §11);
                   here it runs when the owner decides. --backend bigquery uses
                   GCP_PROJECT from .env; each scan is dry-run first and capped.

The warehouse is customer.yaml warehouse.backend (BigQuery: project from
GCP_PROJECT, dataset cost_guard_thelook) unless --backend is given; --db alone
means the DuckDB file (tests, the demo). --llm claude enables the LLM explainer/rewrite proposer
(needs Anthropic credentials); the default is no LLM at all.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict


def _backend(args, customer):
    """The warehouse per config.choose_backend: --backend, else --db (DuckDB),
    else QUERY_PREFLIGHT_BACKEND, else customer.yaml (BigQuery)."""
    from .config import make_backend

    return make_backend(customer, args.backend, args.project, args.dataset, args.db)


def _llm(args, backend):
    if args.llm == "claude":
        from .llm import ClaudeLLM

        return ClaudeLLM(backend=backend)
    return None


def _registry(args, customer) -> int:
    from . import column_registry as cr
    from .config import choose_backend, warehouse_settings

    settings = cr.registry_settings(customer)
    wcfg = warehouse_settings(customer)
    backend = choose_backend(customer, args.backend, args.db)
    if backend == "bigquery":
        project, dataset = args.project or wcfg["project"], args.dataset or wcfg["dataset"]
        if not project:
            print("BigQuery needs a project: --project or GCP_PROJECT", file=sys.stderr)
            return 2
        path, schema = cr.bigquery_registry_path(project, dataset), dataset
    else:
        db = args.db or wcfg["db_path"]
        path, schema = cr.default_registry_path(db), "main"
    if args.action == "refresh":
        if backend == "bigquery":
            rep = cr.build_bigquery_registry(project, dataset, None, settings, args.mode,
                                             registry_path=path,
                                             maximum_bytes_billed=args.max_bytes_billed,
                                             dry_run=args.dry_run)
        else:
            rep = cr.build_registry(db, None, settings, args.mode, registry_path=path,
                                    dry_run=args.dry_run)
        if rep.dry_run:
            print(f"registry refresh ({args.mode}) DRY RUN, nothing scanned or written: would "
                  f"profile {', '.join(rep.planned) or 'nothing'}")
        else:
            print(f"registry refresh ({args.mode}): profiled {', '.join(rep.profiled) or 'nothing'}"
                  f"; value counts {', '.join(rep.value_counted) or 'none'}"
                  f"; {rep.bytes_processed / 2**20:.1f} MiB processed")
        for label, items in (("up to date", rep.up_to_date), ("over the size gate", rep.oversized),
                             ("FAILED", rep.failed)):
            if items:
                print(f"  {label}: {', '.join(items)}")
        if rep.failed:
            return 1
    st = cr.registry_status(path, schema, settings["max_age_days"])
    if not st["built"]:
        print(f"no registry at {path}: run `registry refresh`")
        return 1
    print(f"{path} (stale after {st['max_age_days']} days)")
    print(f"  {'table':<24}{'rows':>12}{'cols':>6}{'value-counted':>15}{'age (days)':>12}")
    for t in st["tables"]:
        print(f"  {t['table']:<24}{t['rows']:>12,}{t['columns']:>6}{t['value_counted_columns']:>15}"
              f"{t['age_days']:>12}{'  STALE' if t['stale'] else ''}")
    last = st["last_run"]
    print(f"  last refresh: {last['finished_at']} ({last['mode']}), "
          f"{last['bytes_processed'] / 2**20:.1f} MiB" + (f", failed: {last['failed']}" if last["failed"] else "")
          if last else "  last refresh: not recorded")
    return 1 if st["stale_tables"] else 0


def main(argv: list[str] | None = None) -> int:
    from .config import caps_for, load_customer, load_dotenv

    load_dotenv()  # .env settings (e.g. QUERY_PREFLIGHT_LOG_KEY), shell variables win

    parser = argparse.ArgumentParser(prog="query_preflight", description="Query Preflight + Investigator CLI")
    parser.add_argument("--customer", help="path to customer.yaml (default: repo root)")
    parser.add_argument("--db", help="DuckDB file (implies --backend duckdb)")
    # Where queries go: customer.yaml warehouse.backend (BigQuery) unless set here.
    parser.add_argument("--backend", choices=["bigquery", "duckdb"])
    parser.add_argument("--project", help="BigQuery project (default: GCP_PROJECT)")
    parser.add_argument("--dataset", help="BigQuery dataset (default: customer.yaml)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "run"):
        p = sub.add_parser(name)
        p.add_argument("sql")
        p.add_argument("--workload", default="interactive")
        p.add_argument("--llm", choices=["none", "claude"], default="none")
        p.add_argument("--user", help="who runs it (opaque id; budgets are charged to it)")
        if name == "run":
            p.add_argument("--bypass-reason", help="run a cost-blocked query anyway "
                           "(only when customer.yaml guard.allow_bypass is true)")
    p = sub.add_parser("report")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--top", type=int, default=10)
    p = sub.add_parser("investigate")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--workload", default="interactive")
    p = sub.add_parser("accept-normal")
    p.add_argument("shape_id", nargs="?", help="shape id from a report or workload_report "
                   "(a unique prefix of at least 6 characters is enough)")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--by", help="who accepts (opaque id)")
    p.add_argument("--note", help="why this is the new normal")
    p.add_argument("--list", action="store_true", help="show recorded acceptances")
    p = sub.add_parser("investigate-anomalies")
    p.add_argument("--log", default="runs/query_log.jsonl")
    p.add_argument("--traces", default="runs/traces.jsonl")
    p.add_argument("--llm", choices=["none", "claude"], default="none",
                   help="optional summary, within customer.yaml investigator.llm_max_per_day")
    p = sub.add_parser("registry")
    p.add_argument("action", choices=["status", "refresh"])
    # Also accepted after `registry` (as before); SUPPRESS keeps the top-level value.
    p.add_argument("--backend", choices=["duckdb", "bigquery"], default=argparse.SUPPRESS)
    p.add_argument("--project", default=argparse.SUPPRESS)
    p.add_argument("--dataset", default=argparse.SUPPRESS)
    p.add_argument("--mode", choices=["missing", "stale", "all"], default="stale",
                   help="refresh: stale (default) re-profiles only tables older than max_age_days")
    p.add_argument("--dry-run", action="store_true",
                   help="refresh: say which tables would be profiled; scan and write nothing")
    p.add_argument("--max-bytes-billed", type=int, default=2 * 1024**3,
                   help="refresh, BigQuery: per-scan cap; a table over it is reported, not scanned")
    args = parser.parse_args(argv)
    customer = load_customer(args.customer)

    if args.cmd == "registry":
        return _registry(args, customer)

    if args.cmd == "check":
        from .budgets import budget_state
        from .guard import decide
        from .runtime import DEFAULT_LOG, read_log

        backend = _backend(args, customer)
        v = decide(args.sql, args.workload, customer=customer, backend=backend,
                   budget=budget_state(read_log(DEFAULT_LOG), customer, args.user,
                                       args.workload))
        llm = _llm(args, backend)
        if llm is None:
            print(json.dumps(asdict(v), indent=2, default=str))
            return 2 if v.decision == "block" else 0
        # Verdict first: one JSON line now, then one with the model's help
        # (warn and block only) when it is ready.
        from .guard import assist

        print(json.dumps(asdict(v), default=str), flush=True)
        if v.decision in ("warn", "block"):
            assist(args.sql, v, llm, customer=customer, backend=backend)
            print(json.dumps({"assist": {"explanation": v.explanation, "suggestion": v.suggestion,
                                         "assist_notes": v.assist_notes}}, default=str))
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
            **({"assist": {"explanation": out.verdict.explanation,
                           "suggestion": out.verdict.suggestion,
                           "assist_notes": out.verdict.assist_notes}} if args.llm != "none" else {}),
        }, indent=2, default=str))
        return 2 if out.verdict.decision == "block" else 0

    if args.cmd == "report":
        from .ledger import build_report
        from .runtime import read_log

        print(json.dumps(build_report(read_log(args.log), customer, args.top), indent=2))
        return 0

    if args.cmd == "accept-normal":
        from .anomaly import accept_normal, read_acceptances

        if args.list:
            for a in read_acceptances(args.log):
                print(f"{a['ts']}  {a['shape_id']}  {a['flagged_runs']} flagged run(s)  "
                      f"by {a.get('by') or '?'}  {a.get('note') or ''}")
            return 0
        if not args.shape_id:
            parser.error("accept-normal needs a shape id (or --list)")
        try:
            acc = accept_normal(args.log, args.shape_id, by=args.by, note=args.note)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"accepted {acc.shape_id}: {acc.flagged_runs} flagged run(s) up to {acc.ts} now count "
              "as normal; runs flagged after this are still left out of the baseline")
        return 0

    if args.cmd == "investigate-anomalies":
        from .investigator_agent import events_from_log
        from .mcp_server import QueryPreflightService
        from .runtime import read_log

        backend = _backend(args, customer)
        svc = QueryPreflightService(backend, customer, args.log, args.traces, investigations=True,
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
