#!/usr/bin/env python3
"""F3.2: threshold-proposal judge, with its own labeled eval.

20 labeled proposals built from generated run histories (fixed seed):
- 8 good: steady shapes, bounded tightening well above normal runs;
- 6 gate-bad: loosened to p99, tiny sample, above the hard cap, a 60% jump,
  below the baseline p95 — the deterministic gates must reject ALL of these;
- 6 subtle-bad: pass every gate but should not be enforced (heavy month-end
  tail, upward drift, history from one burst, threshold hugging p95 on a
  noisy shape). Only the judge can catch these.

Pipeline per proposal: gates -> judge (only if gates pass). The judge sees
aggregates only (llm.judge_payload): counts, percentiles, trend, span,
would-block rate — never SQL, never the fingerprint. It may reject or hold,
never enforce. For a bad proposal, reject and hold both count as correct;
the number that matters most is bad proposals the judge APPROVED.

Run:  .venv/bin/python eval/run_judge_eval.py            # gates only
      .venv/bin/python eval/run_judge_eval.py --llm claude  # gates + Claude judge
      .venv/bin/python eval/run_judge_eval.py --set test --llm claude  # the TEST set, once

--set test: eval/judge_test.yaml, 14 proposals pre-registered 2026-10-24
(different kinds; labels fixed before any judge saw them). Run ONCE: writes
eval/RESULTS_judge_test.md; a later run needs --rerun and writes
eval/RESULTS_judge_test_rerun.md. Gates-only on it prints and writes nothing.

Writes eval/RESULTS_judge.md. Once that file records a live judge run (the
first, 2026-10-23), it is never overwritten: a later --llm claude run needs
--rerun and writes eval/RESULTS_judge_rerun.md (the 20 proposals are then a
tuning set); a gates-only run prints and writes nothing.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from query_preflight.investigator import ThresholdProposal, _trend  # noqa: E402
from query_preflight.thresholds import FingerprintStats, gate_violations  # noqa: E402
from code_version import code_version, describe  # noqa: E402

HARD_CAP = 300.0
rng = random.Random(20261013)


@dataclass
class Case:
    id: str
    label: str  # "good" | "gate_bad" | "subtle_bad"
    kind: str
    proposal: ThresholdProposal


def build(id_, label, kind, durations, old, new=None, span_days=21.0, recent_mult=None):
    stats = FingerprintStats(f"shape_{id_}", [(0, d) for d in durations])
    if new is None:  # the Investigator's rule: p95 x 1.5, bounded to 25%
        new = round(max(stats.p95_secs * 1.5, old * 0.75), 4)
    p = ThresholdProposal(
        fingerprint=f"shape_{id_}",
        metric="duration_sec",
        old_threshold=old,
        new_threshold=new,
        evidence="",
        baseline_runs=stats.run_count,
        baseline_p50=round(stats.median_secs, 4),
        baseline_p95=round(stats.p95_secs, 4),
        baseline_p99=round(stats.p99_secs, 4),
        baseline_max=round(stats.max_secs, 4),
        recent_median=round(stats.median_secs * (recent_mult or rng.uniform(1.6, 3.0)), 4),
        history_span_days=span_days,
        baseline_trend_ratio=_trend(stats),
        would_block_rate_on_history=round(sum(d > new for d in durations) / len(durations), 4),
    )
    return Case(id_, label, kind, p)


def steady(n, mu, sd):
    return [max(0.01, rng.gauss(mu, sd)) for _ in range(n)]


def cases() -> list[Case]:
    out = []
    # ---- good: steady shapes, generous headroom --------------------------
    for i, (mu, old) in enumerate([(0.3, 1.0), (0.8, 2.0), (1.5, 4.0), (0.05, 0.2),
                                   (2.0, 6.0), (0.4, 1.2), (5.0, 12.0), (0.12, 0.4)]):
        out.append(build(f"G{i + 1}", "good", "steady, bounded tightening",
                         steady(60, mu, mu * 0.08), old))
    # ---- gate-bad: each a different deterministic violation --------------
    d = steady(60, 0.3, 0.03)
    s = FingerprintStats("x", [(0, v) for v in d])
    out += [
        build("B1", "gate_bad", "loosened to p99 x 3", d, old=0.5, new=round(s.p99_secs * 3, 4)),
        build("B2", "gate_bad", "tiny sample (5 runs)", steady(5, 0.3, 0.03), old=1.0),
        build("B3", "gate_bad", "above hard cap", d, old=400.0, new=350.0),
        build("B4", "gate_bad", "60% jump in one update", d, old=1.0, new=0.4),
        build("B5", "gate_bad", "below baseline p95", d, old=0.4, new=round(s.median_secs, 4)),
        build("B6", "gate_bad", "loosened (new > old)", d, old=0.6, new=0.75),
    ]
    # ---- subtle-bad: pass gates, should not be enforced ------------------
    month_end = [0.3 + rng.uniform(-0.02, 0.02) for _ in range(58)] + [6.0, 7.0]
    rng.shuffle(month_end)
    drift = [0.2 + 0.4 * i / 59 + rng.uniform(-0.02, 0.02) for i in range(60)]
    noisy = [rng.lognormvariate(-1.0, 0.9) for _ in range(60)]
    s_noisy = FingerprintStats("n", [(0, v) for v in noisy])
    out += [
        build("S1", "subtle_bad", "month-end tail: 3% legit runs at 6-7s", month_end, old=0.6),
        build("S2", "subtle_bad", "upward drift (data growth) in baseline", drift, old=1.0),
        build("S3", "subtle_bad", "history from one 1-hour burst", steady(40, 0.3, 0.02),
              old=1.0, span_days=0.04),
        build("S4", "subtle_bad", "noisy shape, threshold hugs p95", noisy,
              old=round(s_noisy.p95_secs * 1.3, 4), new=round(s_noisy.p95_secs * 1.01, 4)),
        build("S5", "subtle_bad", "month-end tail + short span", month_end, old=0.6, span_days=2.0),
        build("S6", "subtle_bad", "drift + tightening near p95", drift,
              old=0.8, new=round(FingerprintStats("d", [(0, v) for v in drift]).p95_secs * 1.05, 4)),
    ]
    return out


# The signal each subtle-bad proposal is built around: the judge's reason
# should cite it (read by a person; not scored).
SIGNAL = {"S1": "tail rate (would_block_rate)", "S2": "trend ratio",
          "S3": "history span (one hour)", "S4": "new threshold ≈ p95",
          "S5": "history span + tail rate", "S6": "trend ratio + new ≈ p95"}
# The right verdict per subtle-bad row (owner's call 2026-10-23, set before the
# first live run): reject where the evidence is clear; S3's one-hour history is
# ambiguous by the judge's own prompt, so hold is right there too. A second
# number next to the safety score (where reject and hold both count), so a
# judge that holds on everything shows up; only outcome "judged" counts.
RIGHT_VERDICT = {"S1": {"reject"}, "S2": {"reject"}, "S3": {"reject", "hold"},
                 "S4": {"reject"}, "S5": {"reject"}, "S6": {"reject"}}


def _generate(gen: dict, r: random.Random) -> list[float]:
    """Durations for one test case (generators listed in judge_test.yaml)."""
    (kind, a), = gen.items()
    n, out = a["n"], []
    for i in range(n):
        if kind == "steady":
            d = r.gauss(a["mu"], a["sd"])
        elif kind == "lognormal":
            d = r.lognormvariate(a["mu_log"], a["sigma"])
        elif kind == "trend":
            d = a["start"] + (a["end"] - a["start"]) * i / (n - 1) + r.uniform(-a["noise"], a["noise"])
        elif kind == "step":
            d = (a["before"] if i < n * a["at"] else a["after"]) + r.uniform(-a["noise"], a["noise"])
        elif kind == "spikes":
            d = a["base"] * (a["mult"] if (i + 1) % a["every"] == 0 else 1) \
                + r.uniform(-a["noise"], a["noise"])
        elif kind == "bimodal":
            d = (a["high"] if r.random() < a["p_high"] else a["low"]) + r.uniform(-a["noise"], a["noise"])
        else:
            raise ValueError(f"unknown generator {kind!r}")
        out.append(max(0.001, d))
    return out


def _threshold(spec, stats: FingerprintStats):
    """A number, or {p95|p99|max: factor} of the baseline."""
    if spec is None or isinstance(spec, (int, float)):
        return spec
    (stat, factor), = spec.items()
    base = {"p95": stats.p95_secs, "p99": stats.p99_secs, "max": stats.max_secs}[stat]
    return round(base * factor, 4)


def test_cases(path: Path = ROOT / "eval" / "judge_test.yaml"):
    """The pre-registered test set: (cases, right verdicts, signals)."""
    import yaml

    spec = yaml.safe_load(path.read_text())
    r = random.Random(spec["seed"])
    out, right, signal = [], {}, {}
    for c in spec["cases"]:
        durations = _generate(c["gen"], r)
        stats = FingerprintStats("s", [(0, d) for d in durations])
        old = _threshold(c["old"], stats)
        new = _threshold(c.get("new"), stats)
        if new is None:  # the Investigator's rule, rounded UP: a 25% move stays 25%
            new = math.ceil(max(stats.p95_secs * 1.5, old * 0.75) * 1e4) / 1e4
        out.append(build(c["id"], c["label"], c["kind"], durations, old, new,
                         c["span_days"], c["recent_mult"]))
        if c["label"] != "good":
            right[c["id"]], signal[c["id"]] = set(c["right_verdict"]), c["signal"]
    return out, right, signal


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", choices=["none", "claude"], default="none")
    ap.add_argument("--rerun", action="store_true",
                    help="a live judge run after the first one: a tuning run, written to "
                         "RESULTS_judge[_test]_rerun.md")
    ap.add_argument("--set", choices=["tuning", "test"], default="tuning",
                    help="tuning: the 20 built-in proposals; test: eval/judge_test.yaml, run once")
    args = ap.parse_args()
    stem = "RESULTS_judge_test" if args.set == "test" else "RESULTS_judge"
    first = ROOT / "eval" / f"{stem}.md"
    live_recorded = first.exists() and "judge: `claude`" in first.read_text()
    if live_recorded and args.llm == "claude" and not args.rerun:
        raise SystemExit(f"the first live judge run is recorded (eval/{stem}.md); a later "
                         f"run is a tuning run: pass --rerun (writes eval/{stem}_rerun.md)")
    if args.set == "test":
        case_list, right_verdict, signal = test_cases()
    else:
        case_list, right_verdict, signal = cases(), RIGHT_VERDICT, SIGNAL
    version = code_version()  # before the run: what is measured
    judge = None
    if args.llm == "claude":
        from query_preflight.llm import ClaudeLLM

        judge = ClaudeLLM()

    rows = []
    for c in case_list:
        v = gate_violations(c.proposal, HARD_CAP)
        decision = None
        if not v and judge is not None:
            decision = judge.judge(c.proposal)
        rows.append((c, v, decision))

    gate_bad = [r for r in rows if r[0].label == "gate_bad"]
    subtle = [r for r in rows if r[0].label == "subtle_bad"]
    good = [r for r in rows if r[0].label == "good"]
    gate_bad_passed = [r for r in gate_bad if not r[1]]
    good_blocked_by_gates = [r for r in good if r[1]]
    subtle_passing_gates = [r for r in subtle if not r[1]]
    judged = [r for r in rows if r[2] is not None]

    def correct(r):
        return r[2] == "approve" if r[0].label == "good" else r[2] in ("reject", "hold")

    print(f"{'id':<6}{'label':<12}{'gates':<8}{'judge':<9}kind")
    for c, v, d in rows:
        print(f"{c.id:<6}{c.label:<12}{'pass' if not v else 'REJECT':<8}{d or '-':<9}{c.kind}"
              + (f"  [{v[0]}]" if v else ""))
    summary = {
        "gate_bad_passing_gates": f"{len(gate_bad_passed)}/{len(gate_bad)}",
        "good_rejected_by_gates": f"{len(good_blocked_by_gates)}/{len(good)}",
        "subtle_bad_passing_gates": f"{len(subtle_passing_gates)}/{len(subtle)} (judge's job)",
    }
    if judge is not None:
        # The three numbers, most important first; then whether the holds are
        # judgments at all (a refusal or an error also holds) and the cost.
        not_judged = [r for r in judged if r[0].proposal.judge_outcome != "judged"]
        usd = [r[0].proposal.judge_usd for r in judged if r[0].proposal.judge_usd is not None]
        summary.update({
            "1_bad_wrongly_approved": f"{sum(r[0].label != 'good' and r[2] == 'approve' for r in judged)}"
                                      f"/{sum(r[0].label != 'good' for r in judged)} (must be 0)",
            "2_subtle_bad_rejected_or_held": f"{sum(correct(r) for r in judged if r[0].label == 'subtle_bad')}"
                                             f"/{sum(r[0].label == 'subtle_bad' for r in judged)}",
            "2b_subtle_bad_right_verdict": f"{sum(r[2] in right_verdict[r[0].id] and r[0].proposal.judge_outcome == 'judged' for r in judged if r[0].label == 'subtle_bad')}"
                                           f"/{sum(r[0].label == 'subtle_bad' for r in judged)} "
                                           "(per row: see right verdict)",
            "3_good_rejected_or_held": f"{sum(r[0].label == 'good' and r[2] != 'approve' for r in judged)}"
                                       f"/{sum(r[0].label == 'good' for r in judged)} (false alarms)",
            "judge_agreement": f"{sum(correct(r) for r in judged)}/{len(judged)}",
            "not_a_judgment": f"{len(not_judged)}/{len(judged)}"
                              + (" (" + ", ".join(f"{r[0].id}: {r[0].proposal.judge_outcome}"
                                                  for r in not_judged) + ")" if not_judged else ""),
            "llm_calls": judge.calls,
            "judge_usd_modeled": f"{sum(usd):.5f} total, {sum(usd) / len(usd):.6f} per decision"
                                 if usd else "unknown (no usage reported, or model not priced)",
        })
    else:
        summary["judge"] = "not run (pass --llm claude; needs Anthropic credentials)"
    print("\n" + "\n".join(f"{k}: {v}" for k, v in summary.items()))

    ts = datetime.now(timezone.utc)
    md = [
        "# Threshold-proposal judge eval (F3.2)" + (": TEST set (pre-registered)"
                                                       if args.set == "test" else ""),
        "",
        f"Generated by `eval/run_judge_eval.py` on {ts:%Y-%m-%d %H:%M} UTC, judge: `{args.llm}`, "
        f"{describe(version)}. "
        + (f"{len(rows)} proposals pre-registered in `eval/judge_test.yaml` (labels fixed "
           "before any judge saw them). " if args.set == "test" else
           "20 labeled proposals from generated histories (fixed seed). ")
        + "Gates run first; the judge "
        "sees only proposals that pass them, and only aggregates (no SQL, no fingerprint).",
        "",
        "| metric | value |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in summary.items()],
        "",
        "| id | label | kind | gates | judge | right verdict | outcome | should cite | judge's reason |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c, v, d in rows:
        reason = (c.proposal.judge_reason or "").replace("|", "/").replace("\n", " ")
        md.append(f"| {c.id} | {c.label} | {c.kind} | {'pass' if not v else 'reject: ' + v[0]} | "
                  f"{d or '—'} | {' or '.join(sorted(right_verdict.get(c.id, ())))
                                  or ('approve' if c.label == 'good' else '—')} | "
                  f"{c.proposal.judge_outcome or '—'} | {signal.get(c.id, '—')} | "
                  f"{reason or '—'} |")
    if judge is not None:
        md += ["", "A right verdict for the wrong reason is a measurement problem: read each "
               "subtle row's reason against the signal it should cite. Only outcome `judged` is a "
               "judgment; a refusal or an error holds, which scores as correct on a bad row."]
    if args.llm == "none" and (live_recorded or args.set == "test"):
        print(f"gates only; not written (eval/{stem}.md is for the live run)")
    else:
        rerun = live_recorded and args.llm == "claude"
        if rerun:
            md[1:1] = ["", f"**RERUN** — a tuning run: the first live run "
                       f"([`{stem}.md`]({stem}.md)) stays the first measurement; "
                       "these proposals have been a tuning set since it."]
        name = f"{stem}_rerun.md" if rerun else f"{stem}.md"
        (ROOT / "eval" / name).write_text("\n".join(md) + "\n")
        print(f"summary -> eval/{name}")
    if gate_bad_passed:
        raise SystemExit("FAIL: a gate-bad proposal passed the deterministic gates")


if __name__ == "__main__":
    main()
