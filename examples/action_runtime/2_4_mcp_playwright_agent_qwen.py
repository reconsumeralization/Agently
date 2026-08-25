from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import Counter
from tempfile import TemporaryDirectory
from typing import Any

from dotenv import find_dotenv, load_dotenv

from agently import Agently

from _playwright_e2e_shared import (
    TODO_TITLE,
    assert_e2e,
    create_playwright_mcp_config,
    print_evidence,
    serve_todo_app,
)


PLAYWRIGHT_ACTION_IDS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_type",
    "browser_click",
]
PLAYWRIGHT_REF = re.compile(r"e[0-9]+")
PLAYWRIGHT_REF_IN_SNAPSHOT_LINE = re.compile(r"\[ref=(e[0-9]+)\]")


def configure_qwen() -> str:
    load_dotenv(find_dotenv())
    api_key = os.getenv("QWEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("QWEN_API_KEY is required for this example.")
    model = os.getenv("QWEN_MODEL", "qwen3-32b")
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv(
                "QWEN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            "model": model,
            "model_type": "chat",
            "auth": api_key,
            "request_retry": {"max_attempts": 1, "after_output": False},
            "request_options": {"enable_thinking": False},
        },
    )
    Agently.set_settings("debug", False)
    return model


def canonicalize_playwright_target(value: Any) -> str:
    """Project one model-selected snapshot line to Playwright's canonical ref."""
    target = str(value or "").strip()
    if PLAYWRIGHT_REF.fullmatch(target):
        return target
    matches = PLAYWRIGHT_REF_IN_SNAPSHOT_LINE.findall(target)
    if len(matches) != 1:
        raise ValueError(
            "target must be one Playwright ref such as e6 or one snapshot line "
            "containing exactly one [ref=e6] marker"
        )
    return matches[0]


def create_validating_execution_handler(action_trace: list[dict[str, Any]]):
    async def validating_execution_handler(context: dict[str, Any], request: dict[str, Any]):
        action = context["action"]
        settings = context["settings"]
        artifact_scope = context.get("artifact_scope")
        action_calls = request.get("action_calls", [])
        records: list[dict[str, Any]] = []

        for call in action_calls if isinstance(action_calls, list) else []:
            action_id = str(call.get("action_id", ""))
            model_input = dict(call.get("action_input") or {})
            executed_input = dict(model_input)
            try:
                if action_id in {"browser_type", "browser_click"}:
                    executed_input["target"] = canonicalize_playwright_target(
                        model_input.get("target")
                    )
            except ValueError as error:
                record = {
                    "action_id": action_id,
                    "status": "error",
                    "success": False,
                    "error": str(error),
                    "data": {"error": str(error)},
                }
            else:
                record = await action.async_execute_action(
                    action_id,
                    executed_input,
                    settings=settings,
                    purpose=str(call.get("purpose", f"Use {action_id}")),
                    policy_override=call.get("policy_override", {}),
                    source_protocol=str(call.get("source_protocol", "structured_plan")),
                    todo_suggestion=str(
                        call.get("todo_suggestion", call.get("next", ""))
                    ),
                    next_value=str(call.get("next", "")),
                    artifact_scope=(
                        artifact_scope if isinstance(artifact_scope, dict) else None
                    ),
                )
            action_trace.append(
                {
                    "action_id": action_id,
                    "model_input": model_input,
                    "executed_input": executed_input,
                    "status": record.get("status"),
                }
            )
            records.append(record)
        return records

    return validating_execution_handler


async def main() -> None:
    model = configure_qwen()
    agent = Agently.create_agent("playwright-autonomous-e2e")
    agent.set_agent_prompt(
        "system",
        "You are an autonomous browser E2E operator. Use only mounted Playwright "
        "Actions. Select elements from the latest observed accessibility snapshot, "
        "adapt after every Action result, and do not claim success until a final "
        "observed snapshot satisfies the task.",
    )
    agent.set_action_loop(
        max_rounds=8,
        concurrency=1,
        timeout=180,
    )

    action_trace: list[dict[str, Any]] = []
    model_events: Counter[str] = Counter()
    agent.register_action_execution_handler(
        create_validating_execution_handler(action_trace)
    )

    async def capture_event(event: Any) -> None:
        event_type = str(getattr(event, "event_type", ""))
        if event_type == "model.request_started":
            model_events[event_type] += 1

    hook_name = "playwright-autonomous-e2e-model-count"
    Agently.event_center.register_hook(capture_event, hook_name=hook_name)
    started = time.monotonic()

    with (
        serve_todo_app() as (base_url, state),
        TemporaryDirectory(prefix="agently_playwright_agent_") as output_dir,
    ):
        await agent.action.async_use_action_mcp(
            create_playwright_mcp_config(base_url, output_dir),
            default_policy={"network_mode": "enabled", "timeout_seconds": 180},
            side_effect_level="write",
            replay_safe=False,
        )
        execution = (
            agent.create_execution()
            .use_actions(PLAYWRIGHT_ACTION_IDS)
            .input(
                {
                    "authorized_app_url": base_url,
                    "e2e_goal": (
                        f"Create exactly one todo named {TODO_TITLE!r}, mark it "
                        "completed, then inspect the final page."
                    ),
                    "acceptance_rules": [
                        "Use Playwright Actions for every browser operation.",
                        "Select elements only from observed accessibility snapshots.",
                        "Do not stop until a final snapshot shows the todo completed.",
                    ],
                }
            )
            .output(
                {
                    "status": (
                        str,
                        "passed or failed based only on observed browser results",
                        True,
                    ),
                    "summary": (str, "concise observed final UI state", True),
                },
                format="json",
            )
        )
        try:
            report = await execution.async_get_data()
            meta = await execution.async_get_meta()
        finally:
            Agently.event_center.unregister_hook(hook_name)
            await Agently.execution_resource.async_release_scope("agent", agent.name)

        records = meta.get("logs", {}).get("action_logs", [])
        print_evidence(records, state, report)
        print("[AUTONOMOUS_ACTION_TRACE]")
        print(json.dumps(action_trace, ensure_ascii=False, indent=2))
        print(
            "[RUN_FACTS]",
            json.dumps(
                {
                    "model": model,
                    "planning_protocol": "structured_plan",
                    "model_requests": model_events["model.request_started"],
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "resources_after_release": len(
                        Agently.execution_resource.list(
                            scope="agent",
                            owner_id=agent.name,
                        )
                    ),
                },
                ensure_ascii=False,
            ),
        )

        assert_e2e(records, state)
        successful_ids = [
            str(record.get("action_id", ""))
            for record in records
            if record.get("status") in {"success", "partial_success"}
        ]
        assert "browser_type" in successful_ids, successful_ids
        assert successful_ids.count("browser_click") >= 2, successful_ids
        assert successful_ids[-1] == "browser_snapshot", successful_ids
        assert report.get("status") == "passed", report

    print("[HOST_ASSERTION] autonomous E2E passed")


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output from the observed 2026-08-25 qwen3-32b run:
# - The model independently selected navigate, snapshot, type, add-click,
#   snapshot, completion-click, and final snapshot Actions.
# - Every dispatched browser Action succeeded.
# - [SERVER_STATE] contains one completed "Review Agently E2E" todo.
# - [RUN_FACTS] reports resources_after_release=0.
# - [HOST_ASSERTION] autonomous E2E passed
#
# The host does not provide selectors or choose elements. It only projects a
# model-selected snapshot line containing one [ref=eN] marker to the canonical
# target="eN" protocol value. Playwright's live browser backend remains the
# authoritative owner that accepts or rejects the current ref.
