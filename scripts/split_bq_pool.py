#!/usr/bin/env python3
"""Split eval/bq_pool.yaml into validation and test: seeded, stratified by label.

Per label, the ids in file order are shuffled with random.Random(SEED); the
first half (rounded up) is `split: validation`, the rest `split: test`. The
seed was fixed before any Guard verdict on the pool was seen, and the split is
committed with the pool (pre-registration). Rerunning gives the same split.

Run:  .venv/bin/python scripts/split_bq_pool.py
"""
from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "eval" / "bq_pool.yaml"
SEED = 20261018


def main() -> None:
    text = POOL.read_text()
    head, sep, body = text.partition("\nqueries:\n")
    queries = yaml.safe_load(sep + body)["queries"]
    by_label = defaultdict(list)
    for q in queries:
        by_label[q["guard_action_expected"]].append(q["id"])
    rng = random.Random(SEED)
    split = {}
    for label in sorted(by_label):
        ids = by_label[label]
        rng.shuffle(ids)
        half = (len(ids) + 1) // 2
        split.update({i: "validation" for i in ids[:half]})
        split.update({i: "test" for i in ids[half:]})
    out = [{"id": q["id"], "split": split[q["id"]],
            **{k: v for k, v in q.items() if k not in ("id", "split")}} for q in queries]
    POOL.write_text(head + sep + yaml.safe_dump(out, sort_keys=False, width=110, allow_unicode=True))
    for name in ("validation", "test"):
        part = [q for q in queries if split[q["id"]] == name]
        labels = defaultdict(int)
        for q in part:
            labels[q["guard_action_expected"]] += 1
        owner = sum(q["author"] == "owner" for q in part)
        print(f"{name}: {len(part)} ({dict(sorted(labels.items()))}, {owner} by the owner)")


if __name__ == "__main__":
    main()
