"""TEMPLATE — Standard SKILL.md single-shot label + host-side orchestration.

This file is the canonical reference for the default single_shot compatibility
label. It is NOT numbered and NOT meant to be a polished demo; it exists so the
host-owned orchestration boundary is unambiguous.

Run:
    python examples/agent_auto_orchestration/_TEMPLATE_standard_skill_orchestration.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

────────────────────────────────────────────────────────────────────────────────
WHAT CHANGED (read this first)
────────────────────────────────────────────────────────────────────────────────
OLD model (deprecated): a Skill was an executable workflow. `skill.yaml` declared
`stages` (model_plan -> model -> action), the SkillsExecutor ran those stages, and
an `action` stage reached back into the host to save a file / call a tool. The
host and the Skill were entangled.

CURRENT model (standard): a Skill is SKILL.md guidance plus indexed resources
and metadata.
  * The capability is defined by SKILL.md alone: frontmatter `name` / `description`
    (+ optional public metadata such as `keywords`) and a markdown body of
    instructions. No `skill.yaml`; a root-level skill.yaml/skill.json now
    makes install FAIL on purpose.
  * The default route label is `single_shot`: `run_skills_task(...)` builds a
    Blocks ExecutionPlan with `skill_activation` plus a `model_request` block,
    injects the SKILL.md body as instructions, and the model produces a
    structured result in one request. Multi-step compatibility labels lower to
    trusted `flow_segment` blocks instead of custom Skills-local workflow
    engines.
  * Side effects, persistence, durable waiting/resume, and approval policy stay
    with the owning Agently layer: host code, TriggerFlow, Action /
    ActionRuntime, and ExecutionResource.

So the migration recipe for every old `skill.yaml` staged Skill is:
  1. Move the stage `purpose` prose into the SKILL.md body as plain guidance.
  2. Use `output=` for simple single-shot results. If model-side decomposition is
     genuinely needed, select a host `effort=` / route option that lowers through
     Blocks instead of adding Skill-owned stage metadata.
  3. Lift side-effecting `action` stages OUT of the Skill into host code,
     Action/ActionRuntime, ExecutionResource, or TriggerFlow chunks.
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently, TriggerFlow
from examples.dynamic_task._shared import configure_model


# ═══════════════════════════════════════════════════════════════════════════════
# 1. The Skill IS the SKILL.md. This template intentionally uses guidance only.
#    Note the frontmatter: `name` (canonical display name; the skill_id is its
#    slug -> "incident-response-planner"), `description` (drives the decision card
#    and model_decision routing), and `keywords`. The body is what the model reads.
#    There is deliberately NO skill.yaml and NO execution metadata here because
#    this template demonstrates the default single_shot compatibility label.
# ═══════════════════════════════════════════════════════════════════════════════
SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "incident-response-planner"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Install the Skill from a standard directory (SKILL.md at its root).
#    install_skills() copies it into .agently/skills/<skill_id>/ and writes the
#    Agently-managed files under .agently/skills/<skill_id>/.agently/.
# ═══════════════════════════════════════════════════════════════════════════════
def install_skill(runtime_dir: Path) -> str:
    skill_src = SKILL_SOURCE

    Agently.skills_executor.configure(registry_root=str(runtime_dir / "registry"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])  # -> "incident-response-planner"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. The host owns orchestration. Persisting the document used to be an `action`
#    stage INSIDE the Skill; now it is a normal host step. (For the Agently-native
#    surface you would register_action(...) and/or gate it behind an
#    ExecutionResource approval policy — this is exactly the layer that wait /
#    approval belongs to now, not the Skill.)
# ═══════════════════════════════════════════════════════════════════════════════
def save_runbook(output_dir: Path, incident_id: str, plan: str, runbook: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"{incident_id.lower()}_{stamp}.md"
    path.write_text(
        f"# Incident {incident_id}\n\n## Response Plan\n\n{plan}\n\n## Runbook\n\n{runbook}\n",
        encoding="utf-8",
    )
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Compose Skill (guidance) + host step (side effect) with TriggerFlow.
#    Chunk A: run the Skill through the single_shot compatibility label, shaping
#             the result with output (this replaces the old per-stage schema).
#    Chunk B: take the structured result and run the host persistence step.
# ═══════════════════════════════════════════════════════════════════════════════
def build_flow(agent, skill_id: str, output_dir: Path) -> TriggerFlow:
    flow = TriggerFlow(name="incident-response")

    async def run_skill(data):
        alert = data.input
        execution = await agent.async_run_skills_task(
            alert,
            skills=[skill_id],
            mode="required",
            output={
                "plan": (str, "Structured incident response plan covering all 6 areas."),
                "runbook": (str, "Step-by-step on-call runbook with owners and verification."),
            },
        )
        if execution.status != "success":
            raise RuntimeError(f"Skill run did not succeed: {execution.status} / {execution.output}")
        await data.async_set_state("skill_status", execution.status)
        return execution.output  # -> {"plan": ..., "runbook": ...}

    async def persist(data):
        result = data.input or {}
        path = save_runbook(
            output_dir,
            incident_id="INC-2026-05-0421",
            plan=str(result.get("plan", "")),
            runbook=str(result.get("runbook", "")),
        )
        await data.async_set_state("document_path", str(path))
        await data.async_set_state("plan_chars", len(str(result.get("plan", ""))))
        await data.async_set_state("runbook_chars", len(str(result.get("runbook", ""))))
        return str(path)

    flow.to(run_skill).to(persist)
    return flow


async def main() -> None:
    configure_model(temperature=0.3)
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skill_template_"))
    output_dir = runtime_dir / "runbooks"

    skill_id = install_skill(runtime_dir)
    agent = Agently.create_agent("incident-commander")
    flow = build_flow(agent, skill_id, output_dir)

    alert = (
        "ALERT: api-gateway p99 latency 4.2s (SLO 800ms) for 12 min in eu-west-1. "
        "5xx rate 18%. Started right after release v2026.05.21. Checkout is degraded."
    )

    print("=" * 60)
    print("installed skill_id:", skill_id)
    print("route: skills (mode=required) -> single_shot Blocks label")
    execution = flow.create_execution()
    await execution.async_start(alert)
    state = await execution.async_close()
    print("skill status:", state.get("skill_status"))
    print(f"plan length: {state.get('plan_chars', 0):,} chars")
    print(f"runbook length: {state.get('runbook_chars', 0):,} chars")
    print("document saved:", state.get("document_path"))
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

# ────────────────────────────────────────────────────────────────────────────────
# Expected key output from a real run (shape; lengths vary by model):
#   installed skill_id: incident-response-planner
#   route: skills (mode=required) -> single_shot Blocks label
#   skill status: success
#   plan length: ~3,000-5,000 chars
#   runbook length: ~2,000-4,000 chars
#   document saved: /.../runbooks/inc-2026-05-0421_<stamp>.md
#
# Compare with the old 12_model_plan_incident_response.py: the three stages
# (analyze_incident / generate_runbook / save_runbook) are gone. analyze + generate
# collapsed into ONE single_shot Blocks-backed Skill request; save moved into
# the host TriggerFlow.
# ────────────────────────────────────────────────────────────────────────────────
