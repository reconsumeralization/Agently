"""TEMPLATE — Standard SKILL.md guidance + host-side orchestration.

This file is the canonical "new pattern" reference for the Skills rewrite. It is
NOT numbered and NOT meant to be a polished demo; it exists so the new layering
is unambiguous before the numbered examples are migrated to it.

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

NEW model (standard): a Skill is ONLY guidance.
  * The capability is defined by SKILL.md alone: frontmatter `name` / `description`
    (+ optional `keywords`) and a markdown body of instructions. No `skill.yaml`,
    no stages, no embedded actions. A root-level skill.yaml/skill.json now makes
    install FAIL on purpose.
  * Running a Skill is a SINGLE model request: the full SKILL.md body is injected
    as instructions and the model produces a structured result in one shot. There
    is no per-stage DAG inside the Skill anymore.
  * Orchestration — side effects, persistence, tool/Action calls, waiting and
    approval — lives in the HOST (TriggerFlow / DynamicTask / register_action /
    ExecutionEnvironment policy), never inside the Skill.

So the migration recipe for every old staged Skill is:
  1. Move the stage `purpose` prose into the SKILL.md body as plain guidance.
  2. Express the structured output you want via `semantic_outputs=` on the run.
  3. Lift every `action` stage OUT of the Skill into a host TriggerFlow chunk.
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
# 1. The Skill IS the SKILL.md — guidance only.
#    Note the frontmatter: `name` (canonical display name; the skill_id is its
#    slug -> "incident-response-planner"), `description` (drives the decision card
#    and model_decision routing), and `keywords`. The body is what the model reads.
#    There is deliberately NO skill.yaml and NO stages/actions here.
# ═══════════════════════════════════════════════════════════════════════════════
SKILL_MD = """\
---
name: Incident Response Planner
description: >-
  Analyze an infrastructure incident alert and produce a structured response
  plan plus an executable on-call runbook. Use for incident, alert, on-call,
  and runbook requests.
keywords: [incident, alert, runbook, incident response, on call, SRE]
version: 1.0.0
---

# Incident Response Planner

You are an SRE incident commander. Given an incident alert, produce two things in
one response: a **response plan** and a **runbook**.

## Response plan
Cover all six areas, be specific and actionable, avoid generic advice:
1. Severity assessment (P0/P1/P2/P3) with justification.
2. Impact radius (which services, users, regions are affected).
3. Immediate mitigation actions (what to do right now).
4. Investigation steps (what to investigate and in what order).
5. Stakeholders to notify (teams, roles, external parties).
6. Expected resolution timeline (best case / worst case).

## Runbook
Convert the plan into a step-by-step checklist an on-call engineer can follow at
3 AM. Each step states: the action, the owner role (e.g. on-call SRE, database
team, security), the expected outcome, and a verification check. Include rollback
steps for any irreversible action.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Install the Skill from a standard directory (SKILL.md at its root).
#    install_skills() copies it into .agently/skills/<skill_id>/ and writes the
#    Agently-managed files under .agently/skills/<skill_id>/.agently/.
# ═══════════════════════════════════════════════════════════════════════════════
def install_skill(runtime_dir: Path) -> str:
    skill_src = runtime_dir / "incident-response-planner"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    Agently.skills_executor.configure(registry_root=str(runtime_dir / "registry"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])  # -> "incident-response-planner"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. The host owns orchestration. Persisting the document used to be an `action`
#    stage INSIDE the Skill; now it is a normal host step. (For the Agently-native
#    surface you would register_action(...) and/or gate it behind an
#    ExecutionEnvironment approval policy — this is exactly the layer that wait /
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
#    Chunk A: run the Skill as a single prompt-only request, shaping the result
#             with semantic_outputs (this replaces the old per-stage output_schema).
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
            semantic_outputs={
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
    print("route: skills (mode=required) -> single prompt-only request")
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
#   route: skills (mode=required) -> single prompt-only request
#   skill status: success
#   plan length: ~3,000-5,000 chars
#   runbook length: ~2,000-4,000 chars
#   document saved: /.../runbooks/inc-2026-05-0421_<stamp>.md
#
# Compare with the old 12_model_plan_incident_response.py: the three stages
# (analyze_incident / generate_runbook / save_runbook) are gone. analyze + generate
# collapsed into ONE prompt-only Skill request; save moved into the host TriggerFlow.
# ────────────────────────────────────────────────────────────────────────────────
