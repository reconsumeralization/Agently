"""Skills runtime planner effort strategy — release readiness review.

Run:
    python examples/skills_executor/09_runtime_planner_effort_strategy.py

Environment:
    DEEPSEEK_API_KEY in the shell or a .env file (loaded here via dotenv).

What this demonstrates:
  - A standard `SKILL.md` (guidance only) declared on the recommended
    `agent.async_run_skills_task(...)` path.
  - `async_run_skills_task(..., effort=...)` selecting a compatibility route
    label at call time. The facade lowers selected Skills through Blocks:
        effort="fast"     → single_shot label → model_request block
        effort="normal"   → runtime_chain label → flow_segment block:
                             preflight → research → plan → execute → verify
                             → reflect/retry → finalize
  - Stage model-key routing through the model pool without hard-coding model
    names in the Skills executor.
  - Streaming `skills.runtime_chain.*` events so each planner phase is visible,
    with Blocks plan/evidence metadata in `execution.close_snapshot["blocks"]`.

Working principle:
    fast   : skill_activation + model_request → FINALIZE
    normal : skill_activation + flow_segment(preflight → ... → finalize)

Host code still owns persistence. The Skill provides guidance; ActionRuntime,
ExecutionResource, TriggerFlow, or host code own real side effects.

Expected key output from one real DeepSeek run:
    fast_strategy=single_shot
    fast_block_kind=model_request
    normal_strategy=runtime_chain
    normal_block_kind=flow_segment
    normal_phase_events=10
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

# A standard SKILL.md: guidance only. The caller selects effort at runtime.
SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "release-readiness-reviewer"

# Mocked release context — what a real CI/CD + change-management system would attach.
RELEASE_CONTEXT = """Release candidate v3.2.0-rc1
Changes since v3.1.4:
  - Migrated auth from session cookies to JWT (breaking for legacy mobile clients <2.0).
  - Added a new async export pipeline (feature-flagged, default OFF).
  - Bumped Postgres driver 14.2 -> 15.1; ran migrations on staging only.
  - Hotfix: fixed a memory leak in the websocket gateway.
Test status: unit 100% pass; integration 92% pass (3 flaky payment tests); no load test run.
Rollback: blue/green available; DB migration is forward-only (no down migration written)."""

READINESS_OUTPUTS = {
    "decision": (str, "GO or NO-GO.", True),
    "reason": (str, "Concise justification grounded in the release context.", True),
    "top_risks": ([str], "Most important release risks.", True),
    "follow_ups": ([str], "Required follow-up actions before or after release.", True),
}


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

    # Model pool + key pool. The runtime planner resolves phase keys such as
    # planner/research/verifier/finalizer through this pool.
    agent.set_settings(
        "model_pool",
        {
            "planner": model,
            "research": model,
            "reason": model,
            "executor": model,
            "verifier": model,
            "reflector": model,
            "finalizer": model,
            "reason_fast": model,
        },
    )
    agent.set_settings("key_pool", {"primary": api_key})
    agent.set_settings("key_pool_strategy", {
        model: {"mode": "fixed", "pool": ["primary"]},
    })

    divider = "=" * 60
    print(divider)
    print("Release Readiness Reviewer — fast vs normal runtime planner")
    print(divider)

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        skill_root = SKILL_SOURCE

        Agently.skills_executor.configure(
            registry_root=str(tmp_path / "registry"),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(skill_root, trust_level="local", update=True)
        skill_id = str(contract["skill_id"])
        print(f"installed skill_id={skill_id}")
        print("compatibility route label: single_shot default")

        task = f"Review this release candidate and decide go/no-go.\n\n{RELEASE_CONTEXT}"

        # ── effort="fast" → single_shot ──────────────────────────────────────
        print(f"\n{divider}\n[1] effort='fast'  (expect single_shot)\n{divider}")
        fast_exec = await agent.async_run_skills_task(
            task,
            skills=[skill_id],
            mode="required",
            effort="fast",
            output=READINESS_OUTPUTS,
        )
        fast_strategy = (fast_exec.close_snapshot or {}).get("execution_mode")
        fast_blocks = fast_exec.close_snapshot["blocks"]
        fast_block_kind = fast_blocks["execution_graph"]["execution_blocks"][-1]["kind"]
        print(f"  status={fast_exec.status}  strategy={fast_strategy}  block={fast_block_kind}")

        # ── effort="normal" → full runtime-chain compatibility label ─────────
        print(f"\n{divider}\n[2] effort='normal'  (expect runtime_chain)\n{divider}")
        phase_events: list[str] = []

        async def on_stream(item: dict) -> None:
            if item.get("type") == "skills.runtime_chain.phase_start":
                phase = str(item.get("phase") or "")
                phase_events.append(phase)
                print(f"  → phase {len(phase_events)}: {phase}  model_key={item.get('model_key')}")

        normal_exec = await agent.async_run_skills_task(
            task,
            skills=[skill_id],
            mode="required",
            effort="normal",
            output=READINESS_OUTPUTS,
            stream_handler=on_stream,
        )
        normal_strategy = (normal_exec.close_snapshot or {}).get("execution_mode")
        normal_blocks = normal_exec.close_snapshot["blocks"]
        normal_block_kind = normal_blocks["execution_graph"]["execution_blocks"][-1]["kind"]
        decision = normal_exec.output or {}
        print(
            f"  status={normal_exec.status}  strategy={normal_strategy}  "
            f"block={normal_block_kind}  phases={len(phase_events)}"
        )

        # Host owns persistence: write a decision document from the finalized result.
        out_dir = ROOT / "examples" / "skills_executor" / "_artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        doc_path = out_dir / "release_v3.2.0-rc1_readiness.md"
        lines = [
            "# Release Readiness Review — v3.2.0-rc1\n",
            f"## Decision\n\n{decision.get('decision', '—')}\n",
            f"## Reason\n\n{decision.get('reason', '—')}\n",
        ]
        follow_ups = decision.get("follow_ups") or []
        if follow_ups:
            lines.append("## Follow-ups\n\n" + "\n".join(f"- {item}" for item in follow_ups))
        doc_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  decision doc written: {doc_path}")

        print(f"\n  decision={decision.get('decision', '—')}")
        print(f"  reason={str(decision.get('reason', '—'))[:300]}")

    print(f"\n{divider}")
    print(f"fast_strategy={fast_strategy}")
    print(f"fast_block_kind={fast_block_kind}")
    print(f"normal_strategy={normal_strategy}")
    print(f"normal_block_kind={normal_block_kind}")
    print(f"normal_phase_events={len(phase_events)}")
    print(f"decision_doc_written={doc_path.exists()}")


if __name__ == "__main__":
    asyncio.run(main())
