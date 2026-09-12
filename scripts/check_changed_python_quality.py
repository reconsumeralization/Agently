"""Run Ruff only on Python files changed from a reviewed Git base."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def _git_paths(repository: Path, *args: str) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return [
        path.decode("utf-8")
        for path in completed.stdout.split(b"\0")
        if path
    ]


def changed_python_files(repository: Path, base: str) -> list[str]:
    committed = _git_paths(
        repository,
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
        f"{base}...HEAD",
        "--",
        "*.py",
    )
    working = _git_paths(
        repository,
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
        "HEAD",
        "--",
        "*.py",
    )
    untracked = _git_paths(
        repository,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        "*.py",
    )
    return sorted(
        {
            path
            for path in [*committed, *working, *untracked]
            if (repository / path).is_file()
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="Reviewed Git base commit/ref.")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--ruff", default="ruff")
    args = parser.parse_args()

    repository = args.repository.resolve()
    paths = changed_python_files(repository, args.base)
    if not paths:
        print("No changed Python files to lint.")
        return 0
    completed = subprocess.run(
        [args.ruff, "check", "--force-exclude", *paths],
        cwd=repository,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
