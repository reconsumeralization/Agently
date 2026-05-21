from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


# Skills + DAG process-stream example.
# Expected key output from one local run:
# selected_route=skills
# stream_route_selected=True
# stream_task_events=True
# stream_stage_events=True
# output_keys=collect_requirements,emit_summary,task,validate_requirements
#
# This is a local execution-facade smoke case. It validates that Skills Executor
# stages compile through Dynamic Task / Task DAG and surface process stream
# checkpoints through agent.create_execution().


RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "skills_dag"


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_skill() -> Path:
    skill_root = RUNTIME_ROOT / "release-planning-skill"
    write_text(
        skill_root / "skill.yaml",
        """
skill_id: release-planning
version: 0.1.0
display_name: Release Planning
purpose: Plan and validate release readiness notes.
trust_level: local
activation:
  keywords: [release, readiness]
stages:
  - id: collect_requirements
    kind: model_plan
    purpose: Collect release readiness requirements.
    produces:
      - role: requirements
        type: plan
  - id: validate_requirements
    kind: validate
    validation:
      required_state: [collect_requirements]
  - id: emit_summary
    kind: emit
    data:
      summary: release planning stream completed
""",
    )
    write_text(
        skill_root / "SKILL.md",
        """---
name: Release Planning
description: Use for release readiness planning.
keywords:
  - release
---

Plan release readiness work and expose progress.
""",
    )
    return skill_root


async def main():
    Agently.skills_executor.install_skills(prepare_skill(), trust_level="local", update=True)
    agent = Agently.create_agent("skills-dag-stream")
    execution = (
        agent
        .use_skills(["release-planning"], mode="required")
        .input("Prepare release readiness notes.")
        .create_execution()
    )

    stream_paths = []
    async for item in execution.get_async_generator(type="instant"):
        if item.is_complete:
            stream_paths.append(item.path)

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    print(f"selected_route={meta['route_plan']['selected_route']}")
    print(f"stream_route_selected={'route.selected' in stream_paths}")
    print(f"stream_task_events={any(path.startswith('task_dag.tasks.') for path in stream_paths)}")
    print(f"stream_stage_events={any(path.startswith('skills.stages.') for path in stream_paths)}")
    print(f"output_keys={','.join(sorted(data.keys()))}")


if __name__ == "__main__":
    asyncio.run(main())
