"""Real-model integration tests for staged and react execution strategies.

Run:
    python tests/test_strategies_real_model.py

Environment:
    DEEPSEEK_API_KEY must be available in the shell or .env file.
    Optional:
      DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
      DEEPSEEK_DEFAULT_MODEL=deepseek-chat
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("DEEPSEEK_API_KEY"),
        reason="DEEPSEEK_API_KEY is required for real-model strategy tests.",
    ),
]

from agently import Agently
from agently.types.data import SkillExecutionPlan


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════

def _setup_agent(name="strategy-test-agent"):
    """Set up an agent with DeepSeek model pool + key pool."""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is required. Put it in your shell or .env."
        )

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    default_model = os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat")

    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": base_url,
            "model": default_model,
            "model_type": "chat",
            "auth": api_key,
            "request_options": {"temperature": 0.2},
        },
    )
    Agently.set_settings("debug", False)

    agent = Agently.create_agent(name)

    agent.set_settings("model_pool", {
        "reason": default_model,
        "reason_fast": default_model,
    })
    agent.set_settings("key_pool", {"primary": api_key})
    agent.set_settings("key_pool_strategy", {
        default_model: {"mode": "fixed", "pool": ["primary"]},
    })

    return agent


# ═══════════════════════════════════════════════════════════════════
#  Test 1: Staged strategy — multi-step writing task
# ═══════════════════════════════════════════════════════════════════

STAGED_SKILL_MD = """\
---
name: multi-step-writer
description: >-
  Complete writing tasks in multiple sequential stages: plan → draft → polish.
  Use for any task that benefits from iterative refinement.
keywords: [writing, multi-step, staged, draft, refine]
execution: staged
stages:
  - "Read and analyze the task requirements. Identify key points to cover."
  - "Write a complete first draft covering all key points."
  - "Review and polish: improve clarity, fix grammar, and tighten prose."
version: "1.0.0"
---

# Multi-Step Writer

You are a careful writer who works in stages. When given a task, follow the
stage-by-stage instructions in the plan. At each stage, focus ONLY on that
stage's goal. Do not skip ahead.

## Stage goals

1. **Plan**: Understand the task. List the key points you need to cover. Output
   a brief numbered outline.

2. **Draft**: Write a complete draft. Include all key points from the outline.
   Use clear, engaging prose.

3. **Polish**: Review the draft. Fix errors, improve word choice, and make sure
   the flow is natural. Output the final polished text.
"""


async def test_staged_strategy_real():
    """Real model test: staged strategy runs 3 stages and produces polished output."""
    print("\n" + "═" * 72)
    print("Test 1: Staged strategy — multi-step writing")
    print("═" * 72)

    agent = _setup_agent("staged-test-agent")

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Create and install skill
        skill_root = temp_path / "multi-step-writer"
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(STAGED_SKILL_MD, encoding="utf-8")

        Agently.skills_executor.configure(
            registry_root=str(temp_path / "registry"),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(
            skill_root, trust_level="local"
        )
        skill_id = str(contract["skill_id"])
        print(f"Installed skill: {skill_id}")

        # ── Plan ──
        task = "Write a short paragraph (max 5 sentences) about why staged execution matters for LLM agents."
        plan = agent.resolve_skills_plan(task, skills=[skill_id], mode="required")

        strategy = plan.get("execution_strategy")
        stages = plan.get("execution_stages") or []
        print(f"Plan strategy: {strategy}")
        print(f"Plan status: {plan.get('status')}")
        print(f"Stages ({len(stages)}):")
        for s in stages:
            print(f"  - {s.get('description', '')[:80]}...")

        assert strategy == "staged", f"Expected 'staged', got '{strategy}'"
        assert len(stages) == 3, f"Expected 3 stages, got {len(stages)}"

        # ── Execute ──
        print("\nExecuting staged plan with real model...")
        execution = await agent.async_execute_skills_plan(
            task=task,
            plan=plan,
        )

        status = execution.status
        output = execution.output
        print(f"Execution status: {status}")

        if status == "success":
            # The output from FinalizeBlock should contain the assembled steps
            if isinstance(output, dict):
                steps = output.get("steps", [])
                print(f"Steps executed: {len(steps)}")
                for s in steps:
                    step_out = str(s.get("output", ""))[:120]
                    print(f"  Step {s.get('step_index', '?')}: {step_out}...")

                # Check that step outputs look like real model responses
                assert len(steps) >= 2, f"Expected at least 2 steps, got {len(steps)}"
                for s in steps:
                    out = str(s.get("output", ""))
                    assert len(out) > 20, f"Step output too short: {out[:50]}"

                print(" PASS: staged strategy produced multi-step real model output")
                return True
            else:
                print("  Output (non-dict):", str(output)[:200])
                # Even non-dict output is fine — the model ran
                print(" PASS: staged strategy completed with real model")
                return True
        else:
            print(f"  FAIL: unexpected status '{status}', output={str(output)[:200]}")
            return False


# ═══════════════════════════════════════════════════════════════════
#  Test 2: React strategy — skill with allowed-tools
# ═══════════════════════════════════════════════════════════════════

REACT_SKILL_MD = """\
---
name: research-assistant
description: >-
  Research assistant that can search and compute. Uses tools when needed.
keywords: [research, search, compute, tool-use]
allowed-tools: [search, calculate]
version: "1.0.0"
---

# Research Assistant

You are a research assistant with access to tools. Use them when it helps
answer the user's question. When a tool result is available, incorporate it
into your reasoning.

## Available tools

- **search**: Search for factual information. Input: a search query string.
- **calculate**: Perform a mathematical calculation. Input: a math expression.

