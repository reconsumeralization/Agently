"""Recovered pre-refactor behavior, NOT semantic model-quality assertions."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from execution_characterization.support import (
    BASELINE_COMMIT, BASELINE_TREE, CASES, HERE, assert_characterized, differences, fixture_paths, normalize, run_probe,
)


SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    baseline = json.loads(fixture_paths()[0].read_text(encoding="utf-8"))
    assert baseline["provenance"]["commit"] == BASELINE_COMMIT
    assert baseline["provenance"]["tree"] == BASELINE_TREE
    assert baseline["provenance"]["evidence_kind"] == "synthetic_transport_host_observations"
    for name, field in (("probe.py", "probe_sha256"), ("support.py", "normalizer_sha256")):
        assert baseline["provenance"][field] == hashlib.sha256((HERE / name).read_bytes()).hexdigest(), (
            f"{name} changed: rerun the immutable baseline and explicitly reconcile its evidence before updating provenance"
        )
    assert set(baseline["observations"]) == set(CASES)
    return baseline


@pytest.fixture(scope="module")
def approved() -> dict[str, Any]:
    ledger = json.loads(fixture_paths()[1].read_text(encoding="utf-8"))
    assert ledger["baseline_commit"] == BASELINE_COMMIT
    assert set(ledger["changes"]) == set(CASES)
    assert all(change["reason"] in ledger["reasons"] for changes in ledger["changes"].values() for change in changes)
    return ledger["changes"]


@pytest.mark.parametrize("case", CASES)
def test_execution_matches_recovered_behavior(case: str, frozen: dict[str, Any], approved: dict[str, Any]) -> None:
    observed = normalize(run_probe(SOURCE, "current", case))
    assert_characterized(frozen["observations"][case], observed, approved[case])


@pytest.mark.parametrize("case,path,replacement", [
    ("direct_json", ["outcome", "value", "count"], 3),
    ("direct_json", ["outcome", "value", "count"], True),
    ("direct_text", ["requests", 0, "prompt", "input"], "Dropped original input"),
    ("direct_text", ["requests", 0, "prompt_text"], "Changed instruction"),
    ("concurrent_readers", ["dispatches"], 2),
    ("action_once", ["effect"], "marker\nmarker\n"),
    ("action_once", ["action_calls"], ["marker", "marker"]),
    ("provider_failure", ["outcome", "error", "type"], "ValueError"),
    ("artifact_review", ["artifacts", 0, "sha256"], "0" * 64),
    ("default_review_artifact", ["requests", 1, "prompt_text"], "Artifact body omitted"),
])
def test_comparator_detects_unapproved_changes(
    frozen: dict[str, Any], case: str, path: list[str | int], replacement: Any,
) -> None:
    baseline = frozen["observations"][case]
    changed = copy.deepcopy(baseline)
    owner = changed
    for key in path[:-1]:
        owner = owner[key]
    owner[path[-1]] = replacement
    with pytest.raises(AssertionError, match="Unapproved characterization"):
        assert_characterized(baseline, changed, [])


def test_comparator_detects_event_reordering_and_missing_keys(frozen: dict[str, Any]) -> None:
    baseline = frozen["observations"]["artifact_review"]
    changed = copy.deepcopy(baseline)
    events = changed["events"]
    artifact = next(index for index, item in enumerate(events) if item["path"] == "artifact.completed")
    review = next(index for index, item in enumerate(events) if item["path"] == "review.started")
    events[artifact], events[review] = events[review], events[artifact]
    with pytest.raises(AssertionError):
        assert_characterized(baseline, changed, [])
    changed = copy.deepcopy(baseline)
    del changed["files"]
    with pytest.raises(AssertionError):
        assert_characterized(baseline, changed, [])


def test_approved_delta_is_exact_not_a_path_wildcard() -> None:
    baseline = {"field": "old"}
    delta = [{"path": ["field"], "before": "old", "after": "approved", "reason": "Explicit migration"}]
    assert_characterized(baseline, {"field": "approved"}, delta)
    with pytest.raises(AssertionError):
        assert_characterized(baseline, {"field": "anything else"}, delta)
    with pytest.raises(AssertionError):
        assert_characterized(baseline, baseline, delta)
    with pytest.raises(AssertionError):
        assert_characterized(baseline, {"field": "approved", "extra": True}, delta)
    with pytest.raises(AssertionError):
        assert_characterized({"field": 0}, {"field": True}, [
            {"path": ["field"], "before": 0, "after": 1, "reason": "Integer-only delta"},
        ])


@pytest.mark.parametrize("case,path,replacement", [
    ("long_content", ["outcome", "value"], "Lost chapter"),
    ("long_content", ["dispatches"], 5),
    ("long_content_rejected", ["outcome"], {"value": "bypassed validation"}),
    ("ensure_continuation", ["outcome", "value"], "lost prefix"),
    ("ensure_stale", ["outcome", "error", "type"], "ValueError"),
])
def test_migrated_contracts_still_reject_unapproved_results(
    frozen: dict[str, Any], approved: dict[str, Any],
    case: str, path: list[str], replacement: Any,
) -> None:
    observed = normalize(run_probe(SOURCE, "current", case))
    baseline = frozen["observations"][case]
    assert_characterized(baseline, observed, approved[case])
    owner = observed
    for key in path[:-1]:
        owner = owner[key]
    owner[path[-1]] = replacement
    with pytest.raises(AssertionError, match="Unapproved characterization"):
        assert_characterized(baseline, observed, approved[case])


def test_normalization_preserves_unbound_business_ids_refs_and_file_digests() -> None:
    raw = {"workspace_root": "/tmp/probe", "identities": {"runtime-id": "<EXECUTION:1>"}, "observed": {
        "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sha256": "a" * 64,
        "prompt": "[info.source] {output.reply} /tmp/unrelated", "elapsed": 1.2345,
        "execution": "runtime-id", "review": "runtime-id:review:1", "path": "/tmp/probe/result.md",
    }}
    normalized = normalize(raw)
    assert normalized == {**raw["observed"], "execution": "<EXECUTION:1>",
                          "review": "<EXECUTION:1>:review:1", "path": "<WORKSPACE>/result.md"}
    assert differences({"value": 1}, {"value": True})


def test_normalization_preserves_distinct_identity_relationships() -> None:
    raw = {"workspace_root": "/tmp/probe", "identities": {"id-1": "<RUN:1>", "id-2": "<RUN:2>"},
           "observed": {"parent": "id-1", "child": "id-2", "references": ["id-1", "id-2", "id-1"]}}
    assert normalize(raw) == {"parent": "<RUN:1>", "child": "<RUN:2>",
                              "references": ["<RUN:1>", "<RUN:2>", "<RUN:1>"]}


@pytest.mark.parametrize("events", [[{"monotonic_time": 2.0}, {"monotonic_time": 1.0}], [{"monotonic_time": -1.0}]])
def test_normalization_rejects_broken_host_clocks(events: list[dict[str, float]]) -> None:
    raw = {"workspace_root": "/tmp/probe", "observed": {"requests": [{"prompt": {
        "input": {"execution_meta": {"diagnostics": {"stages": {"events": events}}}},
    }}]}}
    with pytest.raises(AssertionError):
        normalize(raw)
