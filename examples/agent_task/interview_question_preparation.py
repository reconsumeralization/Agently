from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from dotenv import find_dotenv, load_dotenv

from agently import Agently


DEFAULT_TARGET_INPUTS = ["Karpathy from Anthropic", "Agently的作者莫欣（Maplemx）"]
PROGRESS_MODEL_KEY = "ollama-qwen2.5-progress"
TASK_MODEL_KEY = "task-main"
DEFAULT_AUDIENCE = [
    "AI application developers",
    "technical founders evaluating AI organizations, products, or works",
    "readers of blog-style founder, builder, or creator interviews",
]
DEFAULT_INTERVIEW_GOAL = (
    "Prepare an evidence-backed blog-style interview preparation brief for the specified person or people. "
    "Use the supplied organization/work, original name, and aliases as search context while preserving "
    "the user's original target wording. The output is for a published article or long-form conversation, "
    "not for hiring, recruiting, or candidate evaluation."
)


def _split_target_values(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare an evidence-backed blog-style interview brief for arbitrary people, "
            "using organization/work plus original name and aliases as article context."
        )
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help=(
            "Interview targets such as 'Karpathy from Anthropic' or 'Agently的作者莫欣（Maplemx）'. "
            "Natural-language multi-target text is passed to the model for parsing."
        ),
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Add one interview target. Can be repeated.",
    )
    parser.add_argument(
        "--workspace",
        default=os.getenv("AGENT_TASK_WORKSPACE", ""),
        help="Workspace directory. Defaults to .agently/tasks/interview-question-preparation-<hash>.",
    )
    parser.add_argument(
        "--output-file",
        default=os.getenv("AGENT_TASK_OUTPUT_FILE", ""),
        help="Workspace-relative Markdown output path. Defaults to outputs/blog_interview_questions_<hash>.md.",
    )
    parser.add_argument(
        "--audience",
        action="append",
        default=[],
        help="Audience line. Can be repeated.",
    )
    parser.add_argument(
        "--angle",
        default=os.getenv("AGENT_TASK_INTERVIEW_GOAL", DEFAULT_INTERVIEW_GOAL),
        help="Intended blog interview angle or preparation goal.",
    )
    parser.add_argument(
        "--min-questions",
        type=int,
        default=int(os.getenv("AGENT_TASK_MIN_QUESTIONS", "12")),
        help="Minimum number of questions expected in the final file.",
    )
    return parser.parse_args(argv)


