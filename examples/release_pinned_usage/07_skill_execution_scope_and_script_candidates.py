"""Pinned per-execution Skill scope and inert script-candidate usage.

Run:
    python examples/release_pinned_usage/07_skill_execution_scope_and_script_candidates.py

Expected key output:
    original_scope_frozen=True
    fresh_execution_sees_new_default=True
    empty_declarations_do_not_scan_library=True
    script_candidate_status=binding_required
    script_candidate_has_sha256=True
    script_candidate_exposes_callable=False
    script_candidate_exposes_installed_path=False

This deterministic example proves composition and authorization boundaries. It
does not execute the Skill script or claim that installing a Skill grants an
Action capability.
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


SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
CHECKLIST_SKILL = SKILLS_ROOT / "pinned-release-checklist"
SCRIPT_SKILL = SKILLS_ROOT / "pinned-script-candidate"


async def main() -> None:
    with TemporaryDirectory() as temp_dir:
        Agently.skills_executor.configure(
            registry_root=str(Path(temp_dir) / "registry"),
            allowed_trust_levels=["local"],
        )
        checklist_contract = Agently.skills_executor.install_skills(
            CHECKLIST_SKILL,
            trust_level="local",
        )
        script_contract = Agently.skills_executor.install_skills(
            SCRIPT_SKILL,
            trust_level="local",
        )
        checklist = Agently.skill_library.resolve(checklist_contract["skill_id"])
        script = Agently.skill_library.resolve(script_contract["skill_id"])

        agent = Agently.create_agent("release-pinned-skill-scope")
        agent.require_skills(checklist.revision_ref, always=True)
        original_execution = agent.input("Prepare the release checklist.")
        await original_execution.async_prepare_task_context()
        original_refs = [
            binding.revision_ref for binding in original_execution.skill_bindings
        ]

        agent.require_skills(script.revision_ref, always=True)
        await original_execution.async_prepare_task_context()
        original_refs_after_agent_change = [
            binding.revision_ref for binding in original_execution.skill_bindings
        ]

        fresh_execution = agent.input("Prepare the release checklist and script review.")
        await fresh_execution.async_prepare_task_context()
        fresh_refs = [binding.revision_ref for binding in fresh_execution.skill_bindings]

        empty_execution = Agently.create_agent(
            "release-pinned-empty-skill-scope"
        ).input("Answer without declared Skills.")
        await empty_execution.async_prepare_task_context()

        context_pack = await Agently.skills_executor.async_build_context_pack(
            task="Inspect the packaged verification script without executing it.",
            skills=[script.revision_ref],
            include_references=False,
            actionize_scripts=True,
        )
        candidates = context_pack["skills"][0]["action_candidates"]
        candidate = candidates[0]

        print(
            "original_scope_frozen="
            f"{original_refs == [checklist.revision_ref] == original_refs_after_agent_change}"
        )
        print(
            "fresh_execution_sees_new_default="
            f"{fresh_refs == [checklist.revision_ref, script.revision_ref]}"
        )
        print(
            "empty_declarations_do_not_scan_library="
            f"{empty_execution.skill_bindings == []}"
        )
        print(f"script_candidate_status={candidate['status']}")
        print(f"script_candidate_has_sha256={bool(candidate['sha256'])}")
        print(f"script_candidate_exposes_callable={'callable' in candidate}")
        print(
            "script_candidate_exposes_installed_path="
            f"{'installed_path' in candidate}"
        )


if __name__ == "__main__":
    asyncio.run(main())
