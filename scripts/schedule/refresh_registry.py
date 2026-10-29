"""Monthly column-registry refresh (DESIGN.md §11), run by launchd
(scripts/schedule/install_launchd.sh): the 1st of each month at 09:00; a Mac
asleep then runs it at wake. Output is appended to runs/registry_refresh.log.

DRY_RUN = True (the default here): the job only says which tables a refresh
would profile. It reads BigQuery metadata (not billed) and the local
registry; it scans nothing, writes nothing and bills nothing.

When you want real refreshes, set DRY_RUN = False. Each run then re-profiles
every table (MODE "all"): ~610 MiB billed on TheLook (measured 2026-10-23,
~$0.004 MODELED). "all" rather than "stale": a stale-mode run only re-profiles
tables older than max_age_days (35), so a monthly stale run would leave
entries unverified (registry rules skipped) for up to ~25 days.
"""
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

DRY_RUN = True  # set to False to really refresh the registry (billed, see above)
MODE = "all"

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "runs" / "registry_refresh.log"
sys.path.insert(0, str(ROOT / "src"))

from query_preflight.__main__ import main  # noqa: E402

if __name__ == "__main__":
    argv = ["registry", "refresh", "--backend", "bigquery", "--mode", MODE]
    if DRY_RUN:
        argv.append("--dry-run")
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as log, redirect_stdout(log), redirect_stderr(log):
        print(f"=== {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
              f"{' '.join(argv)}", flush=True)
        try:
            code = main(argv)
        except Exception as e:  # noqa: BLE001 — logged for the next look at the file
            print(f"FAILED: {type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}")
            code = 1
        print(f"exit {code}", flush=True)
    sys.exit(code)
