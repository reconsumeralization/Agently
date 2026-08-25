from __future__ import annotations

import asyncio
from tempfile import TemporaryDirectory
from typing import Any

from agently import Agently

from _playwright_e2e_shared import (
    TODO_TITLE,
    assert_e2e,
    create_playwright_mcp_config,
    print_evidence,
    serve_todo_app,
)


async def main() -> None:
    agent = Agently.create_agent("playwright-e2e")

    with (
        serve_todo_app() as (base_url, state),
        TemporaryDirectory(prefix="agently_playwright_mcp_") as output_dir,
    ):
        mcp_config = create_playwright_mcp_config(base_url, output_dir)

        # Browser tools mutate page and server state, so declare conservative effect
        # metadata and expose only explicit host-authored calls in this fixed E2E.
        await agent.action.async_use_action_mcp(
            mcp_config,
            default_policy={"network_mode": "enabled", "timeout_seconds": 180},
            side_effect_level="write",
            replay_safe=False,
        )

        records: list[dict[str, Any]] = []

        async def execute(action_id: str, action_input: dict[str, Any]) -> dict[str, Any]:
            record = await agent.action.async_execute_action(
                action_id,
                action_input,
                settings=agent.settings,
            )
            records.append(record)
            assert record.get("status") in {"success", "partial_success"}, record
            return record

        try:
            await execute("browser_navigate", {"url": base_url})
            await execute("browser_snapshot", {})
            await execute(
                "browser_type",
                {
                    "target": '[data-testid="todo-input"]',
                    "text": TODO_TITLE,
                },
            )
            await execute("browser_click", {"target": 'button[type="submit"]'})
            await execute("browser_snapshot", {})
            await execute("browser_click", {"target": 'input[type="checkbox"]'})
            final_snapshot = await execute("browser_snapshot", {})
        finally:
            await Agently.execution_resource.async_release_scope("agent", agent.name)

        report = {
            "status": "passed",
            "summary": "The canonical server state contains one completed todo.",
            "final_snapshot_contains_todo": TODO_TITLE in str(final_snapshot.get("data", "")),
        }
        print_evidence(records, state, report)
        assert report["final_snapshot_contains_todo"] is True
        assert_e2e(records, state)

    print("[HOST_ASSERTION] passed")


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output with Node.js 20+, npm, and Google Chrome:
# [ACTION_RECORDS] includes successful browser_navigate, browser_type,
# browser_click, and browser_snapshot calls.
# [SERVER_STATE] [{"title": "Review Agently E2E", "completed": true}]
# [HOST_ASSERTION] passed
#
# How it works:
# A local HTTP app owns the canonical todo state. Agently mounts Microsoft's
# Playwright MCP server as Actions backed by one shared MCP ExecutionResource.
# Host-authored E2E steps navigate, fill, click, and inspect through ordinary
# Action dispatch. The final assertion checks both Action evidence and canonical
# server state, then releases the agent-scoped MCP/browser session.
