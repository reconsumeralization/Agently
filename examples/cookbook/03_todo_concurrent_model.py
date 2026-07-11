from __future__ import annotations

import asyncio
from pprint import pprint

from agently import Agently

from _shared_model import configure_model, print_model_provider


GOAL = (
    "Build a bilingual customer-service bot that answers FAQs, checks order "
    "status, detects escalation risk, and hands off to a human agent."
)

READINESS_SCHEMA = {
    "summary": (str, "implementation-readiness summary", "not_null"),
    "completed_areas": ([str], "areas covered by the implementation plan", True),
    "dependency_notes": (str, "concise ordering or integration dependencies", "not_null"),
}

SUBMITTED_PLAN = {
    "graph_id": "customer-service-readiness",
    "task_schema_version": "task_dag/v1",
    "tasks": [
        {
            "id": "analyze_business_goal",
            "kind": "model",
            "title": "Analyze business capabilities",
            "purpose": (
                "Analyze the supplied goal for user-facing capabilities, success "
                "criteria, and operational assumptions."
            ),
        },
        {
            "id": "assess_delivery_constraints",
            "kind": "model",
            "title": "Assess delivery constraints",
            "purpose": (
                "Independently assess integration, safety, escalation, and rollout "
                "constraints for the supplied goal."
            ),
        },
        {
            "id": "synthesize_implementation_plan",
            "kind": "model",
            "title": "Synthesize implementation plan",
            "purpose": (
                "Use both dependency_results to create one ordered implementation "
                "plan, keeping parallel workstreams and later dependencies explicit."
            ),
            "depends_on": ["analyze_business_goal", "assess_delivery_constraints"],
        },
        {
            "id": "finalize_readiness_report",
            "kind": "model",
            "title": "Finalize readiness report",
            "purpose": (
                "Turn dependency_results into the requested structured readiness "
                "report. Do not omit implementation dependencies."
            ),
            "depends_on": ["synthesize_implementation_plan"],
        },
    ],
    "semantic_outputs": {"final_readiness_report": "finalize_readiness_report"},
}


async def main_async():
    provider = configure_model(temperature=0.0)
    print_model_provider(provider)

    task = Agently.create_dynamic_task(
        target=GOAL,
        plan=SUBMITTED_PLAN,
        output_schema=READINESS_SCHEMA,
        ensure_keys=["summary", "completed_areas", "dependency_notes"],
    )
    validation = task.validate(SUBMITTED_PLAN, strict_schema_version=True)
    snapshot = await task.async_run(
        graph_input={"goal": GOAL},
        timeout=180,
        concurrency=3,
    )

    semantic_output = snapshot["semantic_outputs"]["final_readiness_report"]
    final_result = semantic_output["result"]
    summary = {
        "task_ids": list(validation.topological_task_ids),
        "root_task_ids": list(validation.root_task_ids),
        "join_task_ids": [
            item.id for item in validation.graph.tasks if len(item.depends_on) >= 2
        ],
        "semantic_final_task": semantic_output["task_id"],
        "completed_area_count": len(final_result["completed_areas"]),
        "dependency_notes_non_empty": bool(str(final_result["dependency_notes"]).strip()),
    }

    print("[TODO_CONCURRENCY_SUMMARY]")
    pprint(summary)
    print("[READINESS_REPORT]")
    pprint(final_result)

    assert summary["task_ids"] == [
        "analyze_business_goal",
        "assess_delivery_constraints",
        "synthesize_implementation_plan",
        "finalize_readiness_report",
    ]
    assert summary["root_task_ids"] == [
        "analyze_business_goal",
        "assess_delivery_constraints",
    ]
    assert summary["join_task_ids"] == ["synthesize_implementation_plan"]
    assert summary["semantic_final_task"] == "finalize_readiness_report"
    assert str(final_result["summary"]).strip()
    assert summary["dependency_notes_non_empty"] is True


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()


# Expected key output from a real DeepSeek run on 2026-07-11:
# task_ids=['analyze_business_goal', 'assess_delivery_constraints',
#           'synthesize_implementation_plan', 'finalize_readiness_report']
# root_task_ids=['analyze_business_goal', 'assess_delivery_constraints']
# join_task_ids=['synthesize_implementation_plan']
# semantic_final_task='finalize_readiness_report'
# completed_area_count=11
# dependency_notes_non_empty=True
#
# How it works:
# The application submits a stable TaskDAG whose four tasks all use real model
# requests. Dynamic Task validates the graph before TaskDAGExecutor runs the two
# independent root analyses concurrently under concurrency=3, then executes the
# declared join and final semantic-output task. Application code does not scan
# pending/completed sets or compile runtime plan data into TriggerFlow definitions;
# TriggerFlow remains the executor substrate under TaskDAG.
