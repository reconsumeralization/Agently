from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


# Actions + Skills process-stream example.
# Expected key output from one local run:
# selected_route=skills
# action_called=True
# stream_action_log=True
# stream_stage_record_note=True
# recorded_text=Prepare release notes with an action-backed Skill.
#
# This is a local execution-facade smoke case for a Skill stage that calls an
# Action and reports both stage and action progress through the Agent stream.


RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "actions_skills"


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_skill() -> Path:
    skill_root = RUNTIME_ROOT / "release-checklist"
    write_text(
        skill_root / "skill.yaml",
        """
skill_id: release-checklist
version: 0.1.0
display_name: Release Checklist
purpose: Record release notes through a controlled Action.
trust_level: local
activation:
  keywords: [release]
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
""",
    )
    write_text(
        skill_root / "SKILL.md",
        """---
name: Release Checklist
description: Record release notes.
keywords:
  - release
---

Record release notes through the bound Action.
""",
    )
    return skill_root


async def main():
    Agently.skills_executor.install_skills(prepare_skill(), trust_level="local", update=True)
    calls = []

    def record_release_note(text: str):
        calls.append(text)
        return {"recorded": text}

    agent = Agently.create_agent("actions-skills-stream")
    agent.register_action(
        name="record_release_note",
        desc="Record a release note.",
        kwargs={"text": (str, "release note")},
        func=record_release_note,
    )

    task = "Prepare release notes with an action-backed Skill."
    execution = (
        agent
        .use_skills(["release-checklist"], mode="required")
        .input(task)
        .create_execution()
    )

    stream_paths = []
    async for item in execution.get_async_generator(type="instant"):
        if item.is_complete:
            stream_paths.append(item.path)

    await execution.async_get_data()
    meta = await execution.async_get_meta()
    print(f"selected_route={meta['route_plan']['selected_route']}")
    print(f"action_called={calls == [task]}")
    print(f"stream_action_log={'actions.record_release_note' in stream_paths}")
    print(f"stream_stage_record_note={'skills.stages.record_note' in stream_paths}")
    print(f"recorded_text={calls[0]}")


if __name__ == "__main__":
    asyncio.run(main())
