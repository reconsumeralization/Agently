"""Pinned SkillsExecutor compatibility-facade usage.

Run:
    python examples/release_pinned_usage/03_skills_executor_compatibility_facade.py

Expected key output:
    install_status=ok
    skill_id=pinned-release-checklist
    inspected_has_guidance=True
    plan_status=resolved
    selected=pinned-release-checklist
    guidance_injected=True
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "pinned-release-checklist"


def main() -> None:
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

        agent = Agently.create_agent("release-pinned-skills-executor")
        plan = agent.resolve_skills_plan("prepare release readiness notes", skills=[skill_id], mode="required")
        selected = [str(skill.get("skill_id")) for skill in plan.get("selected_skills", [])]
        bindings = plan.get("prompt_bindings", [])

        print("install_status=ok")
        print(f"skill_id={skill_id}")
        print(f"inspected_has_guidance={bool(guidance)}")
        print(f"plan_status={plan.get('status')}")
        print(f"selected={','.join(selected)}")
        print(f"guidance_injected={bool(guidance) and any(binding.get('content') for binding in bindings)}")


if __name__ == "__main__":
    main()
