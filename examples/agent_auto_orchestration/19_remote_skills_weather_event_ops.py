"""Remote Skills + free weather MCP business acceptance example.

Run:
    python examples/agent_auto_orchestration/19_remote_skills_weather_event_ops.py

Environment:
    DEEPSEEK_API_KEY must be available in the shell or a .env file.
    Node.js and npx must be available for the free weather MCP server.
    Optional:
      DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
      DEEPSEEK_DEFAULT_MODEL=deepseek-chat
      EVENT_LOCATION="San Francisco"
      EVENT_LATITUDE=37.77493
      EVENT_LONGITUDE=-122.41942
      EVENT_DATE="2026-05-25"

Expected key output from a real DeepSeek + weather MCP run:
    installed_skills_before_plan=0
    weather_tools_registered=['check_service_status', 'get_alerts', 'get_current_conditions', 'get_forecast', 'search_location']
    weather_action_records=2 successful=2
    selected_remote_skills=['computer-use-agents', 'docx', 'mcp-builder', 'webapp-testing']
    source_discovered=4 source_installed=4
    execution_mode=runtime_chain
    decision=go
    weather_observation_count=8
    webapp_qa_count=16
    mcp_plan_count=8
    computer_use_check_count=23
    word_section_count=9
    risk_count=6

What this demonstrates
----------------------
This example is intentionally strict:

- all Skills are declared as remote sources through ``agent.use_skills(...)``;
- no inline ``SKILL.md`` content is created by the example;
- no business-path ``install_skills_pack(...)`` call is used;
- weather facts come from the real free ``@dangahagan/weather-mcp`` MCP server;
- the weather tool calls are model-owned ActionRuntime decisions;
- the final operations packet is model-generated with remote Skill guidance.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from pprint import pprint
from collections.abc import Mapping, Sequence
from typing import Any, cast

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


RUNTIME_ROOT = ROOT / ".example_runtime" / "remote_skills_weather_event_ops"

REMOTE_SKILLS: list[dict[str, Any]] = [
    {"source": "anthropics/skills", "subpath": "skills/webapp-testing", "trust_level": "remote"},
    {"source": "anthropics/skills", "subpath": "skills/mcp-builder", "trust_level": "remote"},
    {"source": "anthropics/skills", "subpath": "skills/docx", "trust_level": "remote"},
    {
        "source": "davila7/claude-code-templates",
        "subpath": "cli-tool/components/skills/ai-research/computer-use-agents",
        "trust_level": "remote",
    },
]

EXPECTED_SKILLS = {"webapp-testing", "mcp-builder", "docx", "computer-use-agents"}

WEATHER_MCP_CONFIG = {
    "mcpServers": {
        "weather": {
            "command": "npx",
            "args": ["-y", "@dangahagan/weather-mcp@latest"],
        }
    }
}

OPS_OUTPUTS = {
    "decision": (str, "One of: go, hold, conditional.", True),
    "weather_rationale": (str, "Decision rationale grounded in observed MCP weather data.", True),
    "weather_observations": ([str], "Specific weather facts observed through MCP tools.", True),
    "webapp_qa_checklist": ([str], "RSVP/check-in webapp QA checks guided by webapp-testing.", True),
    "mcp_integration_plan": ([str], "MCP integration steps guided by mcp-builder.", True),
    "computer_use_operator_checklist": ([str], "Supervised Computer Use operator checks and safeguards.", True),
    "word_ready_brief_sections": ([str], "Operations brief sections suitable for a Word document.", True),
    "onsite_action_plan": ([str], "Concrete onsite operating steps.", True),
    "risk_register": (
        [
            {
                "risk": (str, "Risk statement.", True),
                "mitigation": (str, "Mitigation action.", True),
                "owner": (str, "Owner role.", True),
            }
        ],
        "Operational risk register.",
        True,
    ),
    "skill_trace": ([str], "Remote Skill ids used and how each shaped the packet.", True),
}


def _configure_deepseek() -> bool:
    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("DEEPSEEK_API_KEY not set; skipping real-model acceptance example.")
        return False
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat"),
            "model_type": "chat",
            "auth": api_key,
            "request_options": {"temperature": 0.0},
        },
    )
    Agently.set_settings("debug", False)
    return True


def _configure_registry() -> None:
    registry_root = RUNTIME_ROOT / "registry"
    if os.getenv("AGENTLY_EXAMPLE_KEEP_REGISTRY") != "1":
        shutil.rmtree(registry_root, ignore_errors=True)
    registry_root.mkdir(parents=True, exist_ok=True)
    Agently.skills_executor.configure(
        registry_root=str(registry_root),
        allowed_trust_levels=["local", "remote"],
    )


def _assert_runtime_prerequisites() -> bool:
    if shutil.which("node") is None or shutil.which("npx") is None:
        print("Node.js/npx not available; skipping weather MCP acceptance example.")
        return False
    return True


def _compact_action_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for record in records:
        result = record.get("result", record.get("data", record.get("error", "")))
        compact.append(
            {
                "action_id": record.get("action_id") or record.get("tool_name"),
                "status": record.get("status"),
                "result": str(result)[:1400],
            }
        )
    return compact


async def _execute_model_owned_weather_actions(agent: Any, *, location: str, latitude: str, longitude: str, event_date: str) -> list[dict[str, Any]]:
    turn = agent.input(
        (
            f"Use the registered weather MCP actions to collect current conditions and forecast "
            f"for an outdoor retail pop-up in {location} on {event_date}. "
            f"Use latitude={latitude}, longitude={longitude}. "
            "Generate action calls only for get_current_conditions and get_forecast. "
            "Do not answer from memory."
        )
    )
    action_calls = await agent.async_generate_action_call(prompt=turn.prompt, max_rounds=1)
    if not action_calls:
        raise RuntimeError("The model did not generate weather MCP action calls.")

    records: list[dict[str, Any]] = []
    for call in action_calls:
        if not isinstance(call, dict):
            continue
        action_id = str(call.get("action_id") or call.get("tool_name") or call.get("name") or "")
        if action_id not in {"get_current_conditions", "get_forecast"}:
            continue
        action_input = call.get("action_input") or call.get("tool_kwargs") or call.get("kwargs") or {}
        if not isinstance(action_input, dict):
            action_input = {}
        result = await agent.action.async_execute_action(
            action_id,
            action_input,
            purpose=str(call.get("purpose") or "Collect weather facts for event operations."),
            source_protocol="example_model_generated_mcp",
        )
        records.append({
            "action_id": action_id,
            "action_input": action_input,
            "status": result.get("status"),
            "result": result.get("data", result.get("result", result.get("error"))),
        })
    if not records:
        raise RuntimeError(f"The model generated no executable weather actions: { action_calls }")
    return records


def _validate_lazy_remote_install(agent: Any, execution: Any) -> None:
    selected = {
        str(item.get("skill_id"))
        for item in execution.plan.get("selected_skills", [])
        if isinstance(item, dict)
    }
    missing = EXPECTED_SKILLS - selected
    if missing:
        raise RuntimeError(f"Remote Skills were not selected/materialized: { sorted(missing) }")

    print(f"selected_remote_skills={sorted(selected)}")
    diagnostics = execution.plan.get("diagnostics", [])
    discovered = [item for item in diagnostics if item.get("code") == "source_discovered"]
    installed = [item for item in diagnostics if item.get("code") == "source_installed"]
    print(f"source_discovered={len(discovered)} source_installed={len(installed)}")

    for skill_id in sorted(EXPECTED_SKILLS):
        contract = Agently.skills_executor.inspect_skills(skill_id)
        source = contract.get("source", {})
        checksum_count = len(contract.get("checksums", {}))
        print(
            "install_metadata",
            skill_id,
            {
                "source_package": source.get("source_package"),
                "source_url": source.get("source_url"),
                "source_subpath": source.get("source_subpath"),
                "source_commit": bool(source.get("source_commit")),
                "trust_level": contract.get("trust_level"),
                "checksums": checksum_count,
            },
        )


async def main() -> None:
    if not _configure_deepseek() or not _assert_runtime_prerequisites():
        return
    _configure_registry()

    location = os.getenv("EVENT_LOCATION", "San Francisco")
    latitude = os.getenv("EVENT_LATITUDE", "37.77493")
    longitude = os.getenv("EVENT_LONGITUDE", "-122.41942")
    event_date = os.getenv("EVENT_DATE", "2026-05-25")

    agent = Agently.create_agent("remote-skills-weather-event-ops")
    agent.set_agent_prompt(
        "system",
        (
            "You are an AI operations service for field-event planning. "
            "Use registered weather MCP actions for weather facts. "
            "Use selected remote Skills as execution guidance. "
            "Do not claim that bundled Skill scripts or Computer Use actions ran unless an Action record proves it."
        ),
    )
    agent.use_skills(REMOTE_SKILLS, mode="required", auto_allow=False)

    before_count = len(Agently.skills_executor.list_skills())
    print(f"installed_skills_before_plan={before_count}")

    agent.use_mcp(WEATHER_MCP_CONFIG)
    weather_tools = sorted(
        str(info.get("name"))
        for info in agent.action.get_tool_info().values()
        if str(info.get("name")) in {
            "search_location",
            "get_current_conditions",
            "get_forecast",
            "get_alerts",
            "check_service_status",
        }
    )
    print(f"weather_tools_registered={weather_tools}")

    action_records = await _execute_model_owned_weather_actions(
        agent,
        location=location,
        latitude=latitude,
        longitude=longitude,
        event_date=event_date,
    )
    compact_weather = _compact_action_records(action_records)
    successful_weather = [item for item in compact_weather if item.get("status") == "success"]
    if not successful_weather:
        raise RuntimeError("Weather MCP did not return a successful observation.")
    print(f"weather_action_records={len(action_records)} successful={len(successful_weather)}")
    pprint(compact_weather)

    task_payload = {
        "business_context": {
            "scenario": "Outdoor retail pop-up launch event",
            "location": location,
            "event_date": event_date,
            "attendees_expected": 180,
            "must_cover": [
                "weather go/hold decision",
                "RSVP/check-in webapp readiness",
                "weather MCP integration handoff",
                "supervised Computer Use operator runbook",
                "Word-ready operations brief",
            ],
        },
        "weather_mcp_action_records": compact_weather,
        "instruction": (
            "Create a structured operations packet. Ground weather claims in MCP records. "
            "Use webapp-testing for QA, mcp-builder for integration design, computer-use-agents "
            "for supervised GUI operation safeguards, and docx for the Word-ready brief structure."
        ),
    }

    execution = await agent.async_run_skills_task(
        json.dumps(task_payload, ensure_ascii=False),
        skills=REMOTE_SKILLS,
        mode="required",
        output=OPS_OUTPUTS,
        output_format="json",
        effort="normal",
    )

    if execution.status != "success":
        raise RuntimeError(f"Skills execution failed: { execution.to_dict() }")

    _validate_lazy_remote_install(agent, execution)
    output = cast(dict[str, Any], execution.output or {})
    print(f"execution_mode={execution.close_snapshot.get('execution_mode')}")
    print(f"decision={output.get('decision')}")
    print(f"weather_observation_count={len(output.get('weather_observations', []))}")
    print(f"webapp_qa_count={len(output.get('webapp_qa_checklist', []))}")
    print(f"mcp_plan_count={len(output.get('mcp_integration_plan', []))}")
    print(f"computer_use_check_count={len(output.get('computer_use_operator_checklist', []))}")
    print(f"word_section_count={len(output.get('word_ready_brief_sections', []))}")
    print(f"risk_count={len(output.get('risk_register', []))}")
    print("[OPERATIONS_PACKET]")
    pprint(output)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
