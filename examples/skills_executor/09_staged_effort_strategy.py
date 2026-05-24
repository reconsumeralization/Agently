"""Staged execution strategy + effort presets — release readiness review.

Run:
    python examples/skills_executor/09_staged_effort_strategy.py

Environment:
    DEEPSEEK_API_KEY in the shell or a .env file (loaded here via dotenv).

What this demonstrates (newly-added Skills Executor features):
  - A standard `SKILL.md` (guidance only) that opts into multi-step execution
    with `execution: staged` + a declared `stages:` list in frontmatter.
  - `agent.set_settings("effort_presets", {...})` mapping a caller-facing
    quality/cost profile to a concrete strategy + model key + step budget.
  - `async_run_skills_task(..., effort=...)` selecting that profile at call time:
        effort="fast"     → single_shot (one model request)
        effort="thorough" → staged (sequential ReasonBlock steps on TriggerFlow)
  - Streaming the `skills.staged.*` runtime events so the per-step execution is
    observable, and reading the staged result shape.

Working principle (staged strategy):
    start → STEP(stage 0) → STEP(stage 1) → ... → FINALIZE
    Each stage is one ReasonBlock model request; prior step outputs are folded
    into the next step's prompt. The staged result is the accumulated per-step
    outputs ({"steps": [...], "task": ...}); host code owns any persistence.

Expected key output from one real DeepSeek run:
    fast_strategy=single_shot
    thorough_strategy=staged
    thorough_step_events=3
    thorough_steps_returned=3
    decision_doc_written=True
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

from agently import Agently

# A standard SKILL.md: guidance only, opting into staged multi-step execution.
SKILL_MD = """---
name: Release Readiness Reviewer
description: Reviews a release candidate and produces a go/no-go readiness decision.
keywords: [release, readiness, review, go-no-go]
execution: staged
stages:
  - "Assess: summarize what changed in this release candidate and its scope."
  - "Risks: identify the top deployment/rollback risks and any missing safeguards."
  - "Decide: give a clear GO or NO-GO with one-line justification and required follow-ups."
version: "1.0.0"
---

# Release Readiness Reviewer

You are a pragmatic release manager. Work through each stage in order, building
on the previous stage's findings. Be concrete and concise; prefer specifics from
the provided change context over generic advice.
"""

# Mocked release context — what a real CI/CD + change-management system would attach.
RELEASE_CONTEXT = """Release candidate v3.2.0-rc1
Changes since v3.1.4:
  - Migrated auth from session cookies to JWT (breaking for legacy mobile clients <2.0).
  - Added a new async export pipeline (feature-flagged, default OFF).
  - Bumped Postgres driver 14.2 -> 15.1; ran migrations on staging only.
  - Hotfix: fixed a memory leak in the websocket gateway.
Test status: unit 100% pass; integration 92% pass (3 flaky payment tests); no load test run.
Rollback: blue/green available; DB migration is forward-only (no down migration written)."""


async def main() -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat")
    if not api_key:
        print("DEEPSEEK_API_KEY not set; skipping (this example needs a real model).")
        return

    Agently.set_settings("OpenAICompatible", {
        "base_url": base_url, "model": model, "model_type": "chat",
        "auth": api_key, "request_options": {"temperature": 0.3},
    })
    Agently.set_settings("debug", False)

    agent = Agently.create_agent("release-readiness-agent")

    # Model pool + key pool so effort presets can resolve a reason model key.
    agent.set_settings("model_pool", {"reason": model, "reason_fast": model})
    agent.set_settings("key_pool", {"primary": api_key})
    agent.set_settings("key_pool_strategy", {
        model: {"mode": "fixed", "pool": ["primary"]},
    })

    # Caller-facing effort profiles -> concrete execution config.
    agent.set_settings("effort_presets", {
        "fast":     {"strategy": "single_shot", "reason_key": "reason_fast", "step_budget": 1},
        "thorough": {"strategy": "staged",      "reason_key": "reason",      "step_budget": 5},
    })

    divider = "=" * 60
    print(divider)
    print("Release Readiness Reviewer — staged strategy + effort presets")
    print(divider)

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_root = tmp_path / "release-readiness-reviewer"
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        Agently.skills_executor.configure(
            registry_root=str(tmp_path / "registry"),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(skill_root, trust_level="local", update=True)
        skill_id = str(contract["skill_id"])
        declared = (contract.get("metadata", {}).get("frontmatter", {}) or {}).get("execution", "—")
        print(f"installed skill_id={skill_id}")
        print(f"declared strategy (frontmatter): {declared}")

        task = f"Review this release candidate and decide go/no-go.\n\n{RELEASE_CONTEXT}"

        # ── effort="fast" → single_shot ──────────────────────────────────────
        print(f"\n{divider}\n[1] effort='fast'  (expect single_shot)\n{divider}")
        fast_exec = await agent.async_run_skills_task(
            task, skills=[skill_id], mode="required", effort="fast",
        )
        fast_strategy = (fast_exec.close_snapshot or {}).get("execution_mode")
        print(f"  status={fast_exec.status}  strategy={fast_strategy}")

        # ── effort="thorough" → staged (stream the per-step events) ──────────
        print(f"\n{divider}\n[2] effort='thorough'  (expect staged, 3 steps)\n{divider}")
        step_events: list[int] = []

        async def on_stream(item: dict) -> None:
            if item.get("type") == "skills.staged.step_start":
                payload = item.get("payload") or {}
                raw_idx = payload.get("step_index")
                idx = raw_idx if isinstance(raw_idx, int) else len(step_events)
                total = (item.get("payload") or {}).get("total_steps")
                step_events.append(idx)
                print(f"  → staged step {idx + 1}/{total} started")

        thorough_exec = await agent.async_run_skills_task(
            task, skills=[skill_id], mode="required", effort="thorough",
            stream_handler=on_stream,
        )
        thorough_strategy = (thorough_exec.close_snapshot or {}).get("execution_mode")
        steps = (thorough_exec.output or {}).get("steps", []) if isinstance(thorough_exec.output, dict) else []
        print(f"  status={thorough_exec.status}  strategy={thorough_strategy}  steps_returned={len(steps)}")

        # Host owns persistence: write a decision document from the staged steps.
        out_dir = ROOT / "examples" / "skills_executor" / "_artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        doc_path = out_dir / "release_v3.2.0-rc1_readiness.md"
        lines = ["# Release Readiness Review — v3.2.0-rc1\n"]
        for s in steps:
            desc = s.get("description", f"Step {s.get('step_index', '?')}")
            body = s.get("output", "")
            lines.append(f"## {desc}\n\n{body}\n")
        doc_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  decision doc written: {doc_path}")

        # Show the final (Decide) stage output, if present.
        if steps:
            final_step = steps[-1]
            preview = str(final_step.get("output", ""))[:300]
            print(f"\n  Final stage ({final_step.get('description', '')[:40]}...):\n  {preview}")

    print(f"\n{divider}")
    print(f"fast_strategy={fast_strategy}")
    print(f"thorough_strategy={thorough_strategy}")
    print(f"thorough_step_events={len(step_events)}")
    print(f"thorough_steps_returned={len(steps)}")
    print(f"decision_doc_written={doc_path.exists()}")


if __name__ == "__main__":
    asyncio.run(main())
