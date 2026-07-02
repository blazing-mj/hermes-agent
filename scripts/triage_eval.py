#!/usr/bin/env python3
"""triage_eval.py — Phase 0 eval harness for TeamOS triage (eval-flywheel brief §5).

Materializes the outbox into a versioned case store and scores ANY triage policy
against it. Reusable infra (the case store + runner + split are shared with the
EMA quality brief per §11) — kept policy-agnostic.

HONEST-BASELINE NOTE (verified 2026-06-27, load-bearing):
The outbox does NOT contain MJ *judgment* labels. Its `mj_decision_state` values
are pipeline lifecycle states (queued/succeeded), and every case that reached MJ
was gated — there are ZERO recorded gate overrides. So this harness:
  • extracts the real cases + the real classifier decision + provenance,
  • assigns a deterministic train/dev/test split up front (anti-Goodhart),
  • scores a policy against the *recorded classifier decision* as a
    self-consistency baseline (the real classifier ⇒ ~100%, proving the harness
    extracts features correctly and the policy is deterministic),
  • and emits a LABEL WORKLIST — the cases MJ must actually label for the first
    real agreement number. Real agreement is *pending those labels*, never
    fabricated from lifecycle states.

Cases are written to ~/.hermes/triage-eval/cases.jsonl (LOCAL, not git-tracked
— they contain ticket content).

CLI:
  triage_eval.py materialize     # build cases.jsonl from the live outbox
  triage_eval.py baseline        # run the REAL classifier; report self-consistency + label gaps
  triage_eval.py worklist [N]    # the N most-informative cases for MJ to label
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

H = Path.home()
DB_PATH = Path(__import__("os").environ.get("TEAM_OS_STATE_DB", H / ".hermes" / "state" / "team-os-cortex.db"))
EVAL_DIR = H / ".hermes" / "triage-eval"
CASES_PATH = EVAL_DIR / "cases.jsonl"
_MOTOR = H / ".hermes" / "hermes-agent" / "scripts" / "team_os_linear_intake_motor.py"

# Policy: features dict -> {"gated": bool}. Injectable for tests.
Policy = Callable[[dict[str, Any]], dict[str, Any]]


def _split_for(case_id: str) -> str:
    """Deterministic train/dev/test by hashing the id (reproducible, ~60/20/20)."""
    h = int(hashlib.sha256(case_id.encode()).hexdigest(), 16) % 100
    return "train" if h < 60 else "dev" if h < 80 else "test"


def extract_cases(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Build eval cases from the outbox. One case per issue: the input features
    the classifier sees + its recorded decision + provenance. Labels are left
    None (no MJ judgment labels exist — see module docstring)."""
    import sqlite3
    if not db_path.exists():
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT source_id, payload_json, created_at FROM outbox ORDER BY id").fetchall()
    con.close()
    cases: list[dict[str, Any]] = []
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        cid = str(r["source_id"])
        labels = p.get("labels") if isinstance(p.get("labels"), list) else []
        cases.append({
            "case_id": cid,
            "input": {
                "title": p.get("title") or "",
                "body": p.get("body") or "",
                "labels": labels,
                "project": p.get("project") or "",
            },
            "classifier_decision": {
                # requires_mj_review is the recorded gate verdict for this case
                "gated": bool(p.get("requires_mj_review")),
                "tier": p.get("failure_cost_tier"),
                "reason": p.get("failure_cost_reason"),
            },
            "label": None,                 # gold label — MUST be supplied by MJ (Phase 2)
            "label_source": "none",        # "mj" once labeled; never fabricated
            "mj_decision_state": p.get("mj_decision_state"),  # lifecycle state, NOT a label
            "provenance": {"source_id": cid, "extracted_at": int(r["created_at"] or 0)},
            "split": _split_for(cid),
        })
    return cases


def materialize(db_path: Path = DB_PATH) -> dict[str, Any]:
    """Write cases.jsonl (immutable snapshot). Returns a summary."""
    cases = extract_cases(db_path)
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    with CASES_PATH.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, sort_keys=True) + "\n")
    from collections import Counter
    splits = Counter(c["split"] for c in cases)
    gated = sum(1 for c in cases if c["classifier_decision"]["gated"])
    return {
        "cases": len(cases),
        "path": str(CASES_PATH),
        "splits": dict(splits),
        "classifier_gated": gated,
        "classifier_not_gated": len(cases) - gated,
        "gold_labels": 0,  # honest: none exist
    }


