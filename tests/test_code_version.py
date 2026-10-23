"""The code version an eval records: commit plus uncommitted changes."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import code_version as cv  # noqa: E402


def test_clean_and_dirty_descriptions():
    assert cv.describe({"commit": "abc1234", "dirty": []}) == "code at commit `abc1234`"
    dirty = cv.describe({"commit": "abc1234", "dirty": [f"src/f{i}.py" for i in range(7)]})
    assert "**plus uncommitted changes**" in dirty and "`src/f0.py`" in dirty
    assert "and 2 more" in dirty and "`src/f5.py`" not in dirty
    assert "unknown" in cv.describe({"commit": None, "dirty": []})


def test_reads_this_checkout():
    v = cv.code_version()
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cv.ROOT,
                          capture_output=True, text=True).stdout.strip()
    assert v["commit"] == head
    assert all(not p.startswith("eval/RESULTS_") for p in v["dirty"])


def test_porcelain_paths_keep_their_first_letter(monkeypatch):
    out = {"rev-parse": "abc1234", "status": " M src/query_preflight/guard.py\nM  eval/run_eval.py"}
    monkeypatch.setattr(cv, "_git", lambda *a: out[a[0]])
    assert cv.code_version()["dirty"] == ["src/query_preflight/guard.py", "eval/run_eval.py"]


def test_outside_git(monkeypatch):
    def fail(*a):
        raise OSError("no git")
    monkeypatch.setattr(cv, "_git", fail)
    assert cv.code_version() == {"commit": None, "dirty": []}
