"""Low-level Skills Executor smoke example (standard SKILL.md, no model call).

Run:
    python examples/skills_executor/01_basic_declarative_skills.py

    The script also works when invoked by absolute path from outside the repo.

Expected key output from a real run:
    install_status=ok
    skill_id=release-checklist
    plan_status=resolved
    selected=release-checklist
    guidance_injected=True

This is intentionally scoped to registry + planner mechanics under the standard
SKILL.md model. It installs a guidance-only Skill, inspects the normalized
contract, and resolves a `required` plan — all deterministic, so it needs no
model. Running the Skill (`run_skills_task`) is shown in the other examples.
"""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


SKILL_MD = """\
---
name: Release Checklist
description: Check release readiness and record a release note. Use for release and rollback requests.
keywords: [release, rollback, checklist]
---

# Release Checklist

Given a release request, confirm readiness: changelog present, migrations
reviewed, rollback plan defined, on-call notified. Then write a concise release
note summarizing what ships.
"""


def main():
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        skill_root = temp_path / "release-skill"
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        Agently.skills_executor.configure(
            registry_root=str(temp_path / "registry"),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(skill_root, trust_level="local")
        skill_id = str(contract["skill_id"])
        print(f"install_status=ok")
        print(f"skill_id={skill_id}")

        # Inspect the normalized contract (guidance-only, no stages).
        inspected = Agently.skills_executor.inspect_skills(skill_id)
        guidance = str(inspected.get("guidance", {}).get("content", ""))

        # Resolve a deterministic `required` plan — no model call needed.
        agent = Agently.create_agent("skills-example")
        plan = agent.resolve_skills_plan("prepare release notes", skills=[skill_id], mode="required")
        selected = [str(s.get("skill_id")) for s in plan.get("selected_skills", [])]
        bindings = plan.get("prompt_bindings", [])

        print(f"plan_status={plan.get('status')}")
        print(f"selected={','.join(selected)}")
        print(f"guidance_injected={bool(guidance) and any(b.get('content') for b in bindings)}")


if __name__ == "__main__":
    main()
