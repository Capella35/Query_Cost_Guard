"""Canonical result hashing for equivalence checks (F1.4).

Rows never leave the backend — only this hash does. Canonical form:
- floats (and decimals) rounded to the comparison tolerance; NaN normalized,
- NULL is one sentinel everywhere, so NULLs compare equal to NULLs only,
- row order ignored unless the query's ORDER BY makes it part of the answer.

Known limitation: rounding to a tolerance can split two values that straddle
a rounding boundary (0.4999996 vs 0.5000004 at 1e-6). Rare, and it fails
safe (a false "differs", never a false "same").
"""
from __future__ import annotations

import hashlib
import math
from decimal import Decimal

_NULL = "\x00NULL"


def _digits(float_tolerance: float) -> int:
    return max(0, round(-math.log10(float_tolerance))) if float_tolerance > 0 else 12


def _canon_value(v, digits: int):
    if v is None:
        return _NULL
    if isinstance(v, bool):
        return v
    if isinstance(v, (float, Decimal)):
        f = float(v)
        if math.isnan(f):
            return "NaN"
        r = round(f, digits)
        return 0.0 if r == 0 else r  # -0.0 == 0.0
    return v


def canonical_rows(rows, ordered: bool, float_tolerance: float = 1e-6) -> list[tuple]:
    digits = _digits(float_tolerance)
    canon = [tuple(_canon_value(v, digits) for v in row) for row in rows]
    if not ordered:
        canon.sort(key=repr)
    return canon


def result_hash(rows, ordered: bool, float_tolerance: float = 1e-6) -> str:
    return hashlib.sha256(
        repr(canonical_rows(rows, ordered, float_tolerance)).encode()
    ).hexdigest()
