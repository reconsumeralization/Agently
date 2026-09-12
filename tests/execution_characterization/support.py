"""Isolated probe runner and strict observation comparison (no Agently import)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


BASELINE_COMMIT = "17f8ab170cfbc36427c75b0906a508cafcceff18"
BASELINE_TREE = "8628bcd20d9aefd32b118d26fd26f0667abb8f5e"
HERE = Path(__file__).resolve().parent
CASES = (
    "direct_text", "direct_json", "concurrent_readers", "mutation_and_fresh", "provider_failure",
    "fluent_isolation", "validation_repair", "validation_exhaustion", "review_warn", "review_block",
    "artifact_review", "artifact_escape", "default_review_artifact", "plan_ready", "plan_clarification",
    "plan_budget", "plan_validation", "long_content", "long_content_rejected", "action_once",
    "resume_flat", "resume_taskboard", "goal_direct",
    "ensure_short", "ensure_continuation", "ensure_stale", "ensure_task_conflict", "flat_task", "taskboard_task",
)


def fixture_paths() -> tuple[Path, Path]:
    """Keep exact observations for the two characterized Python representations.

    Each baseline comes from the same immutable old source, not the candidate.
    Unknown environment differences still fail exact comparison.
    """
    suffix = "-py314" if sys.version_info >= (3, 14) else ""
    return (HERE / "fixtures" / f"baseline{suffix}.json",
            HERE / "fixtures" / f"approved_deltas{suffix}.json")


def run_probe(source: Path, api: str, case: str) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}}
    env.update(PYTHONPATH=str(source.resolve()), PYTHONUTF8="1", PYTHONHASHSEED="0")
    completed = subprocess.run(
        [sys.executable, str(HERE / "probe.py"), "--source-root", str(source.resolve()),
         "--api", api, "--case", case],
        cwd=source, env=env, text=True, encoding="utf-8", capture_output=True, timeout=45,
    )
    if completed.returncode:
        raise AssertionError(f"{case} ({api}) failed:\n{completed.stderr[-9000:]}\n{completed.stdout[-2000:]}")
    records = [line.removeprefix("CHARACTERIZATION:") for line in completed.stdout.splitlines()
               if line.startswith("CHARACTERIZATION:")]
    if len(records) != 1:
        raise AssertionError(f"Expected exactly one probe observation, got {len(records)}: {completed.stdout[-2000:]}")
    result = json.loads(records[0])
    assert result["source_root"] == str(source.resolve())
    assert result["case"] == case
    return result


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Relocate the test workspace and exactly bound host-issued identities.

    Equal identities retain equal labels, including within rendered Prompts;
    arbitrary UUID-shaped business content is never normalized by pattern.
    """
    root = raw["workspace_root"]
    identities = raw.get("identities", {})
    clocks: dict[float, str] = {}
    clock_paths: dict[tuple[Any, ...], str] = {}
    for index, request in enumerate(raw["observed"].get("requests", [])):
        slot = request["prompt"].get("input", {})
        if not isinstance(slot, dict):
            continue
        diagnostics = slot.get("execution_meta", {}).get("diagnostics", {})
        prefix = ("requests", index, "prompt", "input", "execution_meta", "diagnostics")
        event_times = [event["monotonic_time"] for event in diagnostics.get("stages", {}).get("events", [])
                       if "monotonic_time" in event]
        assert event_times == sorted(event_times), "Runtime clock ordering changed"
        timed_records: list[tuple[tuple[str | int, ...], Any]] = [(('last_progress',), diagnostics.get("last_progress", {}))]
        timed_records.extend((("stages", "events", position), event)
                             for position, event in enumerate(diagnostics.get("stages", {}).get("events", [])))
        for suffix, event in timed_records:
            for field in ("age_seconds", "monotonic_time"):
                if field in event:
                    value = event[field]
                    assert isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
                    clocks.setdefault(value, f"<CLOCK:{len(clocks) + 1}>")
                    clock_paths[(*prefix, *suffix, field)] = clocks[value]

    def walk(value: Any, path: tuple[Any, ...] = ()) -> Any:
        if path in clock_paths:
            return clock_paths[path]
        if isinstance(value, str):
            value = value.replace(root, "<WORKSPACE>")
            for identity, label in identities.items():
                value = value.replace(identity, label)
            if len(path) == 3 and path[0] == "requests" and path[2] == "prompt_text":
                for timestamp, label in clocks.items():
                    value = value.replace(str(timestamp), label)
            return value
        if isinstance(value, list):
            return [walk(item, (*path, index)) for index, item in enumerate(value)]
        if isinstance(value, dict):
            mapped = {walk(key): walk(item, (*path, key)) for key, item in value.items()}
            assert len(mapped) == len(value), "Normalization collapsed distinct keys"
            return mapped
        return value

    return walk(raw["observed"])


