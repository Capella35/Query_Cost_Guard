"""Synthetic e-commerce warehouse builder, shared by the CLI demo and the UI.

Builds users (50k), orders (1M), order_items (400k) and a wide events table
(1M rows, 12 columns) in a local DuckDB file, plus its local column registry file.
Deterministic, ~3 seconds.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cost_guard.backends.duckdb_backend import DuckDBWarehouse
from cost_guard.column_registry import build_registry, default_registry_path, registry_settings
from cost_guard.config import load_customer


def build_synthetic_warehouse(db_path: str = "/tmp/cost_guard_demo.duckdb") -> DuckDBWarehouse:
    import duckdb

    Path(db_path).unlink(missing_ok=True)
    Path(default_registry_path(db_path)).unlink(missing_ok=True)
    con = duckdb.connect(db_path)
    con.execute(
        """CREATE TABLE users AS SELECT range AS id,
           'user_' || range AS name, 'user' || range || '@example.com' AS email,
           (range % 50) AS age_band,
           CASE WHEN range % 2 = 0 THEN 'US' ELSE 'UK' END AS country
           FROM range(50000)"""
    )
    con.execute(
        """CREATE TABLE orders AS SELECT range AS order_id,
           range % 50000 + 1 AS user_id,
           CASE WHEN range % 10 = 0 THEN 'cancelled' ELSE 'complete' END AS status,
           DATE '2023-01-01' + (range % 730)::INTEGER AS created_at,
           10 + (range % 490) AS amount_cents
           FROM range(1000000)"""
    )
    con.execute(
        """CREATE TABLE order_items AS SELECT range AS id,
           range % 1000000 + 1 AS order_id, range % 20000 + 1 AS product_id,
           range % 5 + 1 AS quantity FROM range(400000)"""
    )
    con.execute(
        """CREATE TABLE products AS SELECT range AS id,
           'Product ' || range AS name,
           CASE WHEN range % 5 = 0 THEN 'Accessories'
                WHEN range % 5 = 1 THEN 'Tops'
                WHEN range % 5 = 2 THEN 'Bottoms'
                WHEN range % 5 = 3 THEN 'Shoes' ELSE 'Outerwear' END AS category,
           'Brand' || (range % 50) AS brand,
           CASE WHEN range % 2 = 0 THEN 'Men' ELSE 'Women' END AS department,
           1000 + (range % 20000) AS retail_price_cents
           FROM range(20000)"""
    )
    con.execute(
        """CREATE TABLE events AS SELECT range AS id,
           range % 50000 + 1 AS user_id, range AS session_id,
           DATE '2023-01-01' + (range % 730)::INTEGER AS created_at,
           '192.0.2.' || (range % 255 + 1) AS ip_address,
           'City' || (range % 100) AS city,
           CASE WHEN range % 3 = 0 THEN 'purchase'
                WHEN range % 3 = 1 THEN 'view' ELSE 'cart' END AS event_type,
           'https://example.com/p/' || (range % 20000) AS uri,
           CASE WHEN range % 2 = 0 THEN 'chrome' ELSE 'safari' END AS browser,
           'organic' AS traffic_source, range % 1000 AS sequence_number,
           'v' || (range % 5) AS app_version
           FROM range(1000000)"""
    )
    con.close()
    build_registry(db_path, settings=registry_settings(load_customer()), mode="all")  # one-time column profiling into <db>.registry.duckdb
    return DuckDBWarehouse(db_path=db_path)