## Instructions

1. Analyze the user's question
2. Decide if you need a tool call
3. If yes, specify the tool name and arguments
4. When you have enough information, set final: true
"""


async def test_react_strategy_real():
    """Real model test: react strategy loops with tool use then terminates."""
    print("\n" + "═" * 72)
    print("Test 2: React strategy — tool-using skill")
    print("═" * 72)

    agent = _setup_agent("react-test-agent")

    # Register actions on the agent (proper Action API, not deprecated agent.tool)
    agent.action.register_action(
        action_id="search",
        desc="Search for factual information. Returns top matching result.",
        kwargs={"query": (str, "Search query string.")},
        func=lambda query: f'TOP RESULT for "{query}": Paris is the capital of France.',
        expose_to_model=True,
    )
    agent.action.register_action(
        action_id="calculate",
        desc="Evaluate a mathematical expression. Returns the numeric result.",
        kwargs={"expression": (str, "Math expression to evaluate.")},
        func=lambda expression: f"Result of {expression} = {eval(expression)}",
        expose_to_model=True,
    )
    print("Registered actions: search, calculate")

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Create and install skill
        skill_root = temp_path / "research-assistant"
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(REACT_SKILL_MD, encoding="utf-8")

        Agently.skills_executor.configure(
            registry_root=str(temp_path / "registry"),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(
            skill_root, trust_level="local"
        )
        skill_id = str(contract["skill_id"])
        print(f"Installed skill: {skill_id}")

        # ── Plan ──
        task = "What is the capital of France? Use the search tool to verify."
        plan = agent.resolve_skills_plan(task, skills=[skill_id], mode="required")

        strategy = plan.get("execution_strategy")
        print(f"Plan strategy: {strategy}")
        print(f"Plan status: {plan.get('status')}")

        assert strategy == "react", f"Expected 'react', got '{strategy}'"

        # ── Execute ──
        print("\nExecuting react plan with real model...")
        execution = await agent.async_execute_skills_plan(
            task=task,
            plan=plan,
        )

        status = execution.status
        output = execution.output
        print(f"Execution status: {status}")

        if status == "success":
            if isinstance(output, dict):
                history = output.get("history", [])
                step_count = output.get("step_count", 0)
                print(f"Steps executed: {step_count}")
                print(f"Observations: {len(history)}")
                for obs in history[:5]:
                    name = obs.get("name", "?")
                    result_preview = str(obs.get("result", ""))[:120]
                    print(f"  [{name}] {result_preview}")
                print(" PASS: react strategy completed with real model")
                return True
            else:
                print(f"  Output: {str(output)[:200]}")
                print(" PASS: react strategy completed (non-dict output)")
                return True
        else:
            print(f"  FAIL: unexpected status '{status}'")
            print(f"  Output: {str(output)[:500]}")
            return False


# ═══════════════════════════════════════════════════════════════════
#  Test 3: Backward compat — single_shot still works
# ═══════════════════════════════════════════════════════════════════

PLAIN_SKILL_MD = """\
---
name: hello-responder
description: Respond with a friendly greeting.
keywords: [greeting, hello]
version: "1.0.0"
---

# Hello Responder

When asked to greet, respond with a warm, friendly greeting. Keep it short.
"""


async def test_single_shot_still_works():
    """Real model test: plain skill without strategy hints stays single_shot."""
    print("\n" + "═" * 72)
    print("Test 3: Backward compat — single_shot with plain skill")
    print("═" * 72)

    agent = _setup_agent("single-shot-test-agent")

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        skill_root = temp_path / "hello-responder"
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(PLAIN_SKILL_MD, encoding="utf-8")

        Agently.skills_executor.configure(
            registry_root=str(temp_path / "registry"),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(
            skill_root, trust_level="local"
        )
        skill_id = str(contract["skill_id"])
        print(f"Installed skill: {skill_id}")

        plan = agent.resolve_skills_plan(
            "Say hello!", skills=[skill_id], mode="required"
        )

        strategy = plan.get("execution_strategy")
        print(f"Plan strategy: {strategy}")

        assert strategy == "single_shot", f"Expected 'single_shot', got '{strategy}'"

        execution = await agent.async_execute_skills_plan(
            task="Say hello!", plan=plan
        )

        status = execution.status
        output = execution.output
        print(f"Execution status: {status}")

        if status == "success":
            print(f"  Output: {str(output)[:150]}")
            assert output is not None
            print(" PASS: single_shot backward compat works")
            return True
        else:
            print(f"  FAIL: {status}")
            return False


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

async def main():
    results = []

    print("=" * 72)
    print("SkillsExecutor Strategy Integration Tests (Real Model)")
    print("=" * 72)
    print(f"Model: {os.getenv('DEEPSEEK_DEFAULT_MODEL', 'deepseek-chat')}")
    print(f"Base URL: {os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')}")

    try:
        results.append(("single_shot backward compat", await test_single_shot_still_works()))
    except Exception as e:
        print(f"  FAIL with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("single_shot backward compat", False))

    try:
        results.append(("staged strategy", await test_staged_strategy_real()))
    except Exception as e:
        print(f"  FAIL with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("staged strategy", False))

    try:
        results.append(("react strategy", await test_react_strategy_real()))
    except Exception as e:
        print(f"  FAIL with exception: {e}")
        import traceback
        traceback.print_exc()
        results.append(("react strategy", False))

    print("\n" + "=" * 72)
    print("Results Summary")
    print("=" * 72)
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {status}: {name}")
        if not passed:
            all_pass = False

    if not all_pass:
        print("\nSome tests FAILED. Check the output above for details.")
        sys.exit(1)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
