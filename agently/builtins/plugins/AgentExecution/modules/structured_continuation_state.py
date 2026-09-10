"""Private field-local state for lossless structured-string continuation."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from agently.core.model import Prompt

from . import long_output as native


@dataclass
class Slot:
    key: str
    path: tuple[str | int, ...]
    contract: dict[str, Any]
    model: type[BaseModel] | None


from agently.utils import StreamingJSONParser


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: Any
    is_complete: bool


def escape_matches(pending, text):
    """JSON lexical compatibility, not a guess about missing business text."""
    if not pending:
        return True
    if not text:
        return False
    char = text[0]
    spellings = [json.dumps(char, ensure_ascii=True)[1:-1]]
    if ord(char) <= 0xFFFF:
        spellings.append("\\u%04x" % ord(char))
    if char == "/":
        spellings.append("\\/")
    return any(spelling.lower().startswith(pending.lower()) for spelling in spellings)


class StructuredContinuationState:
    """Host path/field state only; one request still owns semantic continuation."""

    def __init__(self, owner, raw):
        self.owner = owner
        self.schema = owner.output_schema
        self.structured = owner.structured
        self.closed = set()
        self.pending = {}
        self.registry = {}
        self.slots = []
        self.offered = None
        self.revision = 0
        if self.structured and self.schema is str:
            from pydantic import RootModel

            self.original_model = RootModel[str]
        else:
            self.original_model = (
                owner.validation_prompt.to_output_model(strict_output=True)
                if self.structured
                else None
            )
        if (
            self.original_model
            and getattr(self.original_model, "__pydantic_root_model__", False)
            and isinstance(self.schema, dict)
        ):
            self.schema = self.schema["root"]
        if not self.structured:
            self.state = raw
            self.schema = str
        else:
            evidence = StreamingJSONParser._inspect_json_prefix(raw)
            declaration, _ = native._unwrap_output_declaration(self.schema)
            self.state = (
                []
                if isinstance(declaration, list)
                else {}
                if isinstance(declaration, dict)
                else ""
            )
            # Do not copy parents over children twice, but preserve empty containers.
            for path, value in sorted(
                evidence.closed.items(), key=lambda pair: len(pair[0])
            ):
                self.state = native._set_path(self.state, path, deepcopy(value))
                self.closed.add(path)
            if evidence.open_string_path is not None:
                self.state = native._set_path(
                    self.state, evidence.open_string_path, evidence.decoded_prefix
                )
                if evidence.pending_escape:
                    self.pending[evidence.open_string_path] = evidence.pending_escape
        self._refresh()

    def _slot(self, declaration, path):
        if path in self.closed:
            return
        if path not in self.registry:
            model = Prompt(
                self.owner.request.plugin_manager,
                self.owner.request.settings,
                prompt_dict={"output": {"value": declaration}, "output_format": "json"},
                name="ContinuationFieldContract",
            ).to_output_model(strict_output=True)
            self.registry[path] = Slot(
                f"p{len(self.registry)}:{native._path_to_dot(path) or '$'}",
                path,
                native.output_schema_to_json_schema(declaration, strict_output=True),
                model,
            )
        self.slots.append(self.registry[path])

    def _complete(self, declaration, path):
        if path in self.closed:
            return True
        if isinstance(declaration, tuple) and len(declaration) > 3:
            contract = declaration[3].get(native.PYDANTIC_CONTRACT_META_KEY, {})
            missing = object()
            if (
                contract.get("required") is False
                and native._get_path(self.state, path, missing) is missing
            ):
                return True
        declared, _ = native._unwrap_output_declaration(declaration)
        if isinstance(declared, dict):
            return all(
                self._complete(child, (*path, name)) for name, child in declared.items()
            )
        if isinstance(declared, list):
            value = native._get_path(self.state, path)
            if not isinstance(value, list):
                return False
            return all(
                self._complete(declared[0], (*path, index))
                for index in range(len(value))
            )
        return path in self.closed

    def _discover(self, declaration, path):
        if path in self.closed:
            return
        declared, constraints = native._unwrap_output_declaration(declaration)
        if isinstance(declared, dict):
            if not declared:
                self._slot(declaration, path)
            for name, child in declared.items():
                self._discover(child, (*path, name))
        elif isinstance(declared, list):
            value = native._get_path(self.state, path, [])
            if not isinstance(value, list):
                raise ValueError("Array prefix has wrong type")
            maximum = constraints.get("maxItems")
            if maximum is not None and len(value) > maximum:
                raise ValueError("Array prefix exceeds original maximum")
            if native._get_path(self.state, path) is None:
                self._slot(declaration, path)
            partial = False
            for index in range(len(value)):
                if not self._complete(declared[0], (*path, index)):
                    if index != len(value) - 1:
                        raise ValueError("Non-tail incomplete array item")
                    self._discover(declared[0], (*path, index))
                    partial = True
            if not partial and (maximum is None or len(value) < maximum):
                # Only the next index is offered, never sparse future positions.
                self._discover(declared[0], (*path, len(value)))
        else:
            self._slot(declaration, path)

    def _refresh(self):
        self.slots = []
        self._discover(self.schema, ())
        for slot in self.slots:
            current = native._get_path(self.state, slot.path)
            maximum = slot.contract.get("maxLength")
            if (
                isinstance(current, str)
                and maximum is not None
                and len(current) > maximum
            ):
                raise ValueError("Initial prefix exceeds original maximum")

    def _text_contract(self, slot):
        if slot.contract.get("type") == "string":
            return slot.contract
        if isinstance(native._get_path(self.state, slot.path), str):
            for branch in slot.contract.get("anyOf", []):
                if branch.get("type") == "string":
                    return branch
        return None

    def offer(self):
        """Freeze the next authorized slots without constructing a model request."""
        self._refresh()
        self.offered = (
            self.revision,
            native._sha256_text(native._canonical_json(self.snapshot())),
            list(self.slots),
        )

    def request(self):
        self.offer()
        context = deepcopy(self.state)
        fields = {}
        for slot in self.slots:
            current = native._get_path(self.state, slot.path)
            text = self._text_contract(slot) is not None
            fields[slot.key] = {
                "value_contract": slot.contract,
                "operation": "append_text" if text else "set_value",
            }
            if slot.contract.get("type") == "array":
                fields[slot.key]["operation"] = "initialize_array"
            if text:
                fields[slot.key]["accepted_prefix"] = (
                    current if isinstance(current, str) else ""
                )
                if current is not None:
                    context = native._set_path(
                        context, slot.path, {"content_location": "input.fields"}
                    )
                if slot.path in self.pending:
                    fields[slot.key]["pending_json_escape"] = self.pending[slot.path]
        request = self.owner.execution.agent.create_request(
            name="StructuredStringContinuation",
            inherit_agent_prompt=False,
            inherit_extension_handlers=False,
            model_key=getattr(self.owner.request, "_model_key", None),
        )
        local_settings = self.owner.request.settings.get(inherit=False)
        if isinstance(local_settings, dict):
            request.settings.update(deepcopy(local_settings))
        request.prompt.update(deepcopy(self.owner.prompt_snapshot))
        request.prompt.set("tools", None)
        request.prompt.set("action_results", None)
        request.input(
            {
                "original_input": self.owner.prompt_snapshot.get("input"),
                "completed_context": context,
                "fields": fields,
            }
        )
        request.info(
            {
                "original_deliverable_instructions": self.owner.prompt_snapshot.get(
                    "instruct"
                )
            }
        )
        request.prompt.set(
            "instruct",
            [
                "Continue the current deliverable in [input.original_input], following [info.original_deliverable_instructions]. Original output references name business fields, not this private response.",
                "For offered fields in [input.fields], append only missing text after accepted_prefix, or set the missing non-string value. Keep the exact join natural; do not repeat or rewrite accepted content. completed_context is read-only.",
                "The Host builds the business objects and arrays. Return field content, not JSON container syntax or encoded JSON suffixes. Omit offered next-array-item fields if no further item is needed; do not invent extra items.",
                "initialize_array creates a missing array before item fields are applied. Its value is []; item field updates in the same response may then populate that array.",
                "The value_contract applies to the assembled field. Mark is_complete only when the field is finished. Empty text can confirm completion; do not add filler or later workflow content. For pending_json_escape, the first added character must finish that observed escape; return the decoded character, not escape spelling.",
                "Return updates keyed only by offered field keys. Return completion as complete, incomplete or undetermined for the whole deliverable after the updates; undetermined means the available context is insufficient to continue safely.",
            ],
        )
        from typing import Literal

        typed_fields = {}
        for index, slot in enumerate(self.slots):
            if self._text_contract(slot) is not None:
                annotation = str
                definition = Field(
                    description="Only new text after accepted_prefix; empty if no text remains.",
                    max_length=4000,
                )
            elif slot.contract.get("type") == "array":
                annotation = list[Any]
                definition = Field(
                    description="Initialize the missing array with []; item field updates may populate it.",
                    max_length=0,
                )
            else:
                annotation = slot.model.model_fields["value"].annotation
                definition = deepcopy(slot.model.model_fields["value"])
            field_model = create_model(
                f"ContinuationUpdate{index}",
                __config__=ConfigDict(extra="forbid"),
                value=(annotation, definition),
                is_complete=(
                    bool,
                    Field(
                        description="Whether this field is complete after the increment."
                    ),
                ),
            )
            if slot.contract.get("type") == "array":
                field_model = create_model(
                    f"ContinuationInitialize{index}",
                    __config__=ConfigDict(extra="forbid"),
                    value=(annotation, definition),
                )
            typed_fields[f"field_{index}"] = (
                field_model | None,
                Field(
                    default=None,
                    alias=slot.key,
                    description="An update to this offered field, or null when not needed.",
                ),
            )
        updates_model = create_model(
            "ContinuationUpdates", __config__=ConfigDict(extra="forbid"), **typed_fields
        )
        Packet = create_model(
            "ContinuationPacket",
            __config__=ConfigDict(extra="forbid"),
            updates=(updates_model, ...),
            completion=(Literal["complete", "incomplete", "undetermined"], ...),
        )
        request.output(Packet, format="json")
        return request

    def snapshot(self):
        return {
            "value": self.state,
            "closed": [list(path) for path in sorted(self.closed, key=str)],
            "pending": [
                {"path": list(path), "raw": raw} for path, raw in self.pending.items()
            ],
        }

    def accept(self, payload, *, validate_final=True):
        if self.offered is None:
            raise ValueError("No request-bound offer")
        revision, digest, active = self.offered
        if revision != self.revision or digest != native._sha256_text(
            native._canonical_json(self.snapshot())
        ):
            raise ValueError("Stale request-bound state")
        allowed = {slot.key: slot for slot in active}
        updates = payload["updates"]
        if not isinstance(updates, dict) or not set(updates) <= set(allowed):
            raise ValueError("Unauthorized fields")
        if payload["completion"] not in {"complete", "incomplete", "undetermined"}:
            raise ValueError("Invalid completion")
        if payload["completion"] == "undetermined":
            raise ValueError("Insufficient evidence; do not retry blindly")
        candidate, closed, pending = (
            deepcopy(self.state),
            set(self.closed),
            dict(self.pending),
        )
        # Parent initialization precedes child updates regardless of JSON key order.
        for key, raw_update in sorted(
            updates.items(), key=lambda item: len(allowed[item[0]].path)
        ):
            if raw_update is None:
                continue
            slot = allowed[key]
            update = Update.model_validate(
                {"is_complete": True, **raw_update}
                if slot.contract.get("type") == "array"
                else raw_update
            )
            text_contract = self._text_contract(slot)
            if text_contract is not None:
                if not isinstance(update.value, str) or len(update.value) > 4000:
                    raise ValueError("Text block must be string at most 4000 chars")
                if not escape_matches(pending.get(slot.path, ""), update.value):
                    raise ValueError("New character conflicts with retained escape")
                old = native._get_path(candidate, slot.path, "")
                combined = (old if isinstance(old, str) else "") + update.value
                maximum = text_contract.get("maxLength")
                if maximum is not None and len(combined) > maximum:
                    raise ValueError("Original maximum exceeded")
                pending.pop(slot.path, None)
            else:
                if any(path[: len(slot.path)] == slot.path for path in closed):
                    raise ValueError(
                        "Cannot replace a container with accepted descendants"
                    )
                old = native._get_path(candidate, slot.path)
                if isinstance(old, str) and old:
                    raise ValueError(
                        "Unsupported partial string contract cannot be replaced"
                    )
                if not update.is_complete:
                    raise ValueError("Non-string value must be closed")
                combined = deepcopy(update.value)
                if slot.contract.get("type") == "array":
                    if (
                        combined != []
                        or native._get_path(candidate, slot.path) is not None
                    ):
                        raise ValueError(
                            "Only a missing array can be initialized with []"
                        )
            if update.is_complete:
                if slot.contract.get("type") != "array":
                    slot.model.model_validate({"value": combined})
                closed.add(slot.path)
            candidate = native._set_path(candidate, slot.path, combined)
        self.state, self.closed, self.pending = candidate, closed, pending
        self.revision += 1
        self.offered = None
        self._refresh()
        if payload["completion"] == "complete" and validate_final:
            self.validate()
        return {"is_final": payload["completion"] == "complete", "state": self.state}

    def validate(self):
        if self.pending or not self._complete(self.schema, ()):
            raise ValueError("Open fields remain")
        if self.original_model:
            self.original_model.model_validate(self.state)
        return self.state
