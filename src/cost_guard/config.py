"""Customer config: customer.yaml is the single source of caps and table facts.

Hard caps per workload class come from here and nowhere else — no module
hard-codes a cap.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "customer.yaml"
DOTENV_PATH = DEFAULT_PATH.with_name(".env")


def load_dotenv(path: str | Path | None = None) -> list[str]:
    """Set variables from the repo's .env (KEY=value lines; # comments).
    A variable already set in the environment wins, and an empty value sets
    nothing. Returns the names it set — never the values. No dependency."""
    import os

    p = Path(path) if path is not None else DOTENV_PATH
    if not p.exists():
        return []
    set_now = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            set_now.append(key)
    return set_now


BACKENDS = ("bigquery", "duckdb")


def warehouse_settings(customer: Optional[dict]) -> dict:
    """customer.yaml `warehouse`: the backend every entry point uses, and its
    connection (BigQuery project from `project` or GCP_PROJECT; dataset;
    DuckDB file)."""
    import os

    w = (customer or {}).get("warehouse") or {}
    bq = w.get("bigquery") or {}
    return {"backend": w.get("backend", "bigquery"),
            "project": bq.get("project") or os.environ.get("GCP_PROJECT"),
            "dataset": bq.get("dataset") or "cost_guard_thelook",
            "db_path": (w.get("duckdb") or {}).get("db_path") or "data/thelook.duckdb"}


def choose_backend(customer: Optional[dict], backend: Optional[str] = None,
                   db: Optional[str] = None) -> str:
    """Which warehouse an entry point opens: an explicit --backend; else
    --db alone means DuckDB (tests, the demo); else COST_GUARD_BACKEND; else
    customer.yaml warehouse.backend (BigQuery, the target). One rule for the
    CLI, the MCP server and the registry build, so none silently defaults to
    the local DuckDB copy."""
    import os

    choice = backend or ("duckdb" if db else None) or os.environ.get("COST_GUARD_BACKEND") \
        or warehouse_settings(customer)["backend"]
    if choice not in BACKENDS:
        raise SystemExit(f"unknown backend {choice!r} (expected one of {', '.join(BACKENDS)})")
    return choice


def make_backend(customer: Optional[dict], backend: Optional[str] = None,
                 project: Optional[str] = None, dataset: Optional[str] = None,
                 db: Optional[str] = None):
    """The warehouse backend per choose_backend, connected as configured."""
    cfg = warehouse_settings(customer)
    if choose_backend(customer, backend, db) == "bigquery":
        from .backends.bigquery_backend import BigQueryWarehouse

        project = project or cfg["project"]
        if not project:
            raise SystemExit("BigQuery needs a project: --project, GCP_PROJECT (.env), or "
                             "customer.yaml warehouse.bigquery.project")
        return BigQueryWarehouse(project, dataset or cfg["dataset"])
    from .backends.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(db or cfg["db_path"])


def load_customer(path: str | Path | None = None) -> dict:
    """Load customer.yaml (repo root by default)."""
    import yaml

    p = Path(path) if path is not None else DEFAULT_PATH
    with p.open() as f:
        cfg = yaml.safe_load(f) or {}
    if "workload_classes" not in cfg:
        raise ValueError(f"{p}: missing 'workload_classes'")
    return cfg


def caps_for(customer: dict, workload_class: str) -> dict:
    """Hard caps for one workload class. Unknown class fails loudly — a typo
    must not silently fall back to looser caps."""
    classes = customer.get("workload_classes") or {}
    if workload_class not in classes:
        raise KeyError(
            f"unknown workload class {workload_class!r}; known: {sorted(classes)}"
        )
    return dict(classes[workload_class])


def partition_column(customer: Optional[dict], table: str) -> Optional[str]:
    return ((customer or {}).get("partitioned_tables") or {}).get(table)


VOLUME_DEFAULTS = {"window_hours": 24, "min_runs": 20, "warn_usd_per_window": 5.0}


def volume_settings(customer: Optional[dict]) -> dict:
    """customer.yaml `volume` (the volume rule), over the defaults."""
    return {**VOLUME_DEFAULTS, **((customer or {}).get("volume") or {})}


def bypass_allowed(customer: Optional[dict]) -> bool:
    return bool(((customer or {}).get("guard") or {}).get("allow_bypass", False))


def no_looser_than(customer: dict, requested: str, ceiling: str) -> bool:
    """True when every cap of `requested` is at most the same cap of `ceiling`
    — a caller may pick an equal or stricter class, never a looser one. A cap
    the ceiling sets and the requested class leaves out counts as looser."""
    req, top = caps_for(customer, requested), caps_for(customer, ceiling)
    for k, limit in top.items():
        if limit is None:
            continue
        mine = req.get(k)
        if mine is None or mine > limit:
            return False
    return True
