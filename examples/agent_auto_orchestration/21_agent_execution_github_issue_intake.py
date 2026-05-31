"""AgentExecution issue intake through agent-owned local bash steps.

Run:
    python examples/agent_auto_orchestration/21_agent_execution_github_issue_intake.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    GitHub CLI (`gh`) installed and authenticated.

The example does not give the model a GitHub API URL, a fixed repo slug, or a
host-side issue-list function. The host gives AgentExecution a restricted bash
action that can run read-only `gh search repos` and `gh issue list` commands.
Step 1 searches GitHub and selects the official repository. Step 2 pulls the
latest open issue list through `gh`. The host reads the framework-owned
ActionRuntime records exposed by AgentExecution, validates the raw `gh` stdout,
and persists the result to Workspace.

Expected key output from one real DeepSeek run on 2026-05-31:
    provider=deepseek
    gh_available=True
    search_agent_used_bash_action=True
    issue_agent_used_bash_action=True
    selected_repo=AgentEra/Agently
    fetched_open_issue_count=5
    intake_execution_mode=task_step
    workspace_issue_ref_recorded=True
    workspace_context_item_count=1
    all_items_are_open_issues=True
    latest_issue_numbers=[280, 278, 277, 276, 274]
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from agently.core.AgentExecution import RuntimeStageStallError
from agently.utils import DataFormatter
from examples.dynamic_task._shared import configure_model


RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "github_issue_intake"
TASK_ID = "agently-public-issue-intake"
EXAMPLE_TIMEOUT_SECONDS = 180


async def collect_lineage_flags(execution) -> dict[str, bool | int]:
    flags: dict[str, bool | int] = {"lineage_ok": True, "route_selected": False, "action_events": 0}
    async for item in execution.get_async_generator(type="instant"):
        if item.path == "route.selected" and item.is_complete:
            flags["route_selected"] = True
        if item.source == "action":
            flags["action_events"] = int(flags["action_events"]) + 1
        meta = item.meta or {}
        if meta.get("execution_mode") != "task_step":
            flags["lineage_ok"] = False
        if (meta.get("lineage") or {}).get("task_id") != TASK_ID:
            flags["lineage_ok"] = False
    return flags


def gh_available() -> bool:
    return shutil.which("gh") is not None


def parse_json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = json.loads(value)
    else:
        raw_items = []
    return [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []


def normalize_issues(value: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for item in parse_json_list(value):
        labels = item.get("labels", [])
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        issues.append(
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": str(item.get("state") or "").lower(),
                "url": item.get("url"),
                "labels": [label.get("name") for label in labels if isinstance(label, dict)],
                "created_at": item.get("createdAt") or item.get("created_at"),
                "updated_at": item.get("updatedAt") or item.get("updated_at"),
                "author": author.get("login") if author else item.get("author"),
            }
        )
    return issues


def action_stdout(meta: dict[str, Any], command_token: str) -> str:
    logs = meta.get("logs", {})
    action_logs = logs.get("action_logs", []) if isinstance(logs, dict) else []
    seen: list[dict[str, Any]] = []
    for log in action_logs if isinstance(action_logs, list) else []:
        if not isinstance(log, dict):
            continue
        raw_digest = log.get("model_digest")
        digest: dict[str, Any] = raw_digest if isinstance(raw_digest, dict) else {}
        raw_instruction = digest.get("instruction")
        instruction: dict[str, Any] = raw_instruction if isinstance(raw_instruction, dict) else {}
        command_preview = str(instruction.get("preview") or "")
        seen.append(
            {
                "action_id": log.get("action_id"),
                "status": log.get("status"),
                "command": command_preview,
                "error": log.get("raw", {}).get("error") if isinstance(log.get("raw"), dict) else None,
            }
        )
        if not all(part in command_preview for part in command_token.split()):
            continue
        raw_data = log.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        stdout = data.get("stdout")
        if isinstance(stdout, str):
            return stdout
    raise ValueError(
        "No AgentExecution action stdout found for command token: "
        f"{command_token}. Seen action logs: {DataFormatter.sanitize(seen)}"
    )


async def main():
    provider = configure_model(temperature=0.0)
    Agently.set_settings("OpenAICompatible.stream_idle_timeout", 60.0)
    Agently.set_settings("OpenAIResponsesCompatible.stream_idle_timeout", 60.0)
    Agently.set_settings("response.materialization_idle_timeout", 60.0)
    if not gh_available():
        raise RuntimeError("GitHub CLI `gh` is required for this example.")
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)

    agent = Agently.create_agent("github-issue-intake").use_workspace(RUNTIME_ROOT)
    assert agent.workspace is not None
    agent.set_action_loop(max_rounds=1, timeout=45)
    agent.enable_shell(
        commands=["gh search repos", "gh issue list"],
        action_id="github_cli",
        desc=(
            "Run read-only GitHub CLI discovery commands. Allowed command prefixes are "
            "`gh search repos` and `gh issue list`. Use this capability to discover the "
            "official repository and pull open issues; do not use GitHub API URLs."
        ),
        timeout=30,
    )

    search_repo = (
        agent
        .input(
            {
                "task": (
                    "Find the official Agently Python framework repository. Agently is the exact "
                    "project name; reject generic 'agents' repositories and OpenAI Agents SDK repositories."
                ),
                "available_local_capability": (
                    "Use the model-callable bash action named `github_cli`. In this step, run only "
                    "`gh search repos ...`."
                ),
            }
        )
        .instruct(
            "Use the `github_cli` bash action once. Run a `gh search repos` command whose query contains "
            "the exact token Agently and returns JSON fields `fullName,url,description,owner,updatedAt`, "
            "with limit 5. Select the official repository only from that real command output."
        )
        .output(
            {
                "selected_repo": (str, "Repository full name selected from `gh search repos`, such as owner/name", True),
                "selected_repo_url": (str, "Repository URL from the selected search result", True),
                "gh_search_command": (str, "The exact `gh search repos` command that was executed", True),
                "why_official": (str, "Evidence from search result metadata that this is the official repo", True),
            },
            format="json",
        )
        .create_execution(
            mode="task_step",
            lineage={
                "task_id": TASK_ID,
                "iteration_id": "iter-1",
                "step_id": "agent-owned-gh-search",
            },
            limits={
                "max_model_requests": 3,
                "max_seconds": 90,
                "max_no_progress_seconds": 60,
            },
        )
    )
    search_stream_task = asyncio.create_task(collect_lineage_flags(search_repo))
    selected = await search_repo.async_get_data()
    search_stream = await search_stream_task
    search_meta = await search_repo.async_get_meta()

    repo_candidates = parse_json_list(action_stdout(search_meta, "gh search repos"))
    candidate_full_names = {str(item.get("fullName")) for item in repo_candidates}
    if str(selected.get("selected_repo")) not in candidate_full_names:
        raise ValueError(f"Agent selected repo outside gh search output: {selected.get('selected_repo')}")
    if str(selected.get("selected_repo")) != "AgentEra/Agently":
        raise ValueError(f"Agent selected an unexpected repository: {selected.get('selected_repo')}")

    issue_intake = (
        agent
        .input(
            {
                "selected_repo": selected,
                "task": "Pull the latest open GitHub issues for maintainer intake and summarize the real issue list.",
                "available_local_capability": (
                    "Use the model-callable bash action named `github_cli`. In this step, run only "
                    "`gh issue list ...`."
                ),
            }
        )
        .instruct(
            "Use the `github_cli` bash action once. Run `gh issue list` for the selected repository with "
            "`--state open`, `--limit 5`, and JSON fields "
            "`number,title,state,url,labels,createdAt,updatedAt,author`. Base your summary only on that "
            "real command output. Do not invent issue numbers, titles, labels, or URLs."
        )
        .output(
            {
                "gh_issue_command": (str, "The exact `gh issue list` command that was executed", True),
                "issue_count": (int, "Number of open issues copied from `gh issue list` output", True),
                "unprocessed_definition": (
                    str,
                    "Definition used for unprocessed issues in this example",
                    True,
                ),
                "intake_summary": (str, "Maintainer-facing summary of the fetched issue list", True),
                "highest_attention_numbers": (
                    [(int,)],
                    "Issue numbers from the fetched list that deserve earlier maintainer review",
                    True,
                ),
            },
            format="json",
        )
        .create_execution(
            mode="task_step",
            lineage={
                "task_id": TASK_ID,
                "iteration_id": "iter-2",
                "step_id": "agent-owned-gh-issue-intake",
                "parent_execution_id": search_repo.id,
            },
            limits={
                "max_model_requests": 3,
                "max_seconds": 90,
                "max_no_progress_seconds": 60,
            },
        )
    )
    issue_stream_task = asyncio.create_task(collect_lineage_flags(issue_intake))
    intake_result = await issue_intake.async_get_data()
    issue_stream = await issue_stream_task
    intake_meta_before_workspace = await issue_intake.async_get_meta()

    issues = normalize_issues(action_stdout(intake_meta_before_workspace, "gh issue list"))
    if not issues:
        raise ValueError("AgentExecution did not expose a parseable issue list from `gh issue list` action output.")
    if int(intake_result.get("issue_count") or 0) != len(issues):
        raise ValueError("Agent issue_count does not match the parsed issue list.")

    issue_batch = {
        "repo": selected["selected_repo"],
        "repo_url": selected["selected_repo_url"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "unprocessed_definition": intake_result["unprocessed_definition"],
        "capability": "agent-owned bash action: gh search repos + gh issue list",
        "gh_search_command": selected["gh_search_command"],
        "gh_issue_command": intake_result["gh_issue_command"],
        "issues": issues,
    }

    workspace_record = await issue_intake.async_record_workspace(
        collection="observations",
        kind="github_issue_intake",
        content={
            "source": {
                "repo": issue_batch["repo"],
                "repo_url": issue_batch["repo_url"],
                "retrieved_at": issue_batch["retrieved_at"],
                "capability": issue_batch["capability"],
                "gh_search_command": issue_batch["gh_search_command"],
                "gh_issue_command": issue_batch["gh_issue_command"],
            },
            "issues": issue_batch["issues"],
            "intake": intake_result,
        },
        summary=f"{TASK_ID} latest open GitHub issue intake",
        scope={"task_id": TASK_ID, "repo": issue_batch["repo"]},
        source={"step": "agent-owned-gh-issue-intake", "external_system": "github", "capability": "gh"},
        checkpoint=True,
    )
    intake_meta = await issue_intake.async_get_meta()
    context_pack = await agent.workspace.build_context(
        goal="",
        scope={"task_id": TASK_ID},
        budget={"chars": 1600},
        profile="software_dev",
    )

    all_items_are_open_issues = bool(issues) and all(item.get("state") == "open" for item in issues)
    workspace_refs = intake_meta.get("workspace_refs", {})
    context_items = context_pack.get("items", []) if isinstance(context_pack, dict) else []

    print(f"provider={provider}")
    print(f"gh_available={gh_available()}")
    print(f"search_agent_used_bash_action={int(search_stream['action_events']) > 0}")
    print(f"issue_agent_used_bash_action={int(issue_stream['action_events']) > 0}")
    print(f"selected_repo={selected['selected_repo']}")
    print(f"fetched_open_issue_count={len(issues)}")
    print(f"intake_execution_mode={intake_meta['execution_mode']}")
    print(f"workspace_issue_ref_recorded={workspace_record['record']['id'] in workspace_refs.get('observations', [])}")
    print(f"workspace_context_item_count={len(context_items)}")
    print(f"all_items_are_open_issues={all_items_are_open_issues}")
    print(f"search_stream_lineage_ok={search_stream['lineage_ok']}")
    print(f"issue_stream_lineage_ok={issue_stream['lineage_ok']}")
    print(f"latest_issue_numbers={[item['number'] for item in issues]}")
    print(f"workspace_record_id={workspace_record['record']['id']}")
    print(f"intake_summary={DataFormatter.sanitize(intake_result).get('intake_summary')}")


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=EXAMPLE_TIMEOUT_SECONDS))
    except RuntimeStageStallError as exc:
        raise SystemExit(
            "AgentExecution stalled with a framework diagnostic. "
            f"stage={exc.stage}, status={exc.status}, "
            f"last_progress_event={exc.last_progress_event}, timeout_seconds={exc.timeout_seconds}. "
            "Attach an EventCenter hook or temporarily call "
            "`agent.set_settings(\"debug\", True)` / `agent.set_settings(\"debug\", \"detail\")`, "
            "then remove debug code after the run is healthy."
        ) from exc
    except asyncio.TimeoutError as exc:
        raise SystemExit(
            "Timed out while waiting for the model-owned GitHub issue intake step. "
            "This usually means the provider, ActionRuntime planning, final response "
            "generation, or final text materialization stalled. For diagnosis, attach "
            "an EventCenter observation hook or temporarily call "
            "`agent.set_settings(\"debug\", True)` / `agent.set_settings(\"debug\", \"detail\")`, "
            "then remove debug code after the run is healthy."
        ) from exc
