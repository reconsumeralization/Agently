"""Model-owned interpretation of missing long-task contract fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model_stage import run_model_stage

if TYPE_CHECKING:
    from .execution import AgentExecution


@dataclass(frozen=True)
class PreparedGoal:
    """Only inferred contributions; explicit declarations stay in the draft."""

    goals: tuple[str, ...]
    success_criteria: tuple[str, ...]
    request_id: str

    def to_record(self) -> dict[str, object]:
        return {
            "goals": list(self.goals), "success_criteria": list(self.success_criteria),
            "request_id": self.request_id, "source": "model",
        }

    @classmethod
    def from_record(cls, value: object) -> PreparedGoal:
        if not isinstance(value, dict) or value.get("source") != "model":
            raise ValueError("Invalid retained goal preparation provenance.")
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Retained goal preparation requires a request id.")
        return cls(
            goals=_strings(value.get("goals")),
            success_criteria=_strings(value.get("success_criteria")),
            request_id=request_id,
        )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("Goal preparation fields must be lists of non-empty strings.")
    return tuple(item.strip() for item in value)


def retain_prepared_goal(execution: AgentExecution, prepared: PreparedGoal) -> None:
    execution._prepared_goal = prepared
    execution.generated_success_criteria = list(prepared.success_criteria)
    execution.diagnostics["goal_preparation"] = prepared.to_record()
    execution._review_contract["goal_preparation"] = {
        "source": "model", "request_id": prepared.request_id,
        "inferred_fields": [
            name for name, values in (("goals", prepared.goals), ("success_criteria", prepared.success_criteria))
            if values
        ],
        "authority": "Interpretation of the original request, not additional user requirements or business hard gates.",
    }


async def prepare_missing_goal(execution: AgentExecution) -> dict[str, object] | None:
    """Return a blocked outcome when information is missing, otherwise retain it."""
    if execution.goal_items and execution.success_criteria_items:
        return None
    missing = [
        name for name, values in (
            ("goals", execution.goal_items), ("success_criteria", execution.success_criteria_items),
        ) if not values
    ]
    schema: dict[str, object] = {
        "status": (str, "Exactly ready or missing_information. ready requires every requested field to be non-empty.", True),
        **{
            name: [(str, "Interpret the original request only; do not invent scope or acceptance thresholds.")]
            for name in missing
        },
        "missing_information": [(str, "Required fact that cannot be inferred; non-empty only when status is missing_information.")],
    }
    stage = await run_model_stage(
        execution, producer="long_task", stage="goal_preparation",
        stage_input={"missing_fields": missing},
        stage_info={"final_output_contract": execution.prompt_snapshot.get("output")},
        stage_instructions=[
            "Interpret only the missing goal-contract fields identified in [input.execution_stage_input.missing_fields].",
            "Ground them in the original request, declared constraints, [info.stage_information.final_output_contract] "
            "and available context. Preserve explicit goals and success criteria; do not return replacements.",
            "Derived criteria describe the requested outcome, not new business thresholds or permission to expand scope.",
            "If a required fact cannot be inferred, report missing_information instead of fabricating it. "
            "Do not execute the task, generate its deliverable, or claim effects in this stage.",
        ],
        output=schema, inherit_extension_handlers=False,
    )
    value = stage.value
    if not isinstance(value, dict) or value.get("status") not in {"ready", "missing_information"}:
        raise ValueError("Goal preparation must return ready or missing_information.")
    if set(value) - {"status", "missing_information", *missing}:
        raise ValueError("Goal preparation cannot replace explicit contract fields.")
    gaps = _strings(value.get("missing_information"))
    contributed = {name: _strings(value.get(name)) for name in missing}
    if value["status"] == "missing_information":
        if not gaps:
            raise ValueError("Missing-information outcome must identify the missing facts.")
        result: dict[str, object] = {
            "status": "blocked", "accepted": False, "artifact_status": "blocked",
            "reason": "Required goal information is unavailable.",
            "missing_information": list(gaps), "request_id": stage.request_id,
        }
        execution.status = "blocked"
        execution.close_snapshot = {"route": "agent_task", **result}
        execution.diagnostics["goal_preparation"] = {"source": "model", **result}
        await execution.emit_stream("goal.preparation.blocked", result, route="agent_task")
        return result
    if gaps or any(not items for items in contributed.values()):
        raise ValueError("Ready goal preparation requires all missing fields and no information gaps.")
    prepared = PreparedGoal(
        goals=contributed.get("goals", ()), success_criteria=contributed.get("success_criteria", ()),
        request_id=stage.request_id,
    )
    retain_prepared_goal(execution, prepared)
    await execution.emit_stream("goal.preparation.completed", prepared.to_record(), route="agent_task")
    return None
