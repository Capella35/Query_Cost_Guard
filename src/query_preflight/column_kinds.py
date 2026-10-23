"""What a column MEANS: identifier, quantity, date, category or text.

Used by the Guard's SUM/AVG rule (ids are counted, never added). Frugal and
low-risk by design:

1. triage() settles almost every column for free: non-numeric columns can't
   be summed; a clear id name (id, *_id, *_key, ...) is an identifier; a
   clear unit word (price, amount, qty, count, minutes, *_cents, ...) is a
   quantity. On the TheLook e-commerce schema that leaves 1 of 73 columns.
2. Only the rest — numeric columns with no signal (customer, owner) or an
   ambiguous ending (items_no, visits_num, zip_code) — go to a model, once,
   offline, in one batched call with a column limit, from names, types and
   counts only (never values). Answers are cached in a local JSON file and
   asked again only when a column is new or its type changed.
3. A model label is a PROPOSAL: until a person confirms it (reviewed=true),
   an "identifier" label only warns. Confirmed labels block like a clear id
   name. The model never runs on the query path.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .column_registry import is_identifier_name

KINDS = ("identifier", "quantity", "date", "category", "text")
_NUMERIC = ("INT", "BIGINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC", "BIGNUMERIC",
            "SMALLINT", "TINYINT", "HUGEINT", "UBIGINT", "UINTEGER", "USMALLINT", "UTINYINT",
            "UHUGEINT")  # prefixes: INT covers INT64/INTEGER, FLOAT covers FLOAT64
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


# The prompt of the first column-kind eval run (cae02bb). Kept for the record:
# it named some of that eval's hard columns as examples, so its hard-set score
# is partly hinted. Not used any more.
CLASSIFY_SYSTEM_V1 = """You classify database columns by what their values MEAN, \
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


# General prompt: no column name from any test set appears in it.
CLASSIFY_SYSTEM = """You classify database columns by what their values MEAN, \
so a cost guard knows whether SUM or AVG over a column makes sense. You see each \
table's row count and, per column, its name, storage type, distinct count and \
NULL share — never the values. Classify EVERY column you are given into exactly \
one kind:

- identifier: the value names one specific entity or record (its own row, or a \
row of another table). Counted, joined, matched; adding or averaging it means \
nothing. A column can be an identifier without an id-like name.
- quantity: a number where adding or averaging means something — money, \
measures, counts, durations, ages, positions, coordinates. A count can have a \
name that looks like an identifier, and an amount can be nearly unique.
- date: a date or time, however it is stored.
- category: a label from a limited set, used to group.
- text: free-form or naming text, not meant for grouping.

Decide from the name, the type, the table and its other columns, and the counts \
together; no single signal decides. Give a short reason for each."""


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


# ---- Triage: settle columns without a model -------------------------------------

STRONG_ID_NAME = re.compile(r"^id$|(_id|_key|_pk|_uid|_guid)$", re.IGNORECASE)
AMBIGUOUS_ID_NAME = re.compile(r"(_no|_num|_number|_code)$", re.IGNORECASE)
QUANTITY_WORD = re.compile(
    r"(price|cost|amount|total|qty|quantity|count|balance|revenue|sales|tax|fee|discount|"
    r"(^|_)age($|_)|weight|height|length|width|duration|minutes|seconds|hours|days|rate|score|"
    r"latitude|longitude|(^|_)lat($|_)|(^|_)lon($|_)|(^|_)lng($|_)|num_of|number_of|_cents$)",
    re.IGNORECASE)


@dataclass
class Triage:
    kind: Optional[str]  # None = an edge case for the model
    why: str


