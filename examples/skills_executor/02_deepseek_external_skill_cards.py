"""DeepSeek + external SKILL.md package smoke test.

Run:
    python examples/skills_executor/02_deepseek_external_skill_cards.py

Environment:
    DEEPSEEK_API_KEY must be available in the shell or a .env file.
    Optional:
      DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
      DEEPSEEK_DEFAULT_MODEL=deepseek-chat
      AGENTLY_SKILLS_REPO=../Agently-Skills
      ANTHROPIC_SKILLS_REPO=.example_runtime/anthropic-skills

Expected key output from a real DeepSeek run:
    [CASE] agently-runtime
    plan_status=resolved selected=['agently-runtime']
    selected_skill=agently-runtime
    [CASE] xlsx
    plan_status=resolved selected=['xlsx']
    selected_skill=xlsx
    [CASE] webapp-testing
    plan_status=resolved selected=['webapp-testing']
    selected_skill=webapp-testing

How it works:
    External SKILL.md folders are installed as guidance-heavy skill contracts.
    agent.use_skills(..., mode="model_decision") exposes their SkillCards to the
    request; DeepSeek decides how to use the card in its answer.
    The Anthropic scripts remain package assets. They are not executed as arbitrary
    Python handlers; executable work should be bound through host Actions when
    required.

Flow:
    local skill dir / cloned upstream skill dir
      |
      v
    Agently.skills_executor.install_skills(...) -> SkillContract + SkillCard
      |
      v
    agent.use_skills(..., model_decision)
      |
      v
    request prefix adds optional skill_cards to prompt.info
      |
      v
    DeepSeek returns structured task guidance using the selected card
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from pprint import pprint
from typing import Any

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently

RUNTIME_ROOT = ROOT / ".example_runtime" / "skills_executor"
ANTHROPIC_REPO_URL = "https://github.com/anthropics/skills.git"


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


def _run(command: list[str], *, cwd: Path):
    subprocess.run(command, cwd=cwd, check=True)


def _ensure_anthropic_repo() -> Path:
    configured = os.getenv("ANTHROPIC_SKILLS_REPO")
    repo_path = Path(configured).expanduser().resolve() if configured else RUNTIME_ROOT / "anthropic-skills"
    if (repo_path / "skills" / "xlsx" / "SKILL.md").exists():
        return repo_path

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--depth", "1", ANTHROPIC_REPO_URL, str(repo_path)], cwd=ROOT)
    return repo_path


def _resolve_source(case: dict[str, str], anthropic_repo: Path) -> Path:
    if case["source"] == "agently-skills":
        configured = os.getenv("AGENTLY_SKILLS_REPO")
        repo_path = Path(configured).expanduser().resolve() if configured else (ROOT / ".." / "Agently-Skills").resolve()
    else:
        repo_path = anthropic_repo
    skill_path = repo_path / case["relative_path"]
    if not (skill_path / "SKILL.md").exists():
        raise RuntimeError(f"Missing SKILL.md for { case['label'] }: { skill_path }")
    return skill_path


def _install_targets() -> list[str]:
    anthropic_repo = _ensure_anthropic_repo()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    Agently.skills_executor.configure(registry_root=str(RUNTIME_ROOT / "registry"), allowed_trust_levels=["local"])

    installed_ids = []
    for case in CASES:
        contract = Agently.skills_executor.install_skills(_resolve_source(case, anthropic_repo), trust_level="local", update=True)
        installed_ids.append(str(contract.get("skill_id")))
    return installed_ids


def _create_agent():
    agent = Agently.create_agent()
    agent.set_agent_prompt(
        "system",
        (
            "You are validating Agently Skills Executor behavior. "
            "When skill_cards are present, select the matching skill only if it fits the task. "
            "Return concrete execution guidance, not marketing copy."
        ),
    )
    return agent


def _selected_ids(plan: dict[str, Any]) -> list[str]:
    return [str(item.get("skill_id")) for item in plan.get("selected_skills", [])]


def _run_case(case: dict[str, str]) -> dict[str, Any]:
    agent = _create_agent()
    selector = case["selector"]
    task = case["task"]
    agent.use_skills([selector], mode="model_decision")
    plan = agent.resolve_skills_plan(task, skills=[selector], mode="model_decision")
    result = (
        agent.input(
            {
                "task": task,
                "expected_skill": selector,
                "instruction": (
                    "Use the disclosed skill card if it fits. "
                    "Set selected_skill to the skill id you used, or 'none'."
                ),
            }
        )
        .output(
            {
                "selected_skill": (str, "The selected skill id, or none.", True),
                "skill_fit": (str, "One concise reason the skill fits or does not fit.", True),
                "first_steps": [(str, "Concrete first execution steps.", True)],
                "must_not_do": [(str, "Important boundary or anti-pattern.", True)],
            }
        )
        .start(max_retries=1, raise_ensure_failure=False)
    )
    return {"plan": plan, "result": result}


def main():
    _configure_deepseek()
    installed_ids = _install_targets()
    print("[INSTALLED_SKILLS]")
    print(installed_ids)

    for case in CASES:
        print(f"\n[CASE] { case['label'] }")
        outcome = _run_case(case)
        plan = outcome["plan"]
        result = outcome["result"]
        print(f"plan_status={ plan.get('status') } selected={ _selected_ids(plan) }")
        print(f"selected_skill={ result.get('selected_skill') }")
        print(f"first_steps_count={ len(result.get('first_steps') or []) }")
        print("[RESULT]")
        pprint(result)


if __name__ == "__main__":
    main()
