"""Smoke test for the Streamlit demo UI (demo/app.py).

Drives the app headlessly with streamlit's AppTest: loads it, runs the Guard
on the default query, and checks a WARN verdict renders with no exceptions.
"""
from pathlib import Path

import pytest

streamlit = pytest.importorskip("streamlit")

ROOT = Path(__file__).resolve().parent.parent


def test_app_guard_warn_flow():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "demo" / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception

    # Default query is the SELECT * preset -> Run Guard -> WARN verdict.
    at.button("run_guard").click().run()
    assert not at.exception, at.exception
    texts = [m.value for m in at.markdown]
    assert any("WARN" in t for t in texts), texts
    assert any("wide table" in t for t in texts), texts


def test_app_investigator_flow():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "demo" / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception

    at.button("analyze_history").click().run()
    assert not at.exception, at.exception
    infos = [e.value for e in at.info]
    assert any("Proposal" in v for v in infos), infos
    assert any("p95" in v for v in infos), infos
