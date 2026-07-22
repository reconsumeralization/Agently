# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tool + MCP + sandbox Blocks example: settlement risk shaping."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping, cast
import sys

EXAMPLE_DIR = Path(__file__).resolve().parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from _business_ladder_runtime import (
    ROOT,
    BusinessCase,
    all_outputs,
    compile_case,
    emit,
    output_for,
    require_number,
    run_business_cases,
)
from agently import Agently
from agently.builtins.plugins.ActionExecutor.MCPActionExecutor import MCPActionExecutor


async def mcp_add(context: Mapping[str, Any]) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": "Calling MCP calculator add tool."})
    server_script = str((ROOT / "examples" / "action_runtime" / "_calculator_mcp_server.py").resolve())
    result = await MCPActionExecutor("add", server_script).execute(
        spec={"action_id": "add"},
        action_call={"action_input": {"first_number": 12.5, "second_number": 7.25}},
        policy={},
        settings=Agently.settings,
    )
    value = require_number(cast(dict[str, Any], result).get("result"), "mcp.add")
    return {
        "tool": "mcp.add",
        "result": value,
        "action_evidence": [{"action_id": "mcp.add", "status": "success", "result": value}],
    }


async def mcp_multiply(context: Mapping[str, Any]) -> dict[str, Any]:
    added = cast(dict[str, Any], output_for(context, "mcp_add") or {})
    await emit(context, {"type": "business.progress", "message": "Calling MCP calculator multiply tool."})
    server_script = str((ROOT / "examples" / "action_runtime" / "_calculator_mcp_server.py").resolve())
    result = await MCPActionExecutor("multiply", server_script).execute(
        spec={"action_id": "multiply"},
        action_call={"action_input": {"first_number": require_number(added.get("result"), "mcp.add"), "second_number": 3}},
        policy={},
        settings=Agently.settings,
    )
    value = require_number(cast(dict[str, Any], result).get("result"), "mcp.multiply")
    return {
        "tool": "mcp.multiply",
        "result": value,
        "action_evidence": [{"action_id": "mcp.multiply", "status": "success", "result": value}],
    }


async def sandbox_risk_check(context: Mapping[str, Any]) -> dict[str, Any]:
    multiplied = cast(dict[str, Any], output_for(context, "mcp_multiply") or {})
    total = require_number(multiplied.get("result"), "mcp.multiply")
    await emit(context, {"type": "business.progress", "message": "Running sandboxed Python risk shaping."})
    code = "\n".join(
        [
            f"total = {total!r}",
            "result = {",
            "    'settlement_total': total,",
            "    'requires_finance_review': total >= 50,",
            "    'risk_band': 'review' if total >= 50 else 'standard',",
            "}",
            "import json",
            "print(json.dumps(result))",
        ]
    )
    agent = Agently.create_agent()
    agent.enable_python(
        action_id="shape_settlement_risk",
        expose_to_model=False,
        sandbox="trusted_local",
    )
    sandbox_output = await agent.action.async_execute_action(
        "shape_settlement_risk",
        {"source_code": code},
    )
    output_data = cast(dict[str, Any], sandbox_output).get("data", {})
    shaped = json.loads(str(output_data.get("stdout", "{}")))
    return {
        "sandbox": "workspace_code_execution",
        "result": shaped,
        "action_evidence": [{"action_id": "shape_settlement_risk", "status": "success", "result": shaped}],
    }


async def deterministic_validation(context: Mapping[str, Any]) -> dict[str, Any]:
    outputs = all_outputs(context)
    shaped = cast(dict[str, Any], outputs.get("sandbox") or {}).get("result") or {}
    ok = shaped.get("settlement_total") == 59.25 and shaped.get("requires_finance_review") is True
    reason = "MCP calculator output was shaped by sandbox policy logic"
    await emit(context, {"type": "business.validation", "scenario": "validate_mcp_sandbox", "accepted": ok, "reason": reason})
    return {"ok": ok, "reason": reason, "validation_results": [{"validator": "validate_mcp_sandbox", "ok": ok, "reason": reason}]}


HANDLERS = {
    "mcp_add": mcp_add,
    "mcp_multiply": mcp_multiply,
    "sandbox_risk_check": sandbox_risk_check,
    "deterministic_validation": deterministic_validation,
}


def build_case() -> BusinessCase:
    return {
        "case_id": "03_tool_mcp_sandbox",
        "title": "MCP calculator tools plus Python sandbox risk shaping",
        "graph": compile_case(
            "blocks-business-mcp-sandbox",
            [
                {
                    "id": "mcp_add",
                    "plan_block_id": "mcp_tool_call",
                    "kind": "mcp_tool_call",
                    "capability_requirements": [{"need": "mcp"}],
                    "runtime_preferences": {"handler": "mcp_add"},
                },
                {
                    "id": "mcp_multiply",
                    "plan_block_id": "mcp_tool_call",
                    "kind": "mcp_tool_call",
                    "capability_requirements": [{"need": "mcp"}],
                    "runtime_preferences": {"handler": "mcp_multiply"},
                },
                {
                    "id": "sandbox",
                    "plan_block_id": "script_action",
                    "kind": "script_action",
                    "capability_requirements": [{"need": "python"}],
                    "runtime_preferences": {"handler": "sandbox_risk_check"},
                },
                {"id": "validate_mcp_sandbox", "plan_block_id": "validation", "kind": "validation", "runtime_preferences": {"handler": "deterministic_validation"}},
            ],
            [
                {"from": "mcp_add", "to": "mcp_multiply"},
                {"from": "mcp_multiply", "to": "sandbox"},
                {"from": "sandbox", "to": "validate_mcp_sandbox"},
            ],
            capability_resolution={"allowed_capabilities": ["mcp", "python"]},
        ),
        "handlers": HANDLERS,
    }


async def main() -> None:
    await run_business_cases([build_case()])


if __name__ == "__main__":
    asyncio.run(main())
