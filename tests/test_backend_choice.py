"""Every entry point opens the configured warehouse (BigQuery, the target),
never the local DuckDB copy by default (2026-10-18: the CLI's check/run and
the MCP server used to default to DuckDB)."""
import argparse

import pytest

from query_preflight import config

BQ = {"warehouse": {"backend": "bigquery", "bigquery": {"project": "p", "dataset": "ds"},
                    "duckdb": {"db_path": "data/thelook.duckdb"}}}


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("QUERY_PREFLIGHT_BACKEND", raising=False)


def test_choose_backend_order(monkeypatch):
    assert config.choose_backend(BQ) == "bigquery"
    assert config.choose_backend(BQ, db="x.duckdb") == "duckdb"  # --db alone: tests, the demo
    assert config.choose_backend(BQ, backend="bigquery", db="x.duckdb") == "bigquery"  # explicit wins
    monkeypatch.setenv("QUERY_PREFLIGHT_BACKEND", "duckdb")
    assert config.choose_backend(BQ) == "duckdb"
    assert config.choose_backend({}) == "duckdb"  # the env var still decides
    monkeypatch.delenv("QUERY_PREFLIGHT_BACKEND")
    assert config.choose_backend({}) == "bigquery"  # no warehouse section: the target
    with pytest.raises(SystemExit, match="unknown backend"):
        config.choose_backend(BQ, backend="sqlite")


def test_repo_config_targets_bigquery():
    assert config.warehouse_settings(config.load_customer())["backend"] == "bigquery"


class FakeBQ:
    name = "bigquery"

    def __init__(self, project, dataset):
        self.project, self.dataset = project, dataset


def _args(**kw):
    return argparse.Namespace(**{"backend": None, "project": None, "dataset": None, "db": None, **kw})


@pytest.mark.parametrize("module", ["query_preflight.__main__", "query_preflight.mcp_server"])
def test_cli_and_mcp_server_open_bigquery_by_default(module, monkeypatch):
    import importlib

    import query_preflight.backends.bigquery_backend as bqb

    monkeypatch.setattr(bqb, "BigQueryWarehouse", FakeBQ)
    mod = importlib.import_module(module)
    wh = mod._backend(_args(), BQ)
    assert isinstance(wh, FakeBQ) and (wh.project, wh.dataset) == ("p", "ds")
    wh = mod._backend(_args(project="q", dataset="other"), BQ)
    assert (wh.project, wh.dataset) == ("q", "other")


def test_bigquery_without_a_project_fails_loudly(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    cfg = {"warehouse": {"backend": "bigquery", "bigquery": {"project": None}}}
    with pytest.raises(SystemExit, match="BigQuery needs a project"):
        config.make_backend(cfg)


def test_run_eval_has_no_default_suite():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, str(root / "eval" / "run_eval.py")],
                         capture_output=True, text=True, cwd=root)
    assert out.returncode != 0 and "--suite" in out.stderr
