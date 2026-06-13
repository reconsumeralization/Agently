"""DeepSeek + lazy remote SKILL.md source smoke test.

Run:
    python examples/skills_executor/02_deepseek_external_skill_cards.py

Environment:
    DEEPSEEK_API_KEY must be available in the shell or a .env file.
    Optional:
      DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
      DEEPSEEK_DEFAULT_MODEL=deepseek-chat
      AGENTLY_SKILLS_REPO=../Agently-Skills       # optional local checkout
      ANTHROPIC_SKILLS_REPO=.example_runtime/anthropic-skills

Expected key output from a real DeepSeek run:
    installed_skills_before_plan=0
    [CASE] agently-runtime
    plan_status=resolved selected=['agently-runtime']
    source_discovered=1 source_installed=1
    execution_status=success
    selected_skill=agently-runtime
    [CASE] xlsx
    plan_status=resolved selected=['xlsx']
    source_discovered=1 source_installed=1
    execution_status=success
    selected_skill=xlsx
    [CASE] webapp-testing
    plan_status=resolved selected=['webapp-testing']
    source_discovered=1 source_installed=1
    execution_status=success
    selected_skill=webapp-testing

How it works:
    External SKILL.md folders are declared on agent.use_skills(...) as remote
    source selectors. The Skills Executor performs lightweight discovery and
    installs the selected Skill only when planning hits that source. The business
    code never calls install_skills_pack(...) on the request path.

    The Anthropic scripts remain package assets/resources. They are not executed
    as arbitrary Python handlers; executable work should be bound through
    ActionRuntime / ExecutionResource when required.

Flow:
    git URL / GitHub shorthand / local checkout
      |
      v
    agent.use_skills({"source": ..., "subpath": ...}, mode="required")
      |
      v
    planner discovers SKILL.md card, then materializes selected Skill
      |
      v
    async_run_skills_task(..., effort="fast") injects full guidance and returns
    structured task guidance
"""

from __future__ import annotations

import os
import shutil
import sys
import asyncio
from pathlib import Path
from pprint import pprint
from typing import Any

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently

RUNTIME_ROOT = ROOT / ".example_runtime" / "skills_executor"


CASES = [
    {
        "label": "agently-runtime",
        "selector": "agently-runtime",
        "source": "agently-skills",
        "relative_path": "skills/agently-runtime",
        "task": (
            "I am building an Agently assistant that needs a Python capability, "
            "action logs, and managed execution environment boundaries. Recommend "
            "the native Agently implementation path."
        ),
    },
    {
        "label": "xlsx",
        "selector": "xlsx",
        "source": "anthropic-skills",
        "relative_path": "skills/xlsx",
        "task": (
            "Create a spreadsheet deliverable for a monthly sales report with "
            "formulas, formatting rules, and charts. Explain the first execution steps."
        ),
    },
    {
        "label": "webapp-testing",
        "selector": "webapp-testing",
        "source": "anthropic-skills",
        "relative_path": "skills/webapp-testing",
        "task": (
            "Test a local React web app login page with Playwright. Include server "
            "startup handling, DOM reconnaissance, screenshots, and assertions."
        ),
    },
]


def _configure_deepseek():
    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DEEPSEEK_API_KEY. Put it in your shell or .env before running this example.")

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


def _source_for(case: dict[str, str]) -> tuple[str, bool, str]:
    if case["source"] == "agently-skills":
        configured = os.getenv("AGENTLY_SKILLS_REPO")
        source = str(Path(configured).expanduser().resolve()) if configured else "AgentEra/Agently-Skills"
    else:
        configured = os.getenv("ANTHROPIC_SKILLS_REPO")
        source = str(Path(configured).expanduser().resolve()) if configured else "anthropics/skills"
    fetch = not Path(source).expanduser().exists()
    trust_level = "remote" if fetch else "local"
    return source, fetch, trust_level


def _selector_for(case: dict[str, str]) -> dict[str, Any]:
    source, fetch, trust_level = _source_for(case)
    return {
        "source": source,
        "subpath": case["relative_path"],
        "fetch": fetch,
        "trust_level": trust_level,
        "auto_allow": False,
    }


def _create_agent():
    agent = Agently.create_agent()
    agent.set_agent_prompt(
        "system",
        (
            "You are validating Agently Skills Executor behavior. "
            "Use the selected remote Skill guidance when it fits the task. "
            "Return concrete execution guidance, not marketing copy."
        ),
    )
    return agent


def _selected_ids(plan: dict[str, Any]) -> list[str]:
    return [str(item.get("skill_id")) for item in plan.get("selected_skills", [])]


async def _run_case(case: dict[str, str]) -> dict[str, Any]:
    agent = _create_agent()
    selector = _selector_for(case)
    task = case["task"]
    agent.use_skills([selector], mode="required")
    execution = await agent.async_run_skills_task(
        (
            f"{task}\n\n"
            f"Expected selected_skill: {case['selector']}. Use the selected Skill guidance "
            "to recommend concrete first execution steps and boundaries."
        ),
        mode="required",
        effort="fast",
        output={
            "selected_skill": (str, "The selected skill id, or none.", True),
            "skill_fit": (str, "One concise reason the skill fits or does not fit.", True),
            "first_steps": [(str, "Concrete first execution steps.", True)],
            "must_not_do": [(str, "Important boundary or anti-pattern.", True)],
        },
    )
    return {"plan": execution.plan, "result": execution.output, "execution": execution}


async def main():
    _configure_deepseek()
    if (RUNTIME_ROOT / "registry").exists():
        shutil.rmtree(RUNTIME_ROOT / "registry")
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    Agently.skills_executor.configure(
        registry_root=str(RUNTIME_ROOT / "registry"),
        allowed_trust_levels=["local", "remote"],
    )
    print(f"installed_skills_before_plan={len(Agently.skills_executor.list_skills())}")

    for case in CASES:
        print(f"\n[CASE] { case['label'] }")
        outcome = await _run_case(case)
        plan = outcome["plan"]
        result = outcome["result"]
        execution = outcome["execution"]
        diagnostics = plan.get("diagnostics", [])
        discovered = sum(1 for item in diagnostics if item.get("code") == "source_discovered")
        installed = sum(1 for item in diagnostics if item.get("code") == "source_installed")
        print(f"plan_status={ plan.get('status') } selected={ _selected_ids(plan) }")
        print(f"source_discovered={discovered} source_installed={installed}")
        print(f"execution_status={execution.status}")
        result_dict = result if isinstance(result, dict) else {}
        print(f"selected_skill={ result_dict.get('selected_skill') }")
        print(f"first_steps_count={ len(result_dict.get('first_steps') or []) }")
        print("[RESULT]")
        pprint(result)


if __name__ == "__main__":
    asyncio.run(main())