def triage(name: str, data_type: str) -> Triage:
    """Settle a column without a model when its name and type are clear.
    Only numeric columns matter for SUM/AVG; the rest are settled as such."""
    if data_type.upper().startswith(_TEMPORAL):
        return Triage("date", "date/time type")
    if not is_numeric(data_type):
        return Triage("not_numeric", "SUM/AVG cannot run on it")
    if STRONG_ID_NAME.search(name):
        return Triage("identifier", "id-like name")
    if AMBIGUOUS_ID_NAME.search(name):
        return Triage(None, "ambiguous ending (_no/_num/_number/_code): a count or an id")
    if QUANTITY_WORD.search(name):
        return Triage("quantity", "unit or measure word in the name")
    return Triage(None, "numeric with no clear signal in the name")


# ---- Label store: cached, human-reviewable --------------------------------------

def kinds_path(registry_path: str) -> str:
    """Labels live next to the column registry: x.registry.duckdb -> x.column_kinds.json."""
    p = Path(registry_path)
    stem = p.name.removesuffix(".duckdb").removesuffix(".registry")
    return str(p.with_name(stem + ".column_kinds.json"))


def load_kinds(path: str) -> dict:
    """{"table.column": {kind, type, source, reviewed, reason, labeled_at}}; {} when absent."""
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def save_kinds(path: str, kinds: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(sorted(kinds.items())), indent=2) + "\n")


@dataclass
class ClassifyReport:
    settled: int = 0  # by triage, no model
    cached: int = 0  # edge cases already labeled (same type)
    asked: list = None  # edge cases sent to the model this run
    over_limit: list = None  # edge cases left for the next run (column limit)
    model_calls: int = 0


def classify_edge_cases(entries, path: str, llm=None, max_columns: int = 50) -> ClassifyReport:
    """Label the edge cases among registry `entries` (RegistryEntry-like:
    table_name, column_name, data_type, total_rows, distinct_count,
    null_count). Triage first; only unlabeled edge cases (new, or type
    changed) go to `llm.classify_columns`, in ONE call, at most
    `max_columns`. Human-confirmed labels are never overwritten. With
    llm=None, nothing is asked (a dry run that lists the edge cases)."""
    kinds = load_kinds(path)
    rep = ClassifyReport(asked=[], over_limit=[])
    todo = []
    for e in entries:
        key = f"{e.table_name}.{e.column_name}"
        if triage(e.column_name, e.data_type).kind is not None:
            rep.settled += 1
            continue
        old = kinds.get(key)
        if old and (old.get("reviewed") or old.get("type") == e.data_type):
            rep.cached += 1
            continue
        todo.append(e)
    batch, rep.over_limit = todo[:max_columns], [f"{e.table_name}.{e.column_name}" for e in todo[max_columns:]]
    rep.asked = [f"{e.table_name}.{e.column_name}" for e in batch]
    if not batch or llm is None:
        return rep
    tables: dict = {}
    for e in batch:  # the model sees the edge cases with their table's size
        spec = tables.setdefault(e.table_name, {"rows": int(e.total_rows), "columns": []})
        spec["columns"].append({"name": e.column_name, "type": e.data_type,
                                "distinct": int(e.distinct_count),
                                "null_share": e.null_count / e.total_rows if e.total_rows else 0.0})
    calls_before = getattr(llm, "calls", 0)
    answer = llm.classify_columns(tables)
    rep.model_calls = getattr(llm, "calls", 0) - calls_before
    now = datetime.now(timezone.utc).isoformat()
    for e in batch:
        got = answer.get((e.table_name, e.column_name))
        if got is None:
            continue
        kinds[f"{e.table_name}.{e.column_name}"] = {
            "kind": got["kind"], "type": e.data_type, "source": getattr(llm, "model", "model"),
            "reviewed": False, "reason": got["reason"], "labeled_at": now}
    save_kinds(path, kinds)
    return rep


def confirm(path: str, column: str, kind: str, reviewer: str = "human") -> None:
    """A person confirms (or corrects) a label: from now on it is trusted."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    kinds = load_kinds(path)
    entry = kinds.get(column, {})
    entry.update(kind=kind, reviewed=True, reviewed_by=reviewer,
                 reviewed_at=datetime.now(timezone.utc).isoformat())
    kinds[column] = entry
    save_kinds(path, kinds)
