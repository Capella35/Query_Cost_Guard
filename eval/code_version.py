"""The code version an eval measured, for its results JSON and summary header.

Taken BEFORE the run: the commit, plus any uncommitted change to the files
that decide the verdicts (src/, eval/, scripts/, customer.yaml,
requirements.txt). The RESULTS_*.md summaries are left out: a run rewrites
its own summary. A number is checkable later only against this version.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEASURED = ("src", "eval", "scripts", "customer.yaml", "requirements.txt", ":!eval/RESULTS_*.md")


def _git(*args: str) -> str:
    # rstrip only: a porcelain line starts with a status column that may be a space
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.rstrip()


def code_version() -> dict:
    """{"commit": short hash, "dirty": [changed paths]}; commit None outside git."""
    try:
        commit = _git("rev-parse", "--short", "HEAD")
        status = _git("status", "--porcelain", "--untracked-files=no", "--", *MEASURED)
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": []}
    return {"commit": commit, "dirty": [line[3:] for line in status.splitlines()]}


def describe(version: dict) -> str:
    """One line for a summary header."""
    if not version.get("commit"):
        return "code version unknown (not a git checkout)"
    if not version["dirty"]:
        return f"code at commit `{version['commit']}`"
    shown = ", ".join(f"`{p}`" for p in version["dirty"][:5])
    more = f" and {len(version['dirty']) - 5} more" if len(version["dirty"]) > 5 else ""
    return (f"code at commit `{version['commit']}` **plus uncommitted changes** "
            f"({shown}{more})")
