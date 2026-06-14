from __future__ import annotations

from typing import Any, cast

from agently.core.workspace.ContextBuilder.Defaults import _context_content, _excerpt


def test_agent_task_observation_context_prioritizes_action_evidence():
    record = {
        "kind": "agent_task_observation",
        "summary": "task iteration 1 observation",
    }
    content = {
        "decision_ref": {"large": "x" * 5000},
        "iteration": 1,
        "plan": {
            "execution_shape": "actions",
            "step_instruction": "collect source evidence",
        },
        "execution_result": {"step_result": "sources collected"},
        "execution_meta": {
            "status": "success",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "fetch_sources",
                        "status": "success",
                        "data": {
                            "sources": [
                                {"path": "docs/overview.md", "status": "ok", "excerpt": "A" * 2000},
                                {
                                    "path": "agently/core/application/SkillsExecutor/SkillsExecutor.py",
                                    "status": "ok",
                                    "excerpt": "SkillsExecutor is an implemented plugin entrypoint.",
                                },
                            ]
                        },
                    }
                ]
            },
        },
    }

    context = _context_content(cast(Any, record), content)
    assert context is not None
    excerpt = _excerpt(context, max_chars=1200) or ""

    assert "action_evidence" in excerpt
    assert "agently/core/application/SkillsExecutor/SkillsExecutor.py" in excerpt
    assert "decision_ref" not in excerpt
