"""Pinned SkillLibrary installation and AgentExecution binding usage.

Run:
    python examples/release_pinned_usage/03_skill_library_agent_binding.py

Expected key output:
    install_status=ok
    skill_id=pinned-release-checklist
    inspected_has_guidance=True
    binding_mode=required
    exact_revision_bound=True
    task_context_has_skill_library=True
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402


SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "pinned-release-checklist"


async def async_main() -> None:
    with TemporaryDirectory() as temp_dir:
        registry_root = Path(temp_dir) / "registry"
        Agently.skills_executor.configure(
            registry_root=str(registry_root),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(SKILL_SOURCE, trust_level="local")
        skill_id = str(contract["skill_id"])
        inspected = Agently.skills_executor.inspect_skills(skill_id)
        guidance = str(inspected.get("guidance", {}).get("content", ""))
        package = Agently.skill_library.resolve(skill_id)

        agent = Agently.create_agent("release-pinned-skill-binding")
        execution = agent.input("prepare release readiness notes").require_skills(
            package.revision_ref
        )
        await execution.async_prepare_task_context()
        binding = execution.skill_bindings[0]
        source_catalog = execution.task_context.source_catalog()

        print("install_status=ok")
        print(f"skill_id={skill_id}")
        print(f"inspected_has_guidance={bool(guidance)}")
        print(f"binding_mode={binding.mode}")
        print(f"exact_revision_bound={binding.revision_ref == package.revision_ref}")
        print(f"task_context_has_skill_library={'skill_library' in source_catalog}")


if __name__ == "__main__":
    asyncio.run(async_main())
