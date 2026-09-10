"""Model-owned interpretation of missing long-task contract fields."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import ConfigDict, Field, StringConstraints, create_model

from agently.core.model.Prompt import Prompt
from agently.utils import DataFormatter

from .model_stage import run_model_stage
from .limits import await_route_with_limits

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
    non_blank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, strict=True)]
    # Pydantic's dynamic field-definition boundary accepts runtime type objects.
    fields: dict[str, Any] = {
        "status": (Literal["ready", "missing_information"], Field(
            description="Whether the missing goal-contract fields can be inferred from the supplied information.")),
        **{
            name: (list[non_blank], Field(description=(
                "Inferred outcomes." if name == "goals" else "Conditions describing the requested outcome."
            ) + " Each item must be a non-blank string."))
            for name in missing
        },
        "missing_information": (list[non_blank], Field(description=
            "Required facts that cannot be inferred; each item must be a non-blank string. "
            "With status=ready: []; every requested goal-contract list must be non-empty. "
            "With status=missing_information: non-empty; goal-contract lists may be empty.")),
    }
    schema = create_model("GoalPreparation", __config__=ConfigDict(extra="forbid"), **fields)
    stage_info: dict[str, object] = {}
    if execution.prompt_snapshot.get("output") is not None:
        stage_info["final_output_contract"] = Prompt(
            execution.agent.plugin_manager,
            execution.request.settings,
            prompt_dict=deepcopy({
                key: execution.prompt_snapshot[key]
                for key in ("output", "output_format", "ensure_all_keys")
                if key in execution.prompt_snapshot
            }),
        ).to_text()
    instructions = [
        "Infer only [input.execution_stage_input.missing_fields] from the original request, "
        "declared constraints and available context. Preserve explicit goals and success criteria; do not return replacements.",
        *(["Include [info.stage_information.final_output_contract] when interpreting the requested outcome; "
           "it describes the final deliverable, not this response."] if stage_info else []),
        "Do not invent scope, business thresholds or required facts. Report unavailable facts in [output.missing_information].",
        "Do not execute the task, generate its deliverable, or claim effects in this stage.",
        "Return only this stage's declared result; do not expose hidden chain-of-thought.",
    ]
    prompt = deepcopy(dict(execution.prompt_snapshot))
    prompt["input"] = {
        "original_input": DataFormatter.sanitize(prompt.get("input")),
        "execution_stage_input": {"missing_fields": missing},
    }
    info = [] if prompt.get("info") is None else [prompt["info"]]
    if stage_info:
        info.append({"stage_information": stage_info})
    if info:
        prompt["info"] = info
    else:
        prompt.pop("info", None)
    original_instruct = prompt.get("instruct")
    prompt["instruct"] = ([original_instruct] if original_instruct is not None else []) + [
        {"agent_execution_stage": instructions}
    ]
    preparation = run_model_stage(
        execution, producer="long_task", stage="goal_preparation",
        stage_input={"missing_fields": missing},
        stage_info=stage_info, stage_instructions=instructions,
        output=schema, inherit_extension_handlers=False, prompt_projection=prompt,
    )
    # The nested task does not exist yet, so its clock cannot bound this work.
    stage = await await_route_with_limits(execution, preparation, enforce_execution_deadline=True)
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
