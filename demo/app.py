"""Cost Guard + Investigator — interactive demo UI.

Screen-share friendly: type (or pick) a query, see its cost estimate live and
the Guard's verdict; then watch the Investigator learn a threshold from history.

Run:  streamlit run demo/app.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "demo"))

import streamlit as st

from warehouse import build_synthetic_warehouse
from cost_guard.config import caps_for, load_customer
from cost_guard.guard import decide
from cost_guard.investigator import THRESHOLD_HEADROOM, propose_threshold
from cost_guard.thresholds import BaselineStore, fingerprint_query

st.set_page_config(page_title="Cost Guard + Investigator", layout="wide")
st.title("Cost Guard + Investigator")
st.caption(
    "An automated cost-control loop for analytics queries — "
    "the Guard checks before execution, the Investigator learns after."
)


PRESETS = {
    "SELECT * on a big read": "SELECT * FROM events WHERE created_at >= '2023-06-01'",
    "Accidental cartesian join": "SELECT * FROM users, orders",
    "Repeated subquery": (
        "SELECT * FROM\n"
        "  (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) a\n"
        "  JOIN (SELECT user_id, COUNT(*) AS c FROM orders GROUP BY user_id) b\n"
        "  USING (user_id) ORDER BY user_id LIMIT 50"
    ),
    "Revenue dashboard (customer query)": (
        "SELECT p.category, SUM(oi.quantity * p.retail_price_cents) / 100.0 AS revenue_usd\n"
        "FROM orders o\n"
        "JOIN order_items oi ON oi.order_id = o.order_id\n"
        "JOIN products p ON p.id = oi.product_id\n"
        "GROUP BY p.category\n"
        "ORDER BY revenue_usd DESC\n"
        "LIMIT 20"
    ),
}

VERDICT_COLORS = {"allow": "green", "warn": "orange", "block": "red", "rewrite": "blue"}


@st.cache_resource
def get_warehouse():
    return build_synthetic_warehouse()


wh = get_warehouse()

tab_guard, tab_loop = st.tabs(
    ["Guard — before execution", "Investigator — after execution"]
)

with tab_guard:
    st.subheader("Try a query")
    if "query" not in st.session_state:
        st.session_state.query = PRESETS["SELECT * on a big read"]

    cols = st.columns(len(PRESETS))
    for (label, q), col in zip(PRESETS.items(), cols):
        if col.button(label, use_container_width=True):
            st.session_state.query = q

    sql = st.text_area("SQL", key="query", height=150)

    # Live estimate on every render — cheap (EXPLAIN, nothing executes).
    try:
        est = wh.estimate(sql)
        st.caption(
            f"Estimated rows processed: **{est.rows_processed:,}** "
            f"({est.detail}) — updates as you type"
        )
    except Exception:
        st.caption("Estimated scan: unavailable for this query")

    if st.button("Run Guard", type="primary", key="run_guard"):
        verdict = decide(sql, backend=wh)
        color = VERDICT_COLORS[verdict.decision]
        st.markdown(f"### Verdict: :{color}[{verdict.decision.upper()}]")
        st.write(f"**Reason:** {verdict.reason}")
        if verdict.findings:
            st.write("**Findings:**")
            for f in verdict.findings:
                st.write(f"- {f}")

        if verdict.decision == "rewrite" and verdict.rewritten_sql:
            st.divider()
            st.subheader("Suggested rewrite — verified, same results")
            st.code(verdict.rewritten_sql, language="sql")
            st.caption(
                "The Guard generated this rewrite and verified it by running both "
                "queries on the same sampled inputs and comparing result hashes "
                f"({verdict.verification}). Your original query still runs — this is "
                "the alternative, offered only because it verified."
            )

with tab_loop:
    st.subheader("What the Investigator learned overnight")
    st.write(
        "Same query shape, different literals — the Investigator groups them by "
        "fingerprint and learns what 'normal' looks like for each shape."
    )
    st.code(fingerprint_query("SELECT name FROM users WHERE id = 42"), language="text")

    if st.button("Analyze query history", type="primary", key="analyze_history"):
        store = BaselineStore(min_runs_for_adaptive=20)
        random.seed(7)
        q = "SELECT name FROM users WHERE id = 42"
        for _ in range(40):
            store.record(q, 50_000, max(0.05, random.gauss(0.35, 0.05)))
        store.record(q, 50_000, 3.1)  # the bad old days
        store.record(q, 50_000, 4.2)
        stats = store.baseline(q)

        c1, c2, c3 = st.columns(3)
        c1.metric("Runs observed", stats.run_count)
        c2.metric("Median runtime", f"{stats.median_secs:.2f}s")
        c3.metric("p95 runtime", f"{stats.p95_secs:.2f}s")

        buckets = {"< 0.5s": 0, "0.5–1s": 0, "1–2s": 0, "2–3s": 0, "3s+": 0}
        for _, s in stats.runs:
            if s < 0.5:
                buckets["< 0.5s"] += 1
            elif s < 1:
                buckets["0.5–1s"] += 1
            elif s < 2:
                buckets["1–2s"] += 1
            elif s < 3:
                buckets["2–3s"] += 1
            else:
                buckets["3s+"] += 1
        st.bar_chart(buckets)

        hard_cap = caps_for(load_customer(), "interactive")["max_runtime_sec"]
        new_threshold = propose_threshold(stats, hard_cap, hard_cap)
        target = stats.p95_secs * THRESHOLD_HEADROOM
        st.info(
            f"**Proposal:** kill this query shape above **{new_threshold:g}s** "
            f"(previous ceiling: the {hard_cap}s interactive hard cap).  \n"
            f"Evidence: {stats.run_count} runs, p95 {stats.p95_secs:.2f}s, so the target is "
            f"{target:.2f}s — but one update may move at most 25%, so the threshold "
            "ratchets down over several shadow-tested updates.  \n"
            "Next: deterministic safety gates → optional LLM judge → shadow trial → enforce, "
            "with auto-rollback if the block rate jumps."
        )

st.divider()
st.caption(
    "Demo data is synthetic and local (DuckDB). Cost here is modeled from EXPLAIN "
    "row estimates, not billed bytes — every dollar figure stays labeled as modeled."
)
