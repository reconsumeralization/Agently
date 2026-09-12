"""Let the real context selector apply reading conditions from SKILL.md.

Run with an explicitly configured OpenAI-compatible service:
    AGENTLY_BASE_URL=... AGENTLY_API_KEY=... AGENTLY_MODEL=... \
      python examples/skills_executor/11_conditional_resource_read.py

Working principle:
    required Skill -> read root instructions -> model selects optional refs
    using current intent/phase -> Host reads selected files -> ContextPackage

Expected key output from a real local-model run:
    outline [None, 'SKILL.md']
    handoff [None, 'SKILL.md', 'references/primary.md']

The None path is the original task-state block, not a Skill resource.
The handoff body is read from primary.md; the outline does not read it.

This inspects disclosure, not a finished business answer or Action execution.
No script execution or filesystem mutation capability is granted to the model.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from agently.core import SkillLibrary  # noqa: E402


async def main() -> None:
    with TemporaryDirectory(prefix="agently-context-guidance-") as directory:
        root = Path(directory)
        agent = Agently.create_agent("conditional-resource-read")
        agent.set_settings(
            "plugins.ModelRequester.OpenAICompatible",
            {
                "base_url": os.environ["AGENTLY_BASE_URL"],
                "auth": os.environ["AGENTLY_API_KEY"],
                "model": os.environ["AGENTLY_MODEL"],
            },
        )
        agent.use_task_workspace(root / "files")
        agent.use_record_store(root / "records")
        agent.skill_library = SkillLibrary(root / "library")
        revision = agent.skill_library.install(
            Path(__file__).parent / "skills/conditional-resource-guide",
            trust="trusted",
        )
        execution = agent.create_execution().input("Prepare a release handoff").require_skills(revision.revision_ref)
        for phase, intent in (
            ("outline", "Sketch the initial outline; detailed work has not begun."),
            ("handoff", "Prepare the release handoff for the operator."),
        ):
            package = await execution.async_read_task_context(
                intent=intent,
                consumer_id=f"writer:{phase}",
                phase=phase,
            )
            print(phase, [block.metadata.get("resource_path") for block in package.blocks])
            for block in package.blocks:
                if block.role != "instruction":
                    print(block.content)


if __name__ == "__main__":
    asyncio.run(main())