def load_cases() -> list[dict[str, Any]]:
    if not CASES_PATH.exists():
        return []
    return [json.loads(l) for l in CASES_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]


def real_classifier_policy() -> Policy:
    """The HONEST baseline arm: the actual production `_is_gated`, never a strawman."""
    spec = importlib.util.spec_from_file_location("_motor", _MOTOR)
    motor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(motor)

    def policy(features: dict[str, Any]) -> dict[str, Any]:
        return {"gated": bool(motor._is_gated(features))}
    return policy


def score_policy(cases: list[dict[str, Any]], policy: Policy, *, split: str | None = None) -> dict[str, Any]:
    """Score a policy over cases. Compares to the gold `label` where present;
    else to the recorded `classifier_decision` (self-consistency). Reports
    agreement, confusion matrix, and the ranked disagreement list."""
    rows = [c for c in cases if split is None or c["split"] == split]
    tp = tn = fp = fn = 0
    disagreements: list[dict[str, Any]] = []
    n_gold = 0
    for c in rows:
        pred = bool(policy(c["input"])["gated"])
        if c.get("label") is not None:
            truth = bool(c["label"]); n_gold += 1
        else:
            truth = bool(c["classifier_decision"]["gated"])  # weak self-label
        if pred and truth:
            tp += 1
        elif not pred and not truth:
            tn += 1
        elif pred and not truth:
            fp += 1; disagreements.append({"case_id": c["case_id"], "pred": pred, "truth": truth, "title": c["input"]["title"][:70]})
        else:
            fn += 1; disagreements.append({"case_id": c["case_id"], "pred": pred, "truth": truth, "title": c["input"]["title"][:70]})
    n = len(rows)
    agree = (tp + tn) / n if n else 0.0
    return {
        "n": n, "split": split or "all",
        "gold_labels_in_set": n_gold,
        "agreement": round(agree, 4),
        "agreement_basis": "gold MJ labels" if n_gold == n and n else ("MIXED" if n_gold else "weak self-labels (no gold) — sanity only"),
        "confusion": {"gated_gated": tp, "ungated_ungated": tn, "pred_gated_truth_not": fp, "pred_not_truth_gated": fn},
        "disagreements": disagreements,
    }


def worklist(n: int = 15) -> list[dict[str, Any]]:
    """The most-informative unlabeled cases for MJ to label (Phase 2 bridge):
    balanced across the gate boundary + tiers, so the first labels are maximally
    informative rather than all-easy."""
    cases = [c for c in load_cases() if c.get("label") is None]
    # balance gated vs not-gated; within gated, spread across tiers
    gated = [c for c in cases if c["classifier_decision"]["gated"]]
    notg = [c for c in cases if not c["classifier_decision"]["gated"]]
    out: list[dict[str, Any]] = []
    i = j = 0
    while len(out) < min(n, len(cases)) and (i < len(gated) or j < len(notg)):
        if j < len(notg):  # not-gated first — rarer, more informative
            out.append(notg[j]); j += 1
        if len(out) < n and i < len(gated):
            out.append(gated[i]); i += 1
    return [{"case_id": c["case_id"], "title": c["input"]["title"][:80],
             "classifier_gated": c["classifier_decision"]["gated"],
             "tier": c["classifier_decision"]["tier"]} for c in out]


def main() -> int:
    ap = argparse.ArgumentParser(description="TeamOS triage eval harness (Phase 0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("materialize")
    sub.add_parser("baseline")
    pw = sub.add_parser("worklist"); pw.add_argument("n", nargs="?", type=int, default=15)
    args = ap.parse_args()

    if args.cmd == "materialize":
        print(json.dumps(materialize(), indent=2))
    elif args.cmd == "baseline":
        cases = load_cases() or extract_cases()
        rep = score_policy(cases, real_classifier_policy())
        print(json.dumps({
            "baseline": "real _is_gated classifier",
            **rep,
            "honest_note": (
                f"{rep['gold_labels_in_set']} gold MJ labels exist. Agreement above is "
                "self-consistency (classifier vs its own recorded decision) — a sanity "
                "check, NOT 'agrees with MJ'. Run `worklist` and label cases to get the "
                "first real agreement number."),
        }, indent=2))
    elif args.cmd == "worklist":
        print(json.dumps({"label_these": worklist(args.n)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
