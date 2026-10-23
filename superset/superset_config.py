"""Apache Superset settings for the local Query Preflight environment
(scripts/superset.sh sets SUPERSET_CONFIG_PATH to this file).

Local only: SQLite for Superset's own metadata under .superset_home/, the
dev server on 127.0.0.1. The BigQuery connection is added by the start
script from GCP_PROJECT in .env, with Google's application-default login.
"""
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent / ".superset_home"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from query_preflight_hook import mutator  # noqa: E402

SECRET_KEY = (HOME / "secret_key").read_text().strip()
SQLALCHEMY_DATABASE_URI = f"sqlite:///{HOME / 'superset.db'}"

# Query Preflight checks every query before Superset sends it; a block stops it.
SQL_QUERY_MUTATOR = mutator
# False (Superset's default): SQL Lab (sql_lab.py) and charts call the mutator
# that way, SQL Lab already one statement at a time; True would skip both.
MUTATE_AFTER_SPLIT = False

# Run SQL Lab queries in the web process (no Celery worker on a laptop).
SQLLAB_ASYNC_TIME_LIMIT_SEC = 300
SQLLAB_TIMEOUT = 120  # the Guard's check plus the query
SUPERSET_WEBSERVER_TIMEOUT = 120
FEATURE_FLAGS = {"ESTIMATE_QUERY_COST": True}
