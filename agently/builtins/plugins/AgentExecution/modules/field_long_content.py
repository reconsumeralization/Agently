"""Explicit field production under one existing Execution, in schema order."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, create_model

from agently.builtins.plugins.PromptGenerator.modules.output_contract import (
    output_schema_to_json_schema,
)
from agently.core.model import Prompt

from .long_content_flow import LongContentExecutionConfig, run_long_content_execution
from .long_output import (
    LongOutputDelivery,
    _set_path,
    _unwrap_output_declaration,
)
from .model_stage import run_model_stage
from .production import ProductionOptions

if TYPE_CHECKING:
    from .execution import AgentExecution

Path = tuple[str | int, ...]


def has_long_content(declaration: Any) -> bool:
    """Inspect explicit metadata only; field names/descriptions are never routes."""
    if isinstance(declaration, type) and issubclass(declaration, BaseModel):
        from agently.builtins.plugins.PromptGenerator.modules.output_contract import (
            pydantic_model_to_output_schema,
        )

        declaration = pydantic_model_to_output_schema(declaration)
    metadata = (
        declaration[3]
        if isinstance(declaration, tuple) and len(declaration) > 3
        else {}
    )
    declared, _ = _unwrap_output_declaration(declaration)
    if isinstance(metadata, Mapping) and "long_content" in metadata:
        marker = metadata["long_content"]
        if not isinstance(marker, bool):
            raise ValueError("long_content field metadata must be a boolean.")
        if marker:
            contract = output_schema_to_json_schema(declaration, strict_output=True)
            if contract.get("type") != "string":
                raise ValueError("long_content can only mark a string field.")
            return True
    if isinstance(declared, Mapping):
        # Do not short-circuit: reject an invalid later declaration before dispatch.
        flags = [has_long_content(child) for child in declared.values()]
        return any(flags)
    if isinstance(declared, list):
        flags = [has_long_content(child) for child in declared]
        if any(flags) and len(declared) != 1:
            raise ValueError('A long-content array must declare one item schema.')
        return any(flags)
    return False


class _FieldProducer:
    def __init__(
        self, execution: AgentExecution, schema: Any, config: LongContentExecutionConfig
    ):
        self.execution = execution
        self.schema = schema
        self.config = config
        self.value: Any = None
        self.stage_index = 0

    def projection(
        self, path: Path, item_context: list[dict[str, Any]]
    ) -> dict[str, Any]:
        original = deepcopy(dict(self.execution.prompt_snapshot))
        for key in (
            "output",
            "output_format",
            "ensure_all_keys",
            "tools",
            "action_results",
        ):
            original.pop(key, None)
        source_info = original.pop("info", None)
        requirements = original.pop("instruct", None)
        original["input"] = {
            "original_input": original.get("input"),
            "accepted_fields": deepcopy(self.value),
            "current_path": list(path),
            "array_item_plans": item_context,
        }
        original["info"] = {
            "source_context": source_info,
            "original_requirements": requirements,
        }
        original["instruct"] = (
            "Follow [input.original_input], [info.source_context] and [info.original_requirements]. "
            "Generate only the current fields declared in [output]. "
            "[input.accepted_fields] contains actual earlier output and partially built containers, "
            "not the final result. Use it as read-only evidence; do not generate other fields. "
            "[input.array_item_plans] scopes the current array item; it is planned content, not an observed fact."
        )
        if self.execution.revision:
            original["info"]["revision_feedback"] = self.execution._rework_feedback
            original["instruct"] += (
                " Apply [info.revision_feedback] to this replacement result."
            )
        return original

    async def request(
        self, schema: Any, path: Path, context: list[dict[str, Any]], *, projection=None
    ) -> Any:
        self.stage_index += 1
        result = await run_model_stage(
            self.execution,
            producer="long_content",
            stage=f"field_group_{self.stage_index}",
            stage_input=None,
            stage_info=None,
            stage_instructions=[],
            output=schema,
            prompt_projection=projection
            if projection is not None
            else self.projection(path, context),
            inherit_extension_handlers=False,
            ensure_long_output=self.execution._ensure_long_output_enabled,
        )
        return result.value

    async def produce(
        self, declaration: Any, path: Path, context: list[dict[str, Any]]
    ) -> None:
        declared, constraints = _unwrap_output_declaration(declaration)
        if not has_long_content(declaration):
            response = await self.request({"value": declaration}, path, context)
            self.value = _set_path(self.value, path, response["value"])
            return
        if isinstance(declared, Mapping):
            self.value = _set_path(self.value, path, {})
            pending: dict[str, Any] = {}

            async def flush() -> None:
                if not pending:
                    return
                response = await self.request(dict(pending), path, context)
                for name in pending:
                    self.value = _set_path(self.value, (*path, name), response[name])
                pending.clear()

            for name, child in declared.items():
                if has_long_content(child):
                    await flush()
                    await self.produce(child, (*path, name), context)
                else:
                    pending[name] = child
            await flush()
        elif isinstance(declared, list):
            if len(declared) != 1:
                raise ValueError("A long-content array must declare one item schema.")
            contract = output_schema_to_json_schema(declaration, strict_output=True)
            projection = self.projection(path, context)
            projection["info"]["array_contract"] = contract
            projection["instruct"] = (
                "Plan only the array at [input.current_path] under [input.original_input] and "
                "[info.original_requirements], using [info.source_context] and [input.accepted_fields]. "
                "Return one brief writing assignment per needed item in order, following "
                "[info.array_contract]. These are plans for subsequent item production, not finished "
                "items or invented source facts. Return an empty list when no items are needed and allowed."
            )
            field = Field(
                min_length=constraints.get("minItems", 0),
                max_length=constraints.get("maxItems"),
                description="Ordered writing assignments for actual array items.",
            )
            schema = create_model("LongContentArrayPlan", items=(list[str], field))
            response = await self.request(schema, path, context, projection=projection)
            plans = schema.model_validate(response).model_dump()['items']
            self.value = _set_path(self.value, path, [])
            for index, brief in enumerate(plans):
                await self.produce(
                    declared[0],
                    (*path, index),
                    [*context, {"path": list(path), "index": index, "brief": brief}],
                )
        else:
            projection = self.projection(path, context)
            projection["input"]["field_contract"] = output_schema_to_json_schema(
                declaration, strict_output=True
            )
            projection["instruct"] = (
                "Produce only the long-form string at [input.current_path], following "
                "[input.field_contract], [input.original_input], [info.original_requirements] and "
                "[info.source_context]. [input.accepted_fields] is read-only actual earlier content; "
                "[input.array_item_plans] scopes this item. Plan and write this field, not the entire "
                "root output or its later fields. Follow the field format; planning titles are not "
                "mandatory output headings. Do not add JSON wrapper syntax to the field prose."
            )
            value = await run_long_content_execution(
                self.execution,
                self.config,
                prompt_projection=projection,
                field_path=path,
            )
            local = Prompt(
                self.execution.request.plugin_manager,
                self.execution.request.settings,
                prompt_dict={"output": {"value": declaration}, "output_format": "json"},
            )
            local.to_output_model(strict_output=True).model_validate({"value": value})
            self.value = _set_path(self.value, path, value)


async def run_field_long_content(
    execution: AgentExecution, options: ProductionOptions
) -> Any:
    """Produce and validate one structured result; final policies remain outside."""
    prompt_object = execution.request.prompt.to_prompt_object()
    if prompt_object.output_format != "json":
        raise ValueError(
            "Field long_content currently requires JSON structured output."
        )
    schema = prompt_object.output
    has_long_content(schema)  # Validate every marker before the first model call.
    maximum = execution.request.settings.get(
        "plugins.AgentExecution.long_content.max_sections", 12
    )
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise ValueError("long_content.max_sections must be a positive integer.")
    producer = _FieldProducer(
        execution, schema, LongContentExecutionConfig(max_sections=maximum)
    )
    await producer.produce(schema, (), [])
    original = execution.request.prompt.to_output_model(
        strict_output=bool(options.ensure_all_keys)
    )
    result_object = original.model_validate(producer.value)
    delivery = LongOutputDelivery(
        execution,
        ensure_keys=options.ensure_keys,
        ensure_all_keys=options.ensure_all_keys,
        validate_handler=None,
        key_style=options.key_style,
        max_retries=0,
        raise_ensure_failure=True,
    )
    delivery.preflight()
    delivery._validate_ensure_keys(producer.value)
    execution._producer_result_object = result_object
    execution._producer_state = {"kind": "field_long_content"}
    return producer.value
