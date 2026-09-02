from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from typing import Any

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.types.data import ActionPlanningProtocol


TEAM = [
    {"user_id": "u1", "level": "L2"},
    {"user_id": "u2", "level": "L3"},
    {"user_id": "u3", "level": "L2"},
]
EXPENSES = {
    "u1": [420.0, 180.0],
    "u2": [500.0, 700.0],
    "u3": [100.0, 150.0],
}
BUDGETS = {"L2": 500.0, "L3": 1000.0}
PROTOCOLS: tuple[ActionPlanningProtocol, ...] = (
    "structured_plan",
    "programmatic",
)


def configure_deepseek() -> None:
    load_dotenv(find_dotenv())
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for this example.")
    base_url = os.environ.get(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com/v1",
    ).rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": base_url,
            "model": os.environ.get(
                "DEEPSEEK_DEFAULT_MODEL",
                "deepseek-v4-flash",
            ),
            "model_type": "chat",
            "auth": api_key,
            "request_retry": {"max_attempts": 1, "after_output": False},
            "request_options": {
                "temperature": 0,
                "thinking": {"type": "disabled"},
            },
        },
    )
    Agently.set_settings("debug", False)


async def run_route(protocol: ActionPlanningProtocol) -> dict[str, Any]:
    agent = Agently.create_agent(name=f"action-loop-{protocol}")
    agent.set_settings("code_execution.providers", ["docker"])
    agent.set_settings("action.programmatic.max_parallel_subcalls", 4)
    agent.set_action_loop(
        planning_protocol=protocol,
        max_rounds=4,
        concurrency=4,
        timeout=180,
    )
    model_events: Counter[str] = Counter()
    action_calls: list[dict[str, Any]] = []
    active_action_calls = 0
    peak_action_calls = 0

    async def start_observed_call() -> None:
        nonlocal active_action_calls, peak_action_calls
        active_action_calls += 1
        peak_action_calls = max(peak_action_calls, active_action_calls)

    def finish_observed_call() -> None:
        nonlocal active_action_calls
        active_action_calls -= 1

    async def capture_event(event: Any) -> None:
        event_type = str(getattr(event, "event_type", ""))
        if event_type == "model.request_started":
            model_events[event_type] += 1

    hook_name = f"action-loop-example-{protocol}"
    Agently.event_center.register_hook(capture_event, hook_name=hook_name)

    async def list_team(department: str) -> list[dict[str, str]]:
        await start_observed_call()
        action_calls.append({"action_id": "list_team", "department": department})
        try:
            await asyncio.sleep(0.04)
            return list(TEAM) if department == "engineering" else []
        finally:
            finish_observed_call()

    async def get_expenses(user_id: str, quarter: str) -> list[dict[str, float]]:
        await start_observed_call()
        action_calls.append(
            {
                "action_id": "get_expenses",
                "user_id": user_id,
                "quarter": quarter,
            }
        )
        try:
            await asyncio.sleep(0.08)
            return [{"amount": amount} for amount in EXPENSES.get(user_id, []) if quarter == "Q3"]
        finally:
            finish_observed_call()

    async def get_budget(level: str) -> dict[str, float]:
        await start_observed_call()
        action_calls.append({"action_id": "get_budget", "level": level})
        try:
            await asyncio.sleep(0.08)
            return {"limit": BUDGETS[level]}
        finally:
            finish_observed_call()

    agent.register_action(
        name="list_team",
        desc="List team members for one department.",
        kwargs={"department": (str, "Exact department name")},
        func=list_team,
        returns=[
            {
                "user_id": (str, "User id"),
                "level": (str, "Employee level"),
            }
        ],
    )
    agent.register_action(
        name="get_expenses",
        desc="Return expense amounts for one exact user and quarter.",
        kwargs={
            "user_id": (str, "User id returned by list_team"),
            "quarter": (str, "Exact quarter, for example Q3"),
        },
        func=get_expenses,
        returns=[{"amount": (float, "One expense amount")}],
        concurrency_mode="parallel",
    )
    agent.register_action(
        name="get_budget",
        desc="Return the expense budget for one employee level.",
        kwargs={"level": (str, "Employee level returned by list_team")},
        func=get_budget,
        returns={"limit": (float, "Budget limit")},
        concurrency_mode="parallel",
    )

    prompt = (
        "For the engineering department in Q3, use the available read Actions "
        "to identify every user whose total expenses exceed the budget for "
        "their employee level. Return only a compact list of objects with "
        "user_id, spent, and limit. Do not guess records that Actions did not return."
    )
    turn = agent.input(prompt).output(
        [
            {
                "user_id": (str, "User id", True),
                "spent": (float, "Observed Q3 expense total", True),
                "limit": (float, "Observed budget limit", True),
            }
        ],
        format="json",
    )
    started = time.monotonic()
    try:
        records = await agent.async_get_action_result(
            prompt=turn.request.prompt,
            planning_protocol=protocol,
            max_rounds=4,
            concurrency=4,
            timeout=180,
            store_for_reply=True,
        )
        result = await turn.async_get_data()
    finally:
        Agently.event_center.unregister_hook(hook_name)
    return {
        "protocol": protocol,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "model_requests": model_events["model.request_started"],
        "business_action_calls": len(action_calls),
        "peak_business_action_concurrency": peak_action_calls,
        "action_statuses": [
            {
                "action_id": record.get("action_id"),
                "status": record.get("status"),
            }
            for record in records
        ],
        "result": result,
    }


async def main() -> None:
    configure_deepseek()
    comparison = {protocol: await run_route(protocol) for protocol in PROTOCOLS}
    print(json.dumps(comparison, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())


# Expected source-grounded business projection:
# - u1/spent=600/limit=500 and u2/spent=1200/limit=1000, with no u3 row.
# Model-owned results are observed rather than asserted. In the 2026-09-02
# DSv4-Flash acceptance sample, programmatic returned the exact projection while
# structured_plan omitted u1. Re-run both routes and compare business completion,
# peak concurrency, requests, provider usage and elapsed time; do not treat PTC
# as an automatic token or latency optimization.
