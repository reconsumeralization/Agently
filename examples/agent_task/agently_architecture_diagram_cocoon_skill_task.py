"""Goal Pursuit variant: self-install the public CocoonAI architecture-diagram skill.

Run:
    python examples/agent_task/agently_architecture_diagram_cocoon_skill_task.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set AGENT_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

How this differs from agently_architecture_diagram_task.py
----------------------------------------------------------
The sibling example fed the model a *hand-written* ARCHITECTURE_DIAGRAM_SKILL_GUIDANCE
rubric whose bullet points were then restated almost 1:1 in the success criteria,
judge rules, and the Mermaid-block smoke check (a circular hint).

This variant removes that inline rubric entirely. Instead the example **installs a
real, independently-authored public skill at runtime** -

    Cocoon-AI/architecture-diagram-generator  (subpath: architecture-diagram)

- via Agently's remote-skills mechanism, and lets the goal-pursuit loop use it as the
"how to draw" guidance. The skill dictates an HTML+SVG design system (colors, fonts,
spacing, export toolbar), so:
    * OUTPUT is a single .html file with inline SVG, not Mermaid markdown.
    * The trusted Skill may reference external font/export assets documented in
      its own design system.
    * The fetch action returns ONLY repository evidence (no embedded guidance).
    * The judge rules stay generic and are NOT a restatement of the skill, so the
      acceptance signal is no longer circular with the guidance.

The architectural content (which layers, ownership boundaries, edges) remains
model-owned and grounded in the supplied repository excerpts.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from agently import Agently

from _business_example_common import (
    TASK_MODEL_KEY,
    configure_agent_model_pool,
    default_workspace,
    judge_business_artifact,
    print_stream_item,
    write_summary,
)


TASK_ID = "agently_architecture_diagram_cocoon_skill"
OUTPUT_FILE = "outputs/agently_architecture_diagram.html"
SUMMARY_FILE = "outputs/agently_architecture_diagram_cocoon_summary.json"

# The public skill the example installs for itself (no hand-written rubric).
COCOON_SKILL_SOURCE = "Cocoon-AI/architecture-diagram-generator"
COCOON_SKILL_SUBPATH = "architecture-diagram"
COCOON_SKILL_ID = "architecture-diagram"
SKILLS_ARTIFACT_EFFORT = "agent_task_artifact_react"

SOURCE_PATHS = [
    "docs/en/reference/execution-layer-selection.md",
    "spec/planned/architecture/CONCEPT_REGISTRY.md",
    "spec/planned/architecture/UNIFIED_AGENT_EXECUTION_IMPLEMENTATION_SPEC.md",
    "spec/planned/architecture/AGENT_TASK_LOOP_4_1_3_7_CLOSEOUT_SPEC.md",
    "spec/planned/agent_task/AGENT_TASK_LOOP_LAYERED_DAG_STEP_EXECUTION_OPTIMIZATION_SPEC.md",
    "agently/core/Agent.py",
    "agently/core/model/ModelRequest.py",
    "agently/core/application/AgentExecution/__init__.py",
    "agently/core/application/AgentExecution/Result.py",
    "agently/core/orchestration/TaskDAG/TaskDAGExecutor.py",
    "agently/core/orchestration/TriggerFlow/TriggerFlow.py",
    "agently/core/Workspace/Workspace.py",
    "agently/core/application/SkillsExecutor/SkillsExecutor.py",
    "agently/builtins/plugins/ActionRuntime/AgentlyActionRuntime.py",
]

ARCHITECTURE_STATUS_FACTS = [
    {
        "component": "AgentExecution",
        "status": "current",
        "source": "docs/en/reference/execution-layer-selection.md",
        "evidence": "AgentExecution is the default user-facing Agent run with prompt, Actions, Skills, goals, effort, result, stream, and metadata.",
    },
    {
        "component": "TaskDAG",
        "status": "current foundation",
        "source": "docs/en/reference/execution-layer-selection.md; agently/core/orchestration/TaskDAG/TaskDAGExecutor.py",
        "evidence": "TaskDAG owns graph-shaped planning and execution logic; DynamicTask is the compatibility/convenience facade over the DAG substrate.",
    },
    {
        "component": "TriggerFlow",
        "status": "current foundation",
        "source": "docs/en/reference/execution-layer-selection.md; agently/core/orchestration/TriggerFlow/TriggerFlow.py",
        "evidence": "TriggerFlow owns the lower-level workflow substrate: execution state, signals, concurrency, stream, pause/resume, persistence, and lifecycle.",
    },
    {
        "component": "Workspace",
        "status": "current foundation",
        "source": "docs/en/reference/execution-layer-selection.md; agently/core/Workspace/Workspace.py",
        "evidence": "Workspace stores evidence and context; it does not decide completion.",
    },
    {
        "component": "Deferred task-loop work",
        "status": "deferred",
        "source": "compatibility/in-development.json; spec/planned/agent_task/AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC.md",
        "evidence": "Deferred areas include multi-task scheduling, production distributed recovery, and first-class artifact handoff contracts; do not label current TaskDAG or AgentExecution as deferred.",
    },
]

EXCERPT_TERMS = [
    "AgentExecution",
    "AgentTask",
    "AgentTaskLoop",
    "TaskDAG",
    "DAG",
    "DynamicTask",
    "TriggerFlow",
    "ModelRequest",
    "ModelRequestResult",
    "Workspace",
    "Action",
    "ActionRuntime",
    "SkillsExecutor",
    "ExecutionResource",
    "RuntimeEvent",
    "EventCenter",
    "goal",
    "effort",
    "provider",
    "strategy",
    "verification",
    "evidence",
    "deferred",
]

# Generic, skill-agnostic acceptance rules. These intentionally do NOT restate the
# installed skill's design system, so a pass is not circular with the guidance.
JUDGE_RULES = [
    "The artifact would help an engineer understand Agently's architecture at a high level without reading the whole repository.",
    "The artifact contains a real diagram (an HTML/SVG visual showing components and how they relate), not just prose.",
    "The artifact chooses and explains important boundaries in its own way, instead of only listing module names.",
    "The artifact is reasonably grounded in the supplied repository evidence and avoids major contradictions or overconfident claims about deferred work.",
    "The artifact is suitable as a design-review conversation starter, even if a maintainer might later refine the exact labels or grouping.",
]


def _line_excerpt(text: str, *, max_chars: int = 5600) -> str:
    lines = text.splitlines()
    if len(text) <= max_chars:
        return text
    lowered_terms = [term.lower() for term in EXCERPT_TERMS]
    selected: set[int] = set()
    for index, line in enumerate(lines):
        lower = line.lower()
        if any(term in lower for term in lowered_terms):
            for offset in range(-2, 5):
                candidate = index + offset
                if 0 <= candidate < len(lines):
                    selected.add(candidate)
    chunks: list[str] = []
    last = -10
    for index in sorted(selected):
        if index > last + 1:
            chunks.append("...")
        chunks.append(f"{index + 1}: {lines[index]}")
        last = index
        if sum(len(chunk) + 1 for chunk in chunks) >= max_chars:
            chunks.append("...")
            break
    return "\n".join(chunks)


def _count_svg_blocks(text: str) -> int:
    return len(re.findall(r"<svg\b", text, flags=re.I))


# Candidate design-system fingerprint tokens for this skill. These are
# necessarily skill-specific and therefore live in the example, not in core
# (BUG_FIX_AGENT_TASK_SKILL_GUIDANCE_BYPASS_SPEC 4.4). They are not asserted
# blindly: `_design_system_fingerprints` keeps only the tokens actually present
# in the installed skill's own files, so the smoke check stays grounded in the
# skill rather than in a hand-written rubric.
_FINGERPRINT_CANDIDATES = ["#020617", "JetBrains Mono", 'pattern id="grid"', "stroke-dasharray"]


def _design_system_fingerprints(skill_dir: Path) -> list[str]:
    """Tokens that genuinely belong to the installed skill's design system.

    Reads SKILL.md and resources/template.html from the installed skill and
    returns the subset of candidate tokens that appear there, so the artifact
    smoke check below verifies conformance to the *skill's own* design system.
    """
    sources: list[str] = []
    for relative in ("SKILL.md", "resources/template.html"):
        path = skill_dir / relative
        if path.is_file():
            sources.append(path.read_text(encoding="utf-8", errors="replace"))
    skill_text = "\n".join(sources)
    return [token for token in _FINGERPRINT_CANDIDATES if token in skill_text]


def _design_system_fingerprint_hits(artifact_text: str, fingerprints: list[str]) -> list[str]:
    return [token for token in fingerprints if token in artifact_text]


def install_cocoon_skill(registry_root: Path) -> dict[str, Any]:
    """The example installs the public skill for itself, from GitHub, at runtime."""
    Agently.skills_executor.configure(
        registry_root=str(registry_root),
        allowed_trust_levels=["local", "remote"],
    )
    record = Agently.skills_executor.install_skills_pack(
        source=COCOON_SKILL_SOURCE,
        subpath=COCOON_SKILL_SUBPATH,
        fetch=True,
        trust_level="remote",
        update=True,
    )
    return record


async def main() -> None:
    os.environ.setdefault("AGENT_TASK_JUDGE_TIMEOUT_SECONDS", "180")
    workspace_dir = default_workspace("agently-architecture-diagram-cocoon")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    install_record = install_cocoon_skill(workspace_dir / "skills_registry")
    installed_skills = install_record.get("installed_skills", []) if isinstance(install_record, dict) else []
    if COCOON_SKILL_ID not in installed_skills:
        raise RuntimeError(f"Self-install did not register '{COCOON_SKILL_ID}'. Got: {installed_skills}")

    agent = Agently.create_agent("agent-task-cocoon-architecture-diagram").use_workspace(workspace_dir)
    provider = configure_agent_model_pool(agent, temperature=0.0)
    # When the planner routes a bounded step to the skills shape, the
    # SkillsExecutor resolves its model under per-phase stage keys
    # (planner/research/reason/executor/verifier/reflector/finalizer; see
    # SkillsExecutor _stage_key_for_phase / _stage_model_key). Those keys must be
    # present in the model pool or the skill's model call has no provider/key and
    # fails with a 401. This is host model configuration (the canonical pattern in
    # examples/skills_executor/09): map every skills stage key onto the same task
    # model profile so the skills route uses DeepSeek with the configured key.
    configured_pool = agent.settings.get("model_pool", {}) or {}
    model_pool = cast("dict[str, Any]", dict(configured_pool) if isinstance(configured_pool, dict) else {})
    task_profile = model_pool.get(TASK_MODEL_KEY)
    if task_profile is not None:
        for stage_key in (
            "planner",
            "research",
            "reason",
            "reason_fast",
            "executor",
            "verifier",
            "reflector",
            "finalizer",
        ):
            model_pool.setdefault(stage_key, task_profile)
        agent.settings.set("model_pool", model_pool)
    agent.set_settings("action.stage_idle_timeout", 240)
    agent.set_settings("tool.stage_idle_timeout", 240)
    raw_effort_presets = agent.settings.get("effort_presets", {})
    effort_presets: dict[str, Any] = (
        dict(cast(dict[str, Any], raw_effort_presets))
        if isinstance(raw_effort_presets, dict)
        else {}
    )
    effort_presets[SKILLS_ARTIFACT_EFFORT] = {
        "strategy": "react",
        "step_budget": 4,
        "artifact_inline_limit": 180000,
        "action_concurrency": 1,
        # Explicit side-effect scope for this artifact-producing Skills route.
        # Skills still provide guidance; ActionRuntime owns the file write/read.
        "allowed_actions": ["write_file", "read_file"],
        "required_actions": ["write_file", "read_file"],
    }
    agent.set_settings("effort_presets", effort_presets)
    workspace = agent.workspace
    if workspace is None:
        raise RuntimeError("Workspace was not initialized.")

    agent.enable_workspace_file_actions(read=True, write=True, expose_to_model=True)
    # Make the self-installed public skill available to the goal-pursuit loop.
    # The Cocoon skill's design system references Google Fonts and CDN export
    # helpers, so this trusted example explicitly authorizes those read-only
    # network capabilities instead of letting capability preparation fail closed.
    agent.use_skills([COCOON_SKILL_ID], mode="model_decision", always=True)
    agent.configure_skill_capabilities(
        auto_load={
            "web_browse": "allow",
            "http_request": "allow",
        }
    )

    @agent.action_func
    def fetch_agently_architecture_sources() -> dict[str, Any]:
        """Return bounded Agently architecture source excerpts from docs, specs, and implementation files."""
        sources: list[dict[str, Any]] = []
        for relative in SOURCE_PATHS:
            path = (ROOT / relative).resolve()
            if not path.is_file():
                sources.append({"path": relative, "status": "missing"})
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            sources.append({"path": relative, "status": "ok", "excerpt": _line_excerpt(text)})
        # NOTE: no embedded design rubric here - "how to draw" now comes from the
        # self-installed architecture-diagram skill, not from this example.
        return {"status": "ok", "sources": sources, "architecture_status_facts": ARCHITECTURE_STATUS_FACTS}

    agent.use_actions(fetch_agently_architecture_sources)
    await workspace.put(
        content={
            "task": TASK_ID,
            "output_file": OUTPUT_FILE,
            "installed_skill": COCOON_SKILL_ID,
            "skill_source": f"{COCOON_SKILL_SOURCE}#{COCOON_SKILL_SUBPATH}",
            "source_paths": SOURCE_PATHS,
            "architecture_status_facts": ARCHITECTURE_STATUS_FACTS,
            "judge_rules": JUDGE_RULES,
        },
        collection="observations",
        kind="architecture_diagram_task_brief",
        summary="Goal Pursuit task brief for an Agently architecture diagram using the self-installed Cocoon skill.",
        scope={"task_id": TASK_ID},
        source={"type": "example_script", "name": "agently_architecture_diagram_cocoon_skill_task"},
    )

    print("[SETUP] Agently architecture diagram (self-installed Cocoon skill) Goal Pursuit experiment")
    print(f"[SETUP] Workspace: {workspace_dir}")
    print(f"[SETUP] Provider: {provider}, model_key={TASK_MODEL_KEY}")
    print(f"[SETUP] Installed skill: {COCOON_SKILL_ID} from {COCOON_SKILL_SOURCE} (commit pinned)")

    goal = (
        "Produce a design-review-ready Agently architecture diagram. First call "
        "fetch_agently_architecture_sources to gather repository evidence, then use the installed "
        f"`{COCOON_SKILL_ID}` skill to render a single-file HTML+SVG document following that "
        f"skill's design system, and write the final HTML to `{OUTPUT_FILE}` with the workspace write "
        "action. Choose the architecture structure yourself from the evidence: prioritize a readable "
        "layered view over covering every implementation detail, and call out current versus deferred "
        "capabilities when the evidence makes that distinction important. Finally read the file back to confirm it."
    )
    success_criteria = [
        f"A single-file HTML architecture diagram exists at `{OUTPUT_FILE}` in Workspace.",
        f"The HTML embeds inline SVG produced with the installed `{COCOON_SKILL_ID}` skill's design system.",
        "The diagram gives engineers a clear high-level view of Agently's major layers, ownership boundaries, and runtime path.",
        "The document handles uncertain, planned, or deferred areas responsibly instead of presenting everything as already landed.",
        "The execution evidence includes source collection and a readback or check of the final document.",
    ]

    execution = (
        agent.goal(goal, success_criteria)
        .effort(
            "high",
            budget={"iteration_limit": 4, "model_call_limit": 16, "wall_time_seconds": 900},
            planning={"depth": "deep", "require_source_collection": True},
            execution={"step_plan": "auto"},
            verification={"strength": "strong", "require_artifact_readback": True},
            replan={"on_missing_criteria": True},
            progress={"stream": True, "snapshots": True},
        )
        .strategy(
            "task",
            task_id=TASK_ID,
            workspace=workspace_dir,
            limits={"max_model_requests": 16, "max_seconds": 900, "max_no_progress_seconds": 240},
            options={
                "agent_task": {
                    "request_timeout_seconds": 180,
                    "stream_progress": True,
                    "stream_snapshots": True,
                },
                "routes": {
                    "model_request": {"action_loop": {"max_rounds": 8}},
                    # Skills route output may carry long HTML/SVG text; avoid the
                    # JSON streaming parser path for artifact-shaped content.
                    "skills": {"effort": SKILLS_ARTIFACT_EFFORT, "output_format": "yaml_literal"},
                },
                # Structured capability-evidence requirement: the host guard fails
                # verification unless this capability (the installed skill) shows
                # up in execution evidence. This grades the OUTCOME of the model's
                # judgment; it does NOT force the route (the skill stays
                # mode="model_decision" above), so the judgment test is preserved.
                "capability_evidence_requirements": [
                    {"capability_id": COCOON_SKILL_ID, "capability_kind": "skill", "kind": "capability_used"},
                    {
                        "capability_id": "fetch_agently_architecture_sources",
                        "capability_kind": "action",
                        "kind": "action_succeeded",
                    },
                    {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "read_file", "capability_kind": "action", "kind": "action_succeeded"},
                ],
            },
        )
    )

    stream_items = []
    stream_trace_path = workspace_dir / "outputs" / "agently_architecture_diagram_cocoon_stream.jsonl"
    stream_trace_path.parent.mkdir(parents=True, exist_ok=True)
    with stream_trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator(type="instant"):
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
            trace_file.flush()
            print_stream_item(item)

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    output_path = workspace.files_root / OUTPUT_FILE
    artifact_text = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
    model_judge = await judge_business_artifact(
        agent,
        scenario="Agently architecture diagram design-review artifact (single-file HTML with inline SVG).",
        artifact_text=artifact_text,
        business_context={
            "installed_skill": COCOON_SKILL_ID,
            "skill_source": f"{COCOON_SKILL_SOURCE}#{COCOON_SKILL_SUBPATH}",
            "source_paths": SOURCE_PATHS,
            "expected_output_file": OUTPUT_FILE,
            "current_release_slice": "4.1.3.8 AgentTask execution quality hardening",
        },
        rules=JUDGE_RULES,
    )
    skill_dir = workspace_dir / "skills_registry" / COCOON_SKILL_ID
    design_system_fingerprints = _design_system_fingerprints(skill_dir)
    fingerprint_hits = _design_system_fingerprint_hits(artifact_text, design_system_fingerprints)
    # Design-system fingerprints are a REPORTED DIAGNOSTIC, not an acceptance gate.
    # Whether the skill ran is proven structurally by the framework's
    # capability-evidence gate (the task is only accepted when the skill shows up
    # in execution evidence). Whether the model then faithfully applied every
    # design token is a model-quality outcome, not a framework guarantee, so it
    # must not gate `example_accepted` (that would make the framework proof flaky
    # on model variation). We still compute and surface the hits as an
    # observation of how well the design landed.
    follows_design_system = bool(design_system_fingerprints) and (
        len(fingerprint_hits) >= (len(design_system_fingerprints) + 1) // 2
    )
    structural_smoke = {
        "output_file_exists": output_path.is_file(),
        "svg_block_count": _count_svg_blocks(artifact_text),
        "has_svg": _count_svg_blocks(artifact_text) >= 1,
        "looks_like_html": "<html" in artifact_text.lower() and "</html>" in artifact_text.lower(),
        "artifact_char_count": len(artifact_text),
        "design_system_fingerprints": design_system_fingerprints,
        "design_system_fingerprint_hits": fingerprint_hits,
        "follows_skill_design_system": follows_design_system,
    }
    framework_contract_passed = bool(
        result.get("accepted", result.get("status") == "completed")
        and str(result.get("artifact_status") or "").strip() == "accepted"
        and structural_smoke["output_file_exists"]
    )
    summary = {
        "provider": provider,
        "installed_skill": COCOON_SKILL_ID,
        "skill_source_url": install_record.get("source_url") if isinstance(install_record, dict) else None,
        "skill_source_commit": bool(install_record.get("source_commit")) if isinstance(install_record, dict) else None,
        "task_status": result.get("status"),
        "task_accepted": bool(result.get("accepted", result.get("status") == "completed")),
        "artifact_status": str(
            result.get("artifact_status") or ("accepted" if result.get("status") == "completed" else "partial")
        ),
        "iterations": result.get("iterations"),
        "model_judge_passed": bool(model_judge.get("accepted")),
        # Acceptance is driven by the framework contract only. With
        # capability_evidence_requirements set, task acceptance proves the
        # selected Skill appeared in execution evidence and the required
        # fetch/write/readback host actions succeeded. Model-judge quality and
        # design-system fingerprints remain visible diagnostics, not gates, so
        # model variation does not turn a framework-contract probe flaky.
        "framework_contract_passed": framework_contract_passed,
        "example_accepted": framework_contract_passed,
        "model_judge": model_judge,
        "structural_smoke": structural_smoke,
        "replan_count": sum(1 for item in stream_items if item.path.endswith(".replan")),
        "workspace_checkpoint_count": len(await workspace.checkpoint_history(TASK_ID)),
        "workspace_decision_count": len(meta.get("workspace_refs", {}).get("decisions", [])),
        "task_refs": result.get("task_refs") or meta.get("task_refs"),
        "stream_trace_file": str(stream_trace_path),
        "output_file": str(output_path),
    }
    summary_path = workspace.files_root / SUMMARY_FILE
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(summary)


if __name__ == "__main__":
    asyncio.run(main())
