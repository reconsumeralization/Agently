from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from scripts.check_changed_python_quality import changed_python_files


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_changed_python_quality.py"


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_changed_python_files_selects_only_live_python_changes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "quality@example.invalid")
    _git(repository, "config", "user.name", "Quality Test")
    (repository / "retained.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "removed.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "notes.md").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    base = _git(repository, "rev-parse", "HEAD")

    (repository / "retained.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repository / "added.py").write_text("VALUE = 3\n", encoding="utf-8")
    (repository / "removed.py").unlink()
    (repository / "notes.md").write_text("changed\n", encoding="utf-8")

    assert changed_python_files(repository, base) == ["added.py", "retained.py"]


def test_changed_python_quality_propagates_linter_failure(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "quality@example.invalid")
    _git(repository, "config", "user.name", "Quality Test")
    (repository / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "bad.py").write_text("import os\n", encoding="utf-8")

    fake_ruff = tmp_path / "fake_ruff.py"
    fake_ruff.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "raise SystemExit(1 if 'bad.py' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    fake_ruff.chmod(0o755)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base",
            base,
            "--repository",
            str(repository),
            "--ruff",
            str(fake_ruff),
        ],
        check=False,
    )
    assert completed.returncode == 1
