"""Customer config: customer.yaml is the single source of caps and table facts.

Hard caps per workload class come from here and nowhere else — no module
hard-codes a cap.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "customer.yaml"


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
