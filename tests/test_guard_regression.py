"""The 11 inline cases as regression tests (synthetic warehouse). These were
written alongside the rules, so passing them is expected, not evidence — the
headline eval is eval/run_eval.py over the 30 TheLook queries."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from eval_config import load_eval_customer  # noqa: E402
from regression_cases import CASES  # noqa: E402

from cost_guard.guard import decide  # noqa: E402
from cost_guard.tracing import Tracer  # noqa: E402


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_regression_case(case, synthetic_wh):
    v = decide(case["sql"], customer=load_eval_customer(), backend=synthetic_wh, tracer=Tracer())
    assert v.decision == case["expected"], f"{case['why']}: {v.reason}"
    assert v.skipped_rules == [] or case["expected"] == "block"
