"""eval/run_replay_bq.py: traffic shape and the per-size-signal evaluation,
without BigQuery (sizes are filled in by hand)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from run_replay_bq import evaluate, gen_traffic, separation  # noqa: E402

from query_preflight.thresholds import fingerprint_query  # noqa: E402


def _filled(wide_factor_a: float, wide_factor_b: float):
    runs = gen_traffic()
    for r in runs:
        wide = r["label"] == "bad" and "users u" not in r["sql"]
        r["decision"] = "block" if "users u, orders o" in r["sql"] else "allow"
        r["a"] = int(1000 * (wide_factor_a if wide else 1))
        r["b"] = int(1000 * (wide_factor_b if wide else 1))
    return runs


def test_traffic_shape():
    runs = gen_traffic()
    assert len(runs) == 96
    shapes = {fingerprint_query(r["sql"]) for r in runs if "FROM orders WHERE" in r["sql"]}
    assert len(shapes) == 1  # narrow and wide windows share one fingerprint
    assert sum(r["label"] == "bad" for r in runs) == 18  # 6 + 6 wide windows, 6 cartesian


def test_size_signal_decides_the_catch():
    runs = _filled(wide_factor_a=2.0, wide_factor_b=100.0)
    weak, exact = evaluate(runs, "a"), evaluate(runs, "b")
    assert exact["catch_static_plus_adaptive"] > weak["catch_static_plus_adaptive"]
    assert exact["catch_static_plus_adaptive"] == 1.0 and exact["false_positive_rate"] == 0.0
    sep = {s["shape"]: s["ratio"] for s in separation(runs, "b")}
    assert sep == {"orders window": 100.0, "order_items monthly": 100.0}
