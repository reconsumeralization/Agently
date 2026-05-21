"""Low-level Skills Executor smoke example.

Run:
    python examples/skills_executor/01_basic_declarative_skills.py

    The script also works when invoked by absolute path from outside the repo.

Expected key output from a real run:
    status=success
    recorded=prepare release notes
    action_logs=1

This is intentionally scoped to executor mechanics. It does not exercise
model-owned planning or response generation.
"""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


SKILL_YAML = """
skill_id: release-checklist
display_name: Release Checklist
purpose: Check release readiness and record a release note.
trust_level: local
activation:
  keywords: [release, rollback]
requires:
  actions: [record_release_note]
stages:
  - id: record_note
    kind: action
    action: record_release_note
    input:
      text: "${task}"
  - id: validate_note
    kind: validate
    validation:
      required_state: [record_note]
"""


def main():
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        registry_root = temp_path / "registry"
        skill_root = temp_path / "release-skill"
        skill_root.mkdir()
        (skill_root / "skill.yaml").write_text(SKILL_YAML, encoding="utf-8")

        Agently.settings.set("skills.registry.root", str(registry_root))
        Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
        Agently.skills_executor.install_skills(skill_root)

        agent = Agently.create_agent("skills-example")

        def record_release_note(text: str):
            return {"recorded": text}

        agent.register_action(
            name="record_release_note",
            desc="Record a release note.",
            kwargs={"text": (str, "Release note text.")},
            func=record_release_note,
        )

        execution = agent.run_skills_task(
            "prepare release notes",
            skills=["release-checklist"],
            mode="required",
        )

        print(f"status={ execution.status }")
        output = cast(dict[str, Any], execution.output)
        print(f"recorded={ output['record_note']['recorded'] }")
        print(f"action_logs={ len(execution.action_logs) }")


if __name__ == "__main__":
    main()
