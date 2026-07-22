from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Resolve sibling Agently-Skills repo at ../Agently-Skills relative to this repo root.
AGENTLY_SKILLS_ROOT = ROOT.parent / "Agently-Skills"
RUNTIME_ROOT = ROOT / ".example_runtime" / "skills_executor" / "agently_skills_availability"

from agently import Agently


# Agently-Skills pack availability check — developer pre-flight case.
#
# Scenario: Before wiring an Agently-Skills guidance pack into the framework-side
# Skills Executor, a developer wants to verify that every active skill can be
# installed and passes executor eligibility checks when explicitly selected.
# This is not an Agent auto-orchestration acceptance example and it does not
# prove model-owned route selection. This script:
#   1. Installs the active skills from a local Agently-Skills clone in a fresh
#      local registry.
#   2. Lists each installed skill with its purpose and compatibility selection hints.
#   3. Runs a plan-resolution check for every skill using required mode and the
#      deterministic planner — no model API key is needed.
#   4. Reports an overall availability verdict.
#
# The deterministic planner check is a lightweight "does the skill pass
# eligibility filters when named directly?" gate. It verifies trust level and
# required actions without calling the model. Guidance-only skills resolve
# immediately; skills that need missing actions surface a blocking reason code.
#
# Expected key output from one local run:
# pack_status=success
# installed_count=6
#
# Installed Agently-Skills (6):
#   agently:
#   agently-dynamic-task:
#   agently-migration:
#   agently-request:
#   agently-runtime:
#   agently-triggerflow:
#
# Availability check (deterministic planner, no model call):
#   agently              → resolved
#   agently-dynamic-task → resolved
#   agently-migration    → resolved
#   agently-request      → resolved
#   agently-runtime      → resolved
#   agently-triggerflow  → resolved
#
# all_available=True
#
# Compatibility selection hints:
#   agently:
#     keywords: []
#     invocation_names: []
#   ...
#
# Note: the released facade retains the activation_hints payload key, but these
# values are compatibility metadata only. Local code must not use them as a
# router. Use this output only as registry/eligibility evidence.


def main() -> None:
    if not AGENTLY_SKILLS_ROOT.exists():
        print(f"Agently-Skills repo not found at: {AGENTLY_SKILLS_ROOT}")
        print("Clone it with:")
        print("  git clone https://github.com/AgentEra/Agently-Skills.git")
        return

    skills_dir = AGENTLY_SKILLS_ROOT / "skills"
    if not skills_dir.exists():
        print(f"Expected 'skills/' directory not found inside: {AGENTLY_SKILLS_ROOT}")
        return

    # Step 1: Install the active skills from the local repo into a fresh registry.
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)
    Agently.skills_executor.configure(registry_root=str(RUNTIME_ROOT / "registry"))
    print("Installing Agently-Skills pack from local repo...")
    pack_report = Agently.skills_executor.install_skills_pack(
        source=str(skills_dir),
        name="agently-skills",
        update=True,
    )
    print(f"pack_status={pack_report.get('status')}")
    print(f"installed_count={len(pack_report.get('installed_skills', []))}")
    if pack_report.get("failed_skills"):
        print("failed:")
        for entry in pack_report["failed_skills"]:
            print(f"  {Path(entry.get('path', '')).name}: {entry.get('error', '')}")

    # Step 2: List installed skills from this pack.
    all_skills = Agently.skills_executor.list_skills()
    pack_skills = [s for s in all_skills if s.get("skills_pack_id") == "agently-skills"]
    pack_skills.sort(key=lambda s: s.get("skill_id", ""))

    print(f"\nInstalled Agently-Skills ({len(pack_skills)}):")
    for skill in pack_skills:
        purpose = str(
            skill.get("description")
            or skill.get("card", {}).get("description")
            or ""
        )
        truncated = purpose[:80] + ("..." if len(purpose) > 80 else "")
        print(f"  {skill['skill_id']}: {truncated}")

    # Step 3: Availability check — plan resolution without a model call.
    agent = Agently.create_agent("skills-availability-checker")
    print("\nAvailability check (deterministic planner, no model call):")

    all_available = True
    max_id_len = max((len(s.get("skill_id", "")) for s in pack_skills), default=10)

    for skill in pack_skills:
        skill_id = skill["skill_id"]
        plan = agent.resolve_skills_plan(
            task=f"availability check for {skill_id}",
            skills=[skill_id],
            mode="required",
        )
        resolved_status = plan.get("status", "unknown")
        is_available = resolved_status == "resolved"
        if not is_available:
            all_available = False
            reason_parts = []
            for rejection in plan.get("rejected_skills", []):
                if rejection.get("skill_id") == skill_id:
                    code = rejection.get("reason_code", "")
                    reason = rejection.get("reason", "")[:60]
                    reason_parts.append(f"{code}: {reason}")
            reason_str = " — " + "; ".join(reason_parts) if reason_parts else ""
            print(f"  {skill_id:<{max_id_len}} → {resolved_status}{reason_str}")
        else:
            print(f"  {skill_id:<{max_id_len}} → resolved")

    print(f"\nall_available={all_available}")

    # Step 4: Inspect compatibility selection metadata. It is not a local router.
    print("\nCompatibility selection hints:")
    for skill in pack_skills:
        contract = Agently.skills_executor.inspect_skills(skill["skill_id"])
        card = contract.get("card", {})
        hints = card.get("activation_hints", {})
        keywords = hints.get("keywords", [])
        invocation_names = list(dict.fromkeys(hints.get("invocation_names", [])))
        print(f"  {skill['skill_id']}:")
        print(f"    keywords:         {keywords}")
        print(f"    invocation_names: {invocation_names[:4]}")

    if not any(s.get("activation_hints", {}).get("keywords") for s in
               [Agently.skills_executor.inspect_skills(s["skill_id"]).get("card", {})
                for s in pack_skills]):
        print(
            "\nNote: these guidance-only skills carry no compatibility keywords.\n"
            "The activation_hints payload key is retained by the released facade only;\n"
            "do not use it for local routing or as execution evidence."
        )


if __name__ == "__main__":
    main()
