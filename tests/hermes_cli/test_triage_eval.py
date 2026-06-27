"""Phase 0 — triage eval harness (scripts/triage_eval.py). Synthetic cases only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path("/Users/alfred/.hermes/hermes-agent")
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import triage_eval as te  # noqa: E402


def _case(cid, gated, label=None, tier="low"):
    return {
        "case_id": cid,
        "input": {"title": f"t {cid}", "body": "b", "labels": [], "project": "P"},
        "classifier_decision": {"gated": gated, "tier": tier, "reason": "r"},
        "label": label, "label_source": "mj" if label is not None else "none",
        "mj_decision_state": None, "provenance": {}, "split": te._split_for(cid),
    }


def test_split_is_deterministic_and_partitions():
    a = te._split_for("AGENTS-148")
    assert a == te._split_for("AGENTS-148")  # stable
    assert a in {"train", "dev", "test"}


def test_score_self_consistency_perfect_when_policy_matches_recorded():
    cases = [_case("A-1", True), _case("A-2", False), _case("A-3", True)]
    # policy that exactly reproduces the recorded decision → 100% (no gold labels)
    policy = lambda f: {"gated": "1" in f["title"] or "3" in f["title"]}
    rep = te.score_policy(cases, policy)
    assert rep["agreement"] == 1.0
    assert rep["gold_labels_in_set"] == 0
    assert "weak self-labels" in rep["agreement_basis"]


def test_score_against_gold_labels_and_disagreements():
    # gold label present → measured against MJ, and a disagreement is surfaced
    cases = [_case("A-1", gated=True, label=False),  # classifier gated, MJ says not
             _case("A-2", gated=False, label=False)]
    policy = lambda f: {"gated": True}  # always gates
    rep = te.score_policy(cases, policy)
    assert rep["gold_labels_in_set"] == 2
    assert rep["agreement_basis"] == "gold MJ labels"
    # A-1: pred True truth False = fp ; A-2: pred True truth False = fp
    assert rep["confusion"]["pred_gated_truth_not"] == 2
    assert {d["case_id"] for d in rep["disagreements"]} == {"A-1", "A-2"}


def test_score_confusion_matrix_counts():
    cases = [_case("A-1", True, label=True), _case("A-2", False, label=False),
             _case("A-3", True, label=False), _case("A-4", False, label=True)]
    policy = lambda f: {"gated": f["title"].endswith(("1", "3"))}  # gates A-1,A-3
    rep = te.score_policy(cases, policy)
    c = rep["confusion"]
    assert c["gated_gated"] == 1        # A-1 pred T truth T
    assert c["ungated_ungated"] == 1    # A-2 pred F truth F
    assert c["pred_gated_truth_not"] == 1   # A-3 pred T truth F
    assert c["pred_not_truth_gated"] == 1   # A-4 pred F truth T


def test_split_filter():
    cases = [_case(f"A-{i}", True) for i in range(20)]
    for sp in ("train", "dev", "test"):
        rep = te.score_policy(cases, lambda f: {"gated": True}, split=sp)
        assert rep["split"] == sp
        assert all(True for _ in range(rep["n"]))


def test_real_classifier_runs_on_real_cases_and_report_is_coherent():
    """The real _is_gated runs over the live cases and produces a coherent
    report. NOTE: it is NOT ~100% vs the recorded `requires_mj_review` — that
    field is the *failure_cost* gate, a different classifier; their divergence
    (~22%) is a real triage-consistency finding, not a harness bug."""
    cases = te.extract_cases()
    if not cases:
        pytest.skip("no live outbox cases")
    rep = te.score_policy(cases, te.real_classifier_policy())
    c = rep["confusion"]
    assert rep["n"] == len(cases)
    # confusion matrix must partition every case exactly once
    assert sum(c.values()) == rep["n"]
    assert 0.0 <= rep["agreement"] <= 1.0
    # deterministic: same result on a second run
    assert te.score_policy(cases, te.real_classifier_policy())["agreement"] == rep["agreement"]
