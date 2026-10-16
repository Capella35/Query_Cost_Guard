"""What a column MEANS: identifier, quantity, date, category or text.

The Guard blocks SUM/AVG over identifiers and today decides "identifier" by
name (column_registry.IDENTIFIER_NAME). A model can classify columns once,
offline, from name, type and counts — never data values — into a label a
human reviews; the Guard would then read the label instead of guessing from
the name. Whether that is better is measured first (eval/column_kinds.yaml,
eval/run_column_kinds.py); nothing here is wired into the Guard yet.
"""
from __future__ import annotations

from typing import Optional

from .column_registry import is_identifier_name

KINDS = ("identifier", "quantity", "date", "category", "text")
_NUMERIC = ("INT", "FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC", "BIGNUMERIC", "SMALLINT",
            "TINYINT", "HUGEINT", "UBIGINT", "UINTEGER")
_TEMPORAL = ("DATE", "TIMESTAMP", "DATETIME", "TIME")


def is_numeric(data_type: str) -> bool:
    return data_type.upper().startswith(_NUMERIC)


def baseline_kind(name: str, data_type: str, rows: int, distinct: int) -> str:
    """Today's logic, as fixed in eval/column_kinds.yaml: the name pattern for
    identifiers, then the storage type."""
    if is_identifier_name(name):
        return "identifier"
    if data_type.upper().startswith(_TEMPORAL):
        return "date"
    if is_numeric(data_type):
        return "quantity"
    return "category" if rows and distinct <= 0.5 * rows else "text"


CLASSIFY_SYSTEM = """You classify database columns by what their values MEAN, \
so a cost guard knows which aggregates make sense. You see each table's row \
count and, per column, its name, storage type, distinct count and NULL share — \
never the values. Classify EVERY column into exactly one kind:

- identifier: names ONE specific entity or record — keys, foreign keys (even \
without an "_id" suffix, e.g. a column "customer" holding customer numbers), \
session ids, SKUs, emails, IP addresses, phone numbers, account numbers. \
Counted, joined, matched; never added or averaged.
- quantity: a number where arithmetic or an average means something — prices, \
costs, amounts (also in cents, even when nearly every value is distinct), \
ages, counts (also when named like "items_no" or "visits_num"), durations, \
positions in a sequence, coordinates.
- date: a date or time, however stored (TIMESTAMP, a date string, a yyyymmdd \
integer).
- category: a label from a limited set used to group — status, type, gender, \
country, state, city, brand, department, a small dimension's name.
- text: free-form or naming text not meant for grouping — person names, product \
names, street addresses, URIs, subjects.

Use the name, the type, the table it sits in and its neighbours, and the counts \
together; a near-unique integer is not automatically an identifier. Give a short \
reason for each."""


def describe_tables(tables: dict) -> str:
    """The prompt body: per table, rows and each column's name, type,
    distinct count and NULL share (from eval/column_kinds.yaml's shape:
    {table: {rows, columns: [{name, type, distinct, null_share}, ...]}})."""
    lines = []
    for t, spec in tables.items():
        lines.append(f"table {t} ({spec['rows']:,} rows):")
        for c in spec["columns"]:
            lines.append(f"  - {c['name']} {c['type']}, {c['distinct']:,} distinct, "
                         f"{c['null_share']:.1%} NULL")
    return "\n".join(lines)


def score(tables: dict, predicted: dict[tuple[str, str], Optional[str]]) -> dict:
    """Metrics fixed in eval/column_kinds.yaml. `predicted` maps (table,
    column) -> kind (None = missing from the answer, counted as wrong)."""
    false_ids, missed_ids, right, n, wrong = [], [], 0, 0, []
    for t, spec in tables.items():
        for c in spec["columns"]:
            n += 1
            ok = {c["kind"], c.get("also_ok")} - {None}
            got = predicted.get((t, c["name"]))
            if got in ok:
                right += 1
            else:
                wrong.append(f"{t}.{c['name']}: {c['kind']} -> {got}")
            if is_numeric(c["type"]):
                if got == "identifier" and "quantity" in ok:  # would block a valid SUM
                    false_ids.append(f"{t}.{c['name']}")
                if c["kind"] == "identifier" and got != "identifier":
                    missed_ids.append(f"{t}.{c['name']}")
    return {"accuracy": f"{right}/{n}", "false_ids": false_ids, "missed_ids": missed_ids,
            "wrong": wrong}
