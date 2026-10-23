"""Table metadata: size, partitioning, clustering. Feeds the static rules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TableMetadata:
    table: str
    num_bytes: Optional[int]  # None on DuckDB (no per-table byte size exposed)
    num_rows: Optional[int] = None
    partitioned_by: Optional[str] = None
    clustered_by: list[str] = field(default_factory=list)
    column_count: Optional[int] = None  # "wide table" = many columns


def get_table_metadata(table: str, backend=None, customer: Optional[dict] = None) -> TableMetadata:
    """Metadata via the backend; the partition column comes from
    customer.yaml (partitioned_tables) on DuckDB, from the table on BigQuery."""
    from ..config import load_customer, partition_column

    if backend is None:
        raise ValueError("get_table_metadata needs a WarehouseBackend")
    md = backend.table_metadata(table)
    customer = customer if customer is not None else load_customer()
    return TableMetadata(
        table=table,
        num_bytes=md.get("num_bytes"),
        num_rows=md.get("num_rows"),
        partitioned_by=md.get("partitioned_by") or partition_column(customer, table),
        clustered_by=list(md.get("clustered_by") or []),
        column_count=md.get("column_count"),
    )
