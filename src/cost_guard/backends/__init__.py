"""Warehouse backend seam: DuckDB (primary, local) + BigQuery (stretch goal).

The Guard/Investigator logic never touches a warehouse directly — it goes
through WarehouseBackend, so the local-first decision changes only the adapter.
"""

from .base import PlanEstimate, WarehouseBackend
from .duckdb_backend import DuckDBWarehouse
from .bigquery_backend import BigQueryWarehouse

__all__ = ["PlanEstimate", "WarehouseBackend", "DuckDBWarehouse", "BigQueryWarehouse"]