def _target_slug(text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"target-{digest}"


def build_raw_interview_setup(args: argparse.Namespace) -> tuple[list[str], str, str, Path]:
    raw_targets = _split_target_values([*args.target, *args.targets])
    if not raw_targets:
        env_targets = os.getenv("AGENT_TASK_INTERVIEW_TARGETS", "")
        raw_targets = _split_target_values([env_targets]) if env_targets else list(DEFAULT_TARGET_INPUTS)
    slug_source = "|".join(raw_targets)
    target_slug = _target_slug(slug_source)
    task_id = f"interview_question_preparation_{target_slug.replace('-', '_')}"
    output_file = args.output_file.strip() or f"outputs/blog_interview_questions_{target_slug}.md"
    workspace_dir = Path(args.workspace or f".agently/tasks/interview-question-preparation-{target_slug}").resolve()
    return raw_targets, task_id, output_file, workspace_dir


async def parse_targets_with_model(
    agent: Any,
    *,
    raw_targets: list[str],
    audience: list[str],
    angle: str,
    output_file: str,
) -> dict[str, Any]:
    print("[PROGRESS] Parsing blog interview target inputs with the main model.")
    parsed = await (
        agent.create_request(model_key=TASK_MODEL_KEY)
        .input(
            {
                "raw_target_inputs": raw_targets,
                "audience": audience,
                "interview_goal": angle,
                "output_file": output_file,
                "examples": [
                    "Karpathy from Anthropic",
                    "Agently的作者莫欣（Maplemx）",
                ],
                "instruction": (
                    "Parse the raw blog-interview target text into interview targets. Do not translate names or rewrite "
                    "the user's language. Use organization/work, role/relation, original name, aliases, display label, "
                    "and search hints only when the input supports them. A single raw input may contain multiple "
                    "targets, for example separated by punctuation or natural language."
                ),
            }
        )
        .output(
            {
                "targets": [
                    {
                        "raw_input": (str, "Exact raw text fragment this target came from", True),
                        "organization_or_work": (str, "Organization, product, project, or representative work if supplied"),
                        "role_or_relation": (str, "Role or relation phrase if supplied, such as author, CEO, maintainer"),
                        "original_name": (str, "Person name exactly as supplied, preserving language", True),
                        "aliases": [(str, "Alias, handle, alternate spelling, or disambiguation label")],
                        "display_label": (str, "Human-readable label preserving the supplied language", True),
                        "search_hints": [(str, "Search phrase that combines name, alias, organization, or work")],
                    }
                ],
                "parse_notes": (str, "Concise explanation of ambiguous parsing choices"),
            },
            format="json",
        )
        .async_start(max_retries=2, raise_ensure_failure=False)
    )
    targets = parsed.get("targets") if isinstance(parsed, dict) else None
    if not isinstance(targets, list) or not targets:
        raise RuntimeError(f"Model target parsing failed. Raw inputs: {raw_targets!r}; parsed={parsed!r}")
    interview_input = {
        "targets": targets,
        "interview_goal": angle,
        "output_file": output_file,
        "audience": audience,
        "model_parse_notes": parsed.get("parse_notes", "") if isinstance(parsed, dict) else "",
        "parsing_contract": {
            "preserve_original_name_language": True,
            "parser": "model-owned structured parsing through model_pool key task-main",
            "no_python_semantic_parsing": True,
        },
    }
    return interview_input


def _target_labels(targets: list[dict[str, Any]]) -> list[str]:
    return [str(target.get("display_label") or target.get("raw_input") or target.get("original_name")) for target in targets]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _mentions_any(text: str, values: list[str]) -> bool:
    normalized_text = text.lower()
    return any(value and value.lower() in normalized_text for value in values)


def _collect_target_inputs_from_prompt() -> list[str]:
    if not sys.stdin.isatty():
        return []
    default_text = "，".join(DEFAULT_TARGET_INPUTS)
    print("[INPUT] Enter interview targets. You can use examples like:")
    print("[INPUT]   Karpathy from Anthropic，Agently的作者莫欣（Maplemx）")
    print(f"[INPUT] Press Enter to use the default: {default_text}")
    try:
        raw_targets = input("[INPUT] Targets: ").strip()
    except EOFError:
        return []
    if not raw_targets:
        return []
    return [raw_targets]


def print_startup_summary(
    *,
    interview_input: dict[str, Any],
    task_id: str,
    output_file: str,
    workspace_dir: Path,
    provider: str,
    task_model_key: str,
    progress_model_key: str,
) -> None:
    print("\n[SETUP] Blog interview preparation")
    print(f"[SETUP] Task id: {task_id}")
    print(f"[SETUP] Workspace: {workspace_dir}")
    print(f"[SETUP] Output file: {workspace_dir / output_file}")
    print(f"[SETUP] Main model: provider={provider}, model_key={task_model_key}")
    print(f"[SETUP] Progress model key: {progress_model_key}")
    print("[SETUP] Parsed targets:")
    for index, target in enumerate(cast(list[dict[str, Any]], interview_input["targets"]), start=1):
        aliases = ", ".join(_string_list(target.get("aliases"))) or "none"
        organization_or_work = str(target.get("organization_or_work") or "not supplied")
        role_or_relation = str(target.get("role_or_relation") or "not supplied")
        print(
            f"[SETUP]   {index}. raw='{target['raw_input']}', organization/work='{organization_or_work}', "
            f"role='{role_or_relation}', original_name='{target['original_name']}', aliases='{aliases}'"
        )
    print("[SETUP] Starting AgentTaskLoop for a blog/media interview brief. Progress lines are generated by the configured progress model.\n")


def print_result_summary(summary: dict[str, Any], file_text: str) -> None:
    if summary.get("task_status") == "completed":
        print("\n[RESULT] Blog interview preparation accepted")
    elif summary.get("output_file_exists"):
        print("\n[RESULT] Blog interview preparation produced a partial artifact")
    else:
        print("\n[RESULT] Blog interview preparation did not produce an accepted artifact")
    print(f"[RESULT] Status: {summary['task_status']}")
    print(f"[RESULT] Accepted: {summary['accepted']}")
    print(f"[RESULT] Artifact status: {summary['artifact_status']}")
    if summary.get("terminal_reason"):
        print(f"[RESULT] Terminal reason: {summary['terminal_reason']}")
    print(f"[RESULT] Output file: {summary['output_file']}")
    print(f"[RESULT] Questions counted: {summary['question_count']}")
    print(f"[RESULT] Sources visible: {summary['has_sources']}")
    print(f"[RESULT] Interview angle visible: {summary['has_interview_angle']}")
    print(f"[RESULT] Targets mentioned: {summary['mentions_all_targets']}")
    print(f"[RESULT] Workspace checkpoints: {summary['workspace_checkpoint_count']}")
    print(f"[RESULT] Observed action history entries: {summary['action_log_count']}")
    print(f"[RESULT] Stream trace: {summary['stream_trace_file']}")
    preview_lines = [line for line in file_text.splitlines() if line.strip()][:18]
    if preview_lines:
        print("[RESULT] File preview:")
        for line in preview_lines:
            print(f"[RESULT]   {line[:220]}")
    print("[RESULT] Run summary JSON:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def configure_agent_model_pool(agent: Any, *, temperature: float = 0.0) -> tuple[str, str, str]:
    load_dotenv(find_dotenv(usecwd=True))
    configured = os.getenv("AGENT_TASK_MODEL_PROVIDER", "").strip().lower()
    if configured in {"deepseek", "ollama"}:
        provider = configured
    elif os.getenv("DEEPSEEK_API_KEY"):
        provider = "deepseek"
    else:
        provider = "ollama"

    progress_model_key = os.getenv("AGENT_TASK_PROGRESS_MODEL_KEY", PROGRESS_MODEL_KEY).strip() or PROGRESS_MODEL_KEY
    configured_model_pool = agent.settings.get("model_pool", {}) or {}
    configured_profiles = agent.settings.get("model_profiles", {}) or {}
    configured_key_pools = agent.settings.get("api_key_pools", {}) or {}
    model_pool = cast(dict[str, Any], dict(configured_model_pool) if isinstance(configured_model_pool, dict) else {})
    model_profiles = cast(dict[str, Any], dict(configured_profiles) if isinstance(configured_profiles, dict) else {})
    api_key_pools = cast(dict[str, Any], dict(configured_key_pools) if isinstance(configured_key_pools, dict) else {})

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY. Set it or run with AGENT_TASK_MODEL_PROVIDER=ollama.")
        model_pool[TASK_MODEL_KEY] = "agent-task-deepseek-main"
        model_profiles["agent-task-deepseek-main"] = {
            "provider": "OpenAICompatible",
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat"),
            "model_type": "chat",
            "api_key_pool": "agent-task-deepseek",
            "request_options": {"temperature": temperature},
        }
        api_key_pools["agent-task-deepseek"] = {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "primary", "value": api_key}],
        }
    else:
        model_pool[TASK_MODEL_KEY] = "agent-task-ollama-main"
        model_profiles["agent-task-ollama-main"] = {
            "provider": "OpenAICompatible",
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
            "model_type": "chat",
            "api_key_pool": "agent-task-ollama",
            "request_options": {"temperature": temperature},
        }
        api_key_pools["agent-task-ollama"] = {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "local", "value": os.getenv("OLLAMA_API_KEY", "ollama")}],
        }

    model_pool[progress_model_key] = "agent-task-ollama-progress"
    model_profiles["agent-task-ollama-progress"] = {
        "provider": "OpenAICompatible",
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "model": os.getenv("AGENT_TASK_PROGRESS_MODEL", "qwen2.5:7b"),
        "model_type": "chat",
        "api_key_pool": "agent-task-ollama",
        "request_options": {"temperature": float(os.getenv("AGENT_TASK_PROGRESS_TEMPERATURE", "0.2"))},
    }
    api_key_pools.setdefault(
        "agent-task-ollama",
        {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "local", "value": os.getenv("OLLAMA_API_KEY", "ollama")}],
        },
    )

    agent.settings.set("model_pool", model_pool)
    agent.settings.set("model_profiles", model_profiles)
    agent.settings.set("api_key_pools", api_key_pools)
    agent.settings.set(
        "skills.runtime.stage_model_keys",
        {
            "planner": TASK_MODEL_KEY,
            "reason": TASK_MODEL_KEY,
            "verifier": TASK_MODEL_KEY,
            "finalizer": TASK_MODEL_KEY,
        },
    )
    agent.settings.set("action.planning_model_key", TASK_MODEL_KEY)
    agent.activate_model(TASK_MODEL_KEY)
    return provider, TASK_MODEL_KEY, progress_model_key


