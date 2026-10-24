"""Cost scorecard arithmetic (eval/cost_scorecard.py), on made-up cases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from cost_scorecard import VERIFY_BYTES, score_set  # noqa: E402

MiB = 1024**2


def test_each_verdict_lands_in_its_column():
    cases = [
        {"id": "a", "role": "bad", "got": "block"},
        {"id": "b", "role": "bad", "got": "warn"},
        {"id": "c", "role": "bad", "got": "allow"},
        {"id": "d", "role": "bad", "got": "rewrite", "rewritten_sql": "d2"},
        {"id": "e", "role": "good", "got": "block"},
        {"id": "f", "role": "good", "got": "warn", "findings": ["llm rewrite failed verification"]},
        {"id": "g", "role": "good", "got": "allow"},
    ]
    sizes = {"a": 100, "b": 40, "c": 30, "d": 50, "d2": 20, "e": 7, "f": 5, "g": 1}
    sc = score_set(cases, {c["id"]: c["id"] for c in cases}, lambda q: sizes[q] * MiB)
    b, n = sc["bytes"], sc["counts"]
    assert (b["stopped"], b["flagged"], b["missed"]) == (100 * MiB, 40 * MiB, 30 * MiB)
    assert b["bad_total"] == 220 * MiB and b["rewrite_saved"] == 30 * MiB and b["fixed"] == 50 * MiB
    assert (n["false_blocks"], n["false_warns"], n["good"]) == (1, 1, 3)
    assert n["verifications"] == 2 and b["guard_verify"] == 2 * VERIFY_BYTES
    assert b["false_block_bytes"] == 7 * MiB
