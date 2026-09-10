"""Field-packet adapter owned by the existing LongOutputDelivery."""

import json
from copy import copy, deepcopy

from agently.utils import StreamingJSONParser

from . import long_output as native
from .structured_continuation_state import StructuredContinuationState


def strict_packet(raw):
    evidence = StreamingJSONParser._inspect_json_prefix(raw, terminal_complete=True)
    value = evidence.closed.get(())
    if not evidence.root_complete or not isinstance(value, dict):
        raise ValueError("One complete JSON object required")
    return value


class StructuredStringContinuation:
    """No separate execution, budget, retry graph or persistence owner."""

    def __init__(self, delivery):
        self.delivery = delivery
        self.field_state = None

    @property
    def current_digest(self):
        if self.field_state is None:
            return native._sha256_text(native._canonical_json(self.delivery.value))
        return native._sha256_text(native._canonical_json(self.field_state.snapshot()))

    async def accept_initial(self, result, *, streaming_events):
        raw = await result.async_get_text()
        if self.delivery.is_stage:
            self.delivery.storage_id = f"{self.delivery.execution.id}/{result.id}"
        self.field_state = StructuredContinuationState(self.delivery, raw)
        self.delivery.value = deepcopy(self.field_state.state)
        response_id = str(result.response_id or result.id)
        await self.delivery._persist_raw_segment(raw, response_id=response_id)
        await self.delivery._persist_unit(
            slot=None,
            operation="seed",
            index=0,
            value=raw,
            response_id=response_id,
            completion_source="observed_boundary",
        )
        await self.delivery._persist_manifest()

    def _build_continuation_request(self):
        assert self.field_state is not None
        request = self.field_state.request()
        if self.delivery.repair_feedback:
            request.info({"repair_feedback": self.delivery.repair_feedback})
            request.prompt.append(
                "instruct",
                "Apply [info.repair_feedback] to this response; retain all accepted field content.",
            )
        return request

    async def request_and_commit_next(self):
        assert self.field_state is not None
        request = self._build_continuation_request()
        packet_model = request.prompt.to_output_model()
        binding = (self.delivery.revision, self.current_digest)
        result = request.get_result(
            parent_run_context=self.delivery.execution.agent_execution_run_context
        )
        self.delivery.request_count += 1
        self.delivery.execution.record_model_response_id(result.id)
        # This stream is consumed for raw retention and trusted response boundaries,
        # not for business effects. No nested parser retry is dispatched.
        async for _event in result.get_async_generator(type="instant"):
            pass
        raw = await result.async_get_text()
        observed_meta = await result.async_get_meta()
        terminal = native.normalized_terminal(observed_meta)
        self.delivery.segment_index += 1
        response_id = str(result.response_id or result.id)
        await self.delivery._persist_raw_segment(raw, response_id=response_id)
        if binding != (self.delivery.revision, self.current_digest):
            raise native.LongOutputError("Stale inline manifest binding")
        before = self.current_digest
        rejected_reason = None
        try:
            evidence = StreamingJSONParser._inspect_json_prefix(raw)
            if evidence.root_complete:
                payload = strict_packet(raw)
            else:
                if terminal == "complete":
                    raise ValueError(
                        "Normally stopped response has an unfinished private packet"
                    )
                updates = {
                    path[1]: value
                    for path, value in evidence.closed.items()
                    if len(path) == 2
                    and path[0] == "updates"
                    and isinstance(path[1], str)
                }
                if not updates:
                    raise ValueError("No complete field update in partial response")
                payload = {"updates": updates, "completion": "incomplete"}
            # Validate control separately so a later bad field cannot erase a
            # previously valid contiguous prefix. Raw framing must still be valid.
            if set(payload) != {"updates", "completion"} or not isinstance(
                payload["updates"], dict
            ):
                raise ValueError("Invalid packet control shape")
            if payload["completion"] not in {"complete", "incomplete", "undetermined"}:
                raise ValueError("Invalid completion")
            if payload["completion"] == "undetermined":
                raise native.LongOutputError(
                    "Continuation is undetermined; no blind retry is authorized"
                )
            from pydantic import TypeAdapter

            update_specs = {
                field.alias: field
                for field in packet_model.model_fields[
                    "updates"
                ].annotation.model_fields.values()
            }
            accepted = {}
            for key, value in payload["updates"].items():
                try:
                    if key not in update_specs:
                        raise ValueError("Unauthorized field")
                    TypeAdapter(update_specs[key].annotation).validate_python(value)
                    trial = copy(self.field_state)
                    trial.registry = dict(self.field_state.registry)
                    trial.accept(
                        {
                            "updates": {**accepted, key: value},
                            "completion": "incomplete",
                        },
                        validate_final=False,
                    )
                    accepted[key] = value
                except (TypeError, ValueError) as error:
                    rejected_reason = str(error)[:500]
                    break
            if rejected_reason:
                if not accepted:
                    raise ValueError(rejected_reason)
                payload = {"updates": accepted, "completion": "incomplete"}
            self.field_state.accept(payload, validate_final=False)
        except (TypeError, ValueError) as error:
            await self.delivery._record_no_progress(
                response_id=response_id,
                provider_terminal=terminal,
                reason_code="continuation_envelope_invalid",
                reason=str(error)[:500],
                action="Return one complete JSON response matching the declared output, with no trailing text. Correct the invalid field only; never repeat accepted prefixes.",
            )
            return {"is_final": False, "progress": False, "terminal": terminal}
        self.delivery.value = deepcopy(self.field_state.state)
        changed = self.current_digest != before
        final = payload["completion"] == "complete"
        if changed:
            await self.delivery._persist_unit(
                slot=None,
                operation="field_packet",
                index=len(self.delivery.units),
                value=payload,
                response_id=response_id,
                completion_source="observed_boundary",
            )
            await self.delivery._persist_manifest()
            self.delivery.continuation_unit_count += 1
            self.delivery.no_progress_count = 0
            self.delivery.repair_feedback = None
            if not self.delivery.structured:
                delta = "".join(
                    item["value"] for item in payload["updates"].values() if item
                )
                await self.delivery.execution.emit_stream(
                    "model.text",
                    delta,
                    route="model_request",
                    source="long_output",
                    delta=delta,
                    event_type="delta",
                    is_complete=True,
                    meta={
                        "stream_kind": "text",
                        "manifest_revision": self.delivery.revision,
                        "committed": True,
                    },
                )
        elif not final:
            await self.delivery._record_no_progress(
                response_id=response_id,
                provider_terminal=terminal,
                reason_code="continuation_no_complete_update",
                reason="No field content or completion changed",
                action="Append the missing field content or confirm completion without filler.",
            )
        if rejected_reason:
            self.delivery.repair_feedback = {
                "reason_code": "continuation_update_rejected",
                "reason": rejected_reason,
                "action": "Correct the rejected field; preserve accepted fields and omit their previous increments.",
            }
            self.delivery.execution.diagnostics.setdefault(
                "long_output_rejected_updates", []
            ).append(
                {"reason": rejected_reason, "manifest_revision": self.delivery.revision}
            )
        return {"is_final": final, "progress": changed, "terminal": terminal}

    async def _replay_latest_manifest(self):
        manifest = json.loads(
            await self.delivery._read_verified_ref(
                self.delivery.current_manifest_ref, label="manifest"
            )
        )
        if (
            manifest["execution_id"] != self.delivery.execution.id
            or manifest["revision"] != self.delivery.revision
            or manifest["units"] != self.delivery.units
        ):
            raise native.LongOutputError("Stale manifest inventory or lineage")
        replay = None
        for index, unit in enumerate(self.delivery.units):
            if unit["unit_index"] != index or unit["index"] != index:
                raise native.LongOutputError("Unit sequence mismatch")
            text = await self.delivery._read_verified_ref(
                unit["ref"], label=f"unit {index}"
            )
            if native._sha256_text(text) != unit["digest"]:
                raise native.LongOutputError("Unit digest mismatch")
            value = json.loads(text)
            if index == 0 and unit["operation"] == "seed":
                replay = StructuredContinuationState(self.delivery, value)
            elif replay is not None and unit["operation"] == "field_packet":
                replay.offer()
                replay.accept(value, validate_final=False)
            else:
                raise native.LongOutputError("Invalid replay operation")
        if (
            replay is None
            or native._sha256_text(native._canonical_json(replay.snapshot()))
            != manifest["digest"]
        ):
            raise native.LongOutputError("Replayed state mismatch")
        if manifest["digest"] != self.current_digest:
            raise native.LongOutputError("In-memory state mismatch")
        try:
            replay.validate()
        except ValueError as error:
            raise native._FinalValidationError(str(error)) from error
        self.delivery.replayed_unit_count = len(self.delivery.units)
        return replay.state

    def validate_ensure_keys(self, candidate):
        """Schema names are literal; only explicit runtime paths use dot/slash syntax."""
        assert self.field_state is not None
        missing = object()
        use_declared = self.delivery.ensure_keys != []

        def check(declaration, path):
            value = native._get_path(candidate, path, missing)
            if isinstance(declaration, tuple) and declaration:
                policy = (
                    native.DataPathBuilder.get_ensure_policy(declaration[2])
                    if len(declaration) > 2
                    else None
                )
                if (
                    use_declared
                    and policy
                    and (
                        value is missing
                        or policy == "not_null"
                        and not self.delivery._ensure_value_is_present(value)
                    )
                ):
                    raise native._FinalValidationError(f"Missing declared field {path}")
                declaration, _ = native._unwrap_output_declaration(declaration)
            if isinstance(declaration, dict):
                for key, child in declaration.items():
                    check(child, (*path, key))
            elif isinstance(declaration, list) and isinstance(value, list):
                for index in range(len(value)):
                    check(declaration[0], (*path, index))

        check(self.field_state.schema, ())
        declared_policies = self.delivery._active_ensure_policies()
        for path in self.delivery.ensure_keys or []:
            value = native.DataLocator.locate_path_in_dict(
                candidate, path, self.delivery.key_style, default=missing
            )
            if value is missing or (
                declared_policies.get(path) == "not_null"
                and not self.delivery._ensure_value_is_present(value)
            ):
                raise native._FinalValidationError(
                    f"Missing explicit ensure path {path}"
                )
