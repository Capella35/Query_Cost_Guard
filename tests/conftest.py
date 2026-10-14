"""Make the src/ package importable for all tests; shared warehouse fixtures."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "demo"))


@pytest.fixture(scope="session")
def synthetic_wh(tmp_path_factory):
    """The demo's synthetic warehouse (users 50k, orders 1M, events 1M...)."""
    pytest.importorskip("duckdb")
    from warehouse import build_synthetic_warehouse

    return build_synthetic_warehouse(str(tmp_path_factory.mktemp("wh") / "synthetic.duckdb"))


@pytest.fixture(scope="session")
def thelook_wh():
    """Real TheLook data (scripts/load_thelook_duckdb.py); skipped when absent."""
    pytest.importorskip("duckdb")
    db = ROOT / "data" / "thelook.duckdb"
    if not db.exists():
        pytest.skip("data/thelook.duckdb not built — see README quickstart")
    from cost_guard.backends.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(str(db))