def differences(expected: Any, actual: Any, path: tuple[str | int, ...] = ()) -> list[dict[str, Any]]:
    """Exact typed comparison; no ignored paths, substring tolerances or sorting."""
    if type(expected) is not type(actual):
        return [{"path": list(path), "before": expected, "after": actual}]
    if isinstance(expected, dict):
        result = []
        for key in sorted(expected.keys() | actual.keys()):
            if key not in expected:
                result.append({"path": [*path, key], "operation": "add", "after": actual[key]})
            elif key not in actual:
                result.append({"path": [*path, key], "operation": "remove", "before": expected[key]})
            else:
                result.extend(differences(expected[key], actual[key], (*path, key)))
        return result
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [{"path": list(path), "before": expected, "after": actual}]
        return [diff for index, (left, right) in enumerate(zip(expected, actual))
                for diff in differences(left, right, (*path, index))]
    return [] if expected == actual else [{"path": list(path), "before": expected, "after": actual}]


def compact_differences(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pin whole rendered Prompt strings by digest instead of duplicating them.

    Full baseline text and full raw paired text remain available for inspection.
    This is exact text identity, not a semantic or approximate text comparison.
    """
    result = []
    for change in changes:
        if (change["path"] and change["path"][-1] == "prompt_text"
                and isinstance(change.get("before"), str) and isinstance(change.get("after"), str)):
            result.append({"path": change["path"], "operation": "text_sha256",
                           "before_sha256": hashlib.sha256(change["before"].encode()).hexdigest(),
                           "after_sha256": hashlib.sha256(change["after"].encode()).hexdigest()})
        else:
            result.append(change)
    return result


def assert_characterized(baseline: Any, actual: Any, approved: list[dict[str, Any]]) -> None:
    allowed = [{key: value for key, value in change.items() if key != "reason"} for change in approved]
    assert all(change.get("reason") for change in approved), "Every approved delta requires a reason"
    observed = compact_differences(differences(baseline, actual))
    mismatch = differences(allowed, observed)
    if mismatch:
        paths = [item["path"] for item in observed]
        raise AssertionError(f"Unapproved characterization difference; observed paths={paths}; "
                             f"delta mismatch={str(mismatch)[:2500]}")


def verify_baseline_source(repository: Path, source: Path) -> str:
    """Verify actual baseline runtime bytes against immutable Git blob ids."""
    tree = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", BASELINE_COMMIT + "^{tree}"], text=True,
    ).strip()
    assert tree == BASELINE_TREE, "Unexpected baseline tree"
    listing = subprocess.check_output(
        ["git", "-C", str(repository), "ls-tree", "-r", BASELINE_COMMIT, "--", "agently"], text=True,
    )
    assert listing, "Baseline runtime tree unavailable"
    expected_paths = set()
    for line in listing.splitlines():
        header, relative = line.split("\t", 1)
        _mode, kind, expected = header.split()
        expected_paths.add(relative)
        assert kind == "blob", f"Unsupported baseline tree entry {relative}"
        data = (source / relative).read_bytes()
        observed = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        assert observed == expected, f"Not the immutable baseline: {relative}"
    actual_paths = {str(path.relative_to(source)) for path in (source / "agently").rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts}
    assert actual_paths == expected_paths, "Unexpected/missing runtime files in baseline source"
    return hashlib.sha256(listing.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record observations; never refresh pytest fixtures implicitly.")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--api", required=True, choices=("baseline", "current"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new observation file.")
    raw, errors = {}, {}
    for case in args.case or CASES:
        try:
            raw[case] = run_probe(args.source_root, args.api, case)
            print(f"{case}: captured", flush=True)
        except Exception as error:
            errors[case] = str(error)
            print(f"{case}: FAILED\n{error}", flush=True)
    output = {
        "api": args.api, "source_root": str(args.source_root.resolve()),
        "probe_sha256": hashlib.sha256((HERE / "probe.py").read_bytes()).hexdigest(),
        "python": sys.version, "raw": raw, "errors": errors,
        "observations": {case: normalize(value) for case, value in raw.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(output, target, indent=2, ensure_ascii=False, allow_nan=False)
        target.write("\n")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
