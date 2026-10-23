"""The customer config the evals run under: customer.yaml with the workload
caps replaced by eval_caps.yaml (the caps the labels were written under)."""
from __future__ import annotations

from pathlib import Path

import yaml

from query_preflight.config import load_customer

EVAL_CAPS = Path(__file__).resolve().parent / "eval_caps.yaml"


def load_eval_customer(caps: Path = EVAL_CAPS) -> dict:
    cfg = load_customer()
    cfg["workload_classes"] = yaml.safe_load(Path(caps).read_text())["workload_classes"]
    return cfg
