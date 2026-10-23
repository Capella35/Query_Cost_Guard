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
    from query_preflight.backends.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(str(db))


@pytest.fixture(autouse=True)
def _fresh_verification_memory():
    """Verification results are remembered per process (guard._verify); each
    test starts without them, so no test sees another's outcome."""
    from query_preflight.guard import clear_verification_memory

    clear_verification_memory()
    yield