async def main(argv: list[str] | None = None):
    args = parse_args(argv)
    if not args.target and not args.targets:
        prompted_targets = _collect_target_inputs_from_prompt()
        if prompted_targets:
            args.targets = prompted_targets
    raw_targets, task_id, output_file, workspace_dir = build_raw_interview_setup(args)
    min_questions = max(1, int(args.min_questions))
    audience = args.audience or DEFAULT_AUDIENCE

    workspace_dir.mkdir(parents=True, exist_ok=True)
    registry_root = workspace_dir / "skills-registry"
    skill_src = Path(__file__).resolve().parent / "skills" / "interview-question-preparer"
    Agently.skills_executor.configure(registry_root=str(registry_root), allowed_trust_levels=["local"])
    Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)

    agent = Agently.create_agent("agent-task-interview-question-preparation").use_workspace(workspace_dir)
    provider, task_model_key, progress_model_key = configure_agent_model_pool(agent, temperature=0.0)
    interview_input = await parse_targets_with_model(
        agent,
        raw_targets=raw_targets,
        audience=audience,
        angle=str(args.angle),
        output_file=output_file,
    )
    target_labels = _target_labels(cast(list[dict[str, Any]], interview_input["targets"]))
    print_startup_summary(
        interview_input=interview_input,
        task_id=task_id,
        output_file=output_file,
        workspace_dir=workspace_dir,
        provider=provider,
        task_model_key=task_model_key,
        progress_model_key=progress_model_key,
    )
    agent.settings.set("skills.capability_discovery.model_assisted", True)
    agent.configure_skill_capabilities(
        auto_load={
            "web_search": "allow",
            "web_browse": "allow",
            "workspace_read": "allow",
            "workspace_write": "allow",
        },
        workspace_root=str(workspace_dir),
        search={
            "backend": os.getenv("SEARCH_BACKEND", "auto"),
            "refresh_ddgs": "allow",
        },
    )
    workspace = agent.workspace
    if workspace is None:
        raise RuntimeError("Workspace was not initialized.")

    agent.set_agent_prompt(
        "system",
        (
            "You prepare blog/media interview briefs from public evidence. Follow the selected Skill guidance. "
            "This is not a hiring interview, recruiting screen, or candidate evaluation. "
            "Do not treat the task as complete until the requested workspace file is written."
        ),
    )
    agent.use_skills(["interview-question-preparer"], mode="required")

    await workspace.ingest(
        content=interview_input,
        collection="observations",
        kind="interview_target",
        summary=f"Interview targets: {', '.join(target_labels)}",
        scope={"task_id": task_id},
        source={"type": "example_input", "phase": "target"},
    )

    agent_task_options = {
        "request_timeout_seconds": 60,
        "stream_progress": True,
        "stream_progress_background": True,
        "stream_snapshots": True,
        "progress_model_key": progress_model_key,
    }

    task = agent.create_task(
        task_id=task_id,
        goal=(
            "Use the interview-question-preparer Skill to prepare a blog-style interview preparation brief for these target "
            f"inputs exactly as supplied: { json.dumps(target_labels, ensure_ascii=False) }. The structured target "
            "context includes organization_or_work, original_name, and aliases. Preserve the user's original name "
            f"language in the final file. The final Markdown file must be written to { output_file }. The file must "
            "contain source notes, a story/interview angle, sufficiency reflection, grouped blog interview questions, "
            "and explicit target-specific questions. Do not frame the output as a job interview, hiring evaluation, "
            "candidate screen, or recruiting guide."
        ),
        success_criteria=[
            "The model used public search or browse evidence for the specified organization/work, original names, and aliases.",
            "The task reflected on whether the information was sufficient before finalizing.",
            f"The final Markdown file { output_file } exists in Workspace.",
            f"The file includes source notes or URLs, a blog/story interview angle, and at least { min_questions } interview questions.",
            "The file preserves each target's original name language and uses aliases as disambiguation/search context rather than silently renaming the target.",
            "Questions cover the target's organization/work context, public reputation, technical or creative choices, adoption/community/business context, article-worthy tension, and future direction where relevant.",
            "Questions are written for a blog/media interview audience, not for hiring, recruiting, or candidate evaluation.",
            "Questions include target-specific prompts for every supplied target, plus comparative article questions when multiple targets are supplied.",
            "After writing the file, the execution evidence includes a file readback or validation checklist for the final Markdown content.",
        ],
        workspace=workspace_dir,
        max_iterations=4,
        limits={"max_model_requests": 18, "max_seconds": 280, "max_no_progress_seconds": 100},
        options={
            "agent_task": agent_task_options,
            "routes": {"skills": {"effort": "react"}},
        },
    )

    output_dir = workspace_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    stream_trace_path = output_dir / "blog_interview_preparation_stream.jsonl"
    stream_items = []
    try:
        with stream_trace_path.open("w", encoding="utf-8") as trace_file:
            async for item in task.stream():
                stream_items.append(item)
                trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
                trace_file.flush()
                value = item.value if isinstance(item.value, dict) else {}
                message = str(value.get("message") or "")
                stream_kind = (item.meta or {}).get("stream_kind")
                if stream_kind == "progress" and message:
                    print(f"[PROGRESS] {message}", flush=True)
                elif stream_kind == "snapshot":
                    stage = value.get("stage") or item.path.rsplit(".", 1)[-1]
                    print(f"[SNAPSHOT] {stage}: {message}", flush=True)
        result = await task.run()
    except Exception:
        print(f"[STREAM_TRACE] {stream_trace_path}", flush=True)
        raise
    meta = await task.meta()

    output_path = workspace_dir / output_file
    file_exists = output_path.is_file()
    file_text = output_path.read_text(encoding="utf-8") if file_exists else ""
    question_count = file_text.count("?") + file_text.count("？")
    has_sources = ("http://" in file_text) or ("https://" in file_text) or ("Source" in file_text) or ("来源" in file_text)
    has_angle = ("angle" in file_text.lower()) or ("采访角度" in file_text) or ("选题" in file_text)
    target_mention_checks = []
    for target in cast(list[dict[str, Any]], interview_input["targets"]):
        mention_values = [
            str(target.get("raw_input") or ""),
            str(target.get("original_name") or ""),
            *_string_list(target.get("aliases")),
        ]
        target_mention_checks.append(_mentions_any(file_text, mention_values))
    mentions_all_targets = all(target_mention_checks)
    has_alias_context = all(
        not _string_list(target.get("aliases")) or _mentions_any(file_text, _string_list(target.get("aliases")))
        for target in cast(list[dict[str, Any]], interview_input["targets"])
    )
    has_organization_or_work_context = all(
        bool(target.get("organization_or_work")) is False
        or _mentions_any(file_text, [str(target.get("organization_or_work") or "")])
        for target in cast(list[dict[str, Any]], interview_input["targets"])
    )
    replan_count = sum(1 for item in stream_items if item.path.endswith(".replan"))
    snapshot_count = sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "snapshot")
    progress_count = sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "progress")
    action_log_count = 0
    action_log_ids: set[str] = set()

    def record_action_entries(entries: Any) -> None:
        nonlocal action_log_count
        if isinstance(entries, dict):
            action_log_count += len(entries)
            action_log_ids.update(str(action_id) for action_id in entries)
        elif isinstance(entries, list):
            action_log_count += len(entries)
            for action_item in entries:
                if isinstance(action_item, dict):
                    action_log_ids.add(str(action_item.get("action_id") or action_item.get("name") or ""))

    def record_action_logs(logs: Any) -> None:
        if not isinstance(logs, dict):
            return
        record_action_entries(logs.get("action_logs", {}))
        route_logs = logs.get("route_logs", {})
        if isinstance(route_logs, dict):
            record_action_entries(route_logs.get("action_logs", {}))
        route_output = route_logs.get("output", {}) if isinstance(route_logs, dict) else {}
        route_history = route_output.get("history", []) if isinstance(route_output, dict) else []
        record_action_entries(route_history)

    for iteration in meta["iterations"]:
        logs = iteration.get("execution_meta", {}).get("logs", {})
        record_action_logs(logs)
    for stream_item in stream_items:
        value = stream_item.value if isinstance(stream_item.value, dict) else {}
        record_action_logs(value.get("logs", {}))
    ddgs_refresh_action_present = "refresh_ddgs_dependency" in action_log_ids
    action_registry = getattr(agent.action, "action_registry", None)
    if action_registry is not None and hasattr(action_registry, "has"):
        ddgs_refresh_action_present = ddgs_refresh_action_present or bool(action_registry.has("refresh_ddgs_dependency"))

    summary = {
        "provider": provider,
        "search_backend": os.getenv("SEARCH_BACKEND", "auto"),
        "task_model_key": task_model_key,
        "task_status": result["status"],
        "accepted": bool(result.get("accepted", result.get("status") == "completed")),
        "artifact_status": str(result.get("artifact_status") or ("accepted" if result.get("status") == "completed" else "partial")),
        "terminal_reason": str(result.get("reason") or ""),
        "target_inputs": [target["raw_input"] for target in cast(list[dict[str, Any]], interview_input["targets"])],
        "parsed_targets": interview_input["targets"],
        "output_file_exists": file_exists,
        "question_count": question_count,
        "has_sources": has_sources,
        "has_interview_angle": has_angle,
        "mentions_all_targets": mentions_all_targets,
        "has_alias_context": has_alias_context,
        "has_organization_or_work_context": has_organization_or_work_context,
        "replan_count": replan_count,
        "progress_event_count": progress_count,
        "snapshot_event_count": snapshot_count,
        "stream_trace_file": str(stream_trace_path),
        "workspace_context_items": max((item.get("context_item_count", 0) for item in meta["iterations"]), default=0),
        "progress_model_key": progress_model_key,
        "progress_model": os.getenv("AGENT_TASK_PROGRESS_MODEL", "qwen2.5:7b"),
        "workspace_checkpoint_count": len(await workspace.checkpoint_history(task_id)),
        "action_log_count": action_log_count,
        "ddgs_refresh_action_present": ddgs_refresh_action_present,
        "output_file": str(output_path),
    }
    print_result_summary(summary, file_text)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

# Expected key output from a real DeepSeek run on 2026-06-03:
# command:
# AGENT_TASK_WORKSPACE=.agently/tasks/blog-interview-semantics-check \
#   python examples/agent_task/interview_question_preparation.py
# task_status="completed"
# accepted=True
# artifact_status="accepted"
# output_file_exists=True
# question_count=20
# has_sources=True
# has_interview_angle=True
# mentions_all_targets=True
# has_alias_context=True for targets that include aliases
# has_organization_or_work_context=True for targets that include organization/work
# task_model_key="task-main"
# progress_model="qwen2.5:7b"
# action_log_count=14
# progress_event_count=2
# snapshot_event_count=4
# stream_trace_file points to a JSONL stream trace under the Workspace
#
# Skills contract note:
# The selected Skill is a standard SKILL.md without Agently-specific
# allowed-actions, allow-scripts, mcp, mcpServers, execution, or stages
# frontmatter. Search/Browse/Workspace read/write access is provided by host policy
# through agent.configure_skill_capabilities(...), not by the Skill itself.
