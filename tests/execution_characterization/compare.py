"""Recover the pinned old runtime, repeat it, and compare with the candidate.

Creates new evidence files only. It never accepts deltas or refreshes fixtures.
The explicit --freeze-baseline option can create a *missing* baseline fixture
only from twice-run, byte-verified old source. Existing fixtures are immutable
to this command. Keep generated full raw reports in a private evidence folder.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from support import (
    BASELINE_COMMIT, BASELINE_TREE, CASES, HERE, assert_characterized, differences,
    fixture_paths, normalize, run_probe, verify_baseline_source,
)


def write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", required=True, type=Path)
    parser.add_argument("--candidate-root", type=Path, default=HERE.parents[1])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--freeze-baseline", action="store_true")
    args = parser.parse_args()
    fixture, delta_path = fixture_paths()
    if args.freeze_baseline and fixture.exists():
        parser.error("Baseline fixture exists; this command cannot overwrite it")
    args.output.mkdir(parents=True, exist_ok=False)
    inventory_hash = verify_baseline_source(args.candidate_root, args.baseline_root)
    provenance = {
        "commit": BASELINE_COMMIT, "tree": BASELINE_TREE,
        "runtime_blob_inventory_sha256": inventory_hash,
        "probe_sha256": hashlib.sha256((HERE / "probe.py").read_bytes()).hexdigest(),
        "normalizer_sha256": hashlib.sha256((HERE / "support.py").read_bytes()).hexdigest(),
        "evidence_kind": "synthetic_transport_host_observations",
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "dependencies": {name: importlib.metadata.version(name) for name in ("agently-stage", "pydantic", "pytest")},
        "source_isolation": "Every probe imports from the explicitly selected source root in a fresh subprocess",
        "baseline_repetitions": 2,
    }
    observed: dict[str, dict[str, Any]] = {}
    for name, source, api in (("baseline", args.baseline_root, "baseline"),
                              ("baseline_repeat", args.baseline_root, "baseline"),
                              ("candidate", args.candidate_root, "current")):
        observed[name] = {}
        for case in CASES:
            raw = run_probe(source, api, case)
            write_new(args.output / name / f"{case}.json", raw)
            observed[name][case] = normalize(raw)
            print(f"{name}/{case}: captured", flush=True)
    stability = differences(observed["baseline"], observed["baseline_repeat"])
    approved = json.loads(delta_path.read_text()) if delta_path.exists() else None
    unapproved = {}
    for case in CASES:
        try:
            assert_characterized(observed["baseline"][case], observed["candidate"][case],
                                 approved["changes"][case] if approved else [])
        except AssertionError as error:
            unapproved[case] = str(error)
    if fixture.exists():
        frozen = json.loads(fixture.read_text())
        assert not differences(frozen["observations"], observed["baseline"]), "Recovered baseline changed"
    report = {
        "provenance": provenance,
        "candidate_commit": subprocess.check_output(["git", "-C", str(args.candidate_root), "rev-parse", "HEAD"], text=True).strip(),
        "candidate_runtime_diff": subprocess.check_output(["git", "-C", str(args.candidate_root), "diff", "HEAD", "--", "agently"], text=True),
        "baseline_stability_differences": stability, "unapproved_cases": unapproved,
        "observations": observed,
    }
    write_new(args.output / "comparison.json", report)
    if stability:
        raise AssertionError(f"Baseline is not stable: {str(stability)[:2000]}")
    if args.freeze_baseline:
        write_new(fixture, {"provenance": provenance, "observations": observed["baseline"]})
        print("Frozen twice-run baseline. Candidate differences still require explicit review.", flush=True)
    if unapproved:
        raise SystemExit(f"Unapproved differences in {list(unapproved)}; see comparison.json")
    print(f"{len(CASES)} paired cases match the recovered baseline plus exact approved deltas.")


if __name__ == "__main__":
    main()
