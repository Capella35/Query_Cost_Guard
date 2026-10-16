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


def bypass_allowed(customer: Optional[dict]) -> bool:
    return bool(((customer or {}).get("guard") or {}).get("allow_bypass", False))
