# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping
from typing import Any, Literal, cast

from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.operation.Action.ActionProgram import (
    build_programmatic_action_catalog,
    build_programmatic_python_source,
    canonical_lossless_json_bytes,
)
from agently.core.runtime import (
    get_current_agent_execution_context,
    get_current_tool_phase_run_context,
)
from agently.types.data import (
    PROGRAMMATIC_ACTION_ARTIFACT_READ_ID,
    PROGRAMMATIC_ACTION_TRANSPORT_ID,
    ActionResult,
    CodeExecutionBinding,
    CodeExecutionBindingError,
    CodeExecutionBindingLimits,
    CodeExecutionRequest,
    TaskWorkspaceAccessGrant,
)


class ProgrammaticActionExecutor:
    """Execute one bounded program whose bindings re-enter ActionRuntime."""

    name = "ProgrammaticActionExecutor"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    kind = "programmatic_action"
    sandboxed = False
    resource_isolation_managed = True

    def __init__(self, *, action: Any, timeout: int = 60) -> None:
        self.action = action
        self.timeout = timeout
        self.adapter = get_code_runtime_adapter("python")

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    @staticmethod
    def _positive_int(settings: Any, key: str, default: int) -> int:
        value = settings.get(key, default)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default

    @staticmethod
    def _positive_float(settings: Any, key: str, default: float) -> float:
        value = settings.get(key, default)
        return (
            float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default
        )

    def _binding_limits(
        self,
        settings: Any,
        *,
        program_source_bytes: int,
    ) -> CodeExecutionBindingLimits:
        max_calls = self._positive_int(
            settings,
            "action.programmatic.max_subcalls",
            128,
        )
        return CodeExecutionBindingLimits(
            # The model-body limit is validated before wrapping. The generic
            # provider owns the complete immutable source/bundle boundary.
            max_program_bytes=max(1, program_source_bytes),
            max_request_bytes=self._positive_int(
                settings,
                "action.programmatic.max_binding_value_bytes",
                4 * 1024 * 1024,
            ),
            max_response_bytes=self._positive_int(
                settings,
                "action.programmatic.max_binding_value_bytes",
                4 * 1024 * 1024,
            ),
            max_frame_bytes=self._positive_int(
                settings,
                "action.programmatic.max_binding_frame_bytes",
                4 * 1024 * 1024,
            ),
            max_total_bytes=self._positive_int(
                settings,
                "action.programmatic.max_total_binding_bytes",
                32 * 1024 * 1024,
            ),
            max_calls=max_calls,
            max_protocol_frames=max(max_calls + 1, max_calls * 2 + 8),
            max_log_bytes=self._positive_int(
                settings,
                "action.programmatic.max_log_bytes",
                64 * 1024,
            ),
            max_log_lines=self._positive_int(
                settings,
                "action.programmatic.max_log_lines",
                1024,
            ),
            call_timeout_seconds=self._positive_float(
                settings,
                "action.programmatic.subcall_timeout",
                30.0,
            ),
            frame_timeout_seconds=self._positive_float(
                settings,
                "action.programmatic.frame_timeout",
                30.0,
            ),
            drain_timeout_seconds=self._positive_float(
                settings,
                "action.programmatic.drain_timeout",
                5.0,
            ),
        )

    def _resolve_catalog(self, revision: str) -> dict[str, Any]:
        runtime = self.action.action_runtime
        resolver = getattr(runtime, "resolve_programmatic_catalog", None)
        catalog = resolver(revision) if callable(resolver) else None
        if not isinstance(catalog, dict):
            raise ValueError("Programmatic Action catalog is missing or stale.")
        entries = catalog.get("entries", [])
        if not isinstance(entries, list) or not entries:
            raise ValueError("Programmatic Action catalog has no eligible bindings.")

        current_specs: list[dict[str, Any]] = []
        registration_versions = catalog.get("_registration_versions", {})
        if not isinstance(registration_versions, dict):
            raise ValueError("Programmatic Action catalog registration snapshot is invalid.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Programmatic Action catalog entry is invalid.")
            action_id = str(entry.get("action_id", ""))
            expected_registration_version = registration_versions.get(action_id)
            current_registration_version = self.action.action_registry._registration_version(action_id)
            if (
                not isinstance(expected_registration_version, int)
                or current_registration_version != expected_registration_version
            ):
                raise ValueError(
                    f"Programmatic Action catalog is stale: Action '{action_id}' " "was removed or replaced."
                )
            current_spec = self.action.action_registry.get_spec(action_id)
            if current_spec is None:
                raise ValueError(f"Programmatic Action catalog is stale: Action '{action_id}' is unavailable.")
            current_specs.append(dict(current_spec))

        current_catalog = build_programmatic_action_catalog(
            current_specs,
            revision_seed=catalog.get("_revision_seed"),
        )
        if current_catalog.get("catalog_revision") != revision:
            raise ValueError("Programmatic Action catalog changed before execution.")

        execution_context = get_current_agent_execution_context()
        scoped_action_ids = getattr(execution_context, "scoped_action_ids", None)
        raw_allowed = scoped_action_ids() if callable(scoped_action_ids) else None
        allowed = {str(item) for item in raw_allowed} if isinstance(raw_allowed, set) else set()
        if allowed:
            disallowed = sorted(
                str(entry.get("action_id", ""))
                for entry in entries
                if str(entry.get("action_id", "")) not in allowed | {PROGRAMMATIC_ACTION_ARTIFACT_READ_ID}
            )
            if disallowed:
                raise ValueError(
                    "Programmatic Action catalog is stale for the current execution scope: "
                    + ", ".join(disallowed)
                    + "."
                )
        return catalog

    async def _emit_subcall_observation(
        self,
        *,
        kind: str,
        action_id: str,
        arguments: Mapping[str, Any],
        subcall_index: int,
        parent_action_call_id: str,
        run: Any,
        record: ActionResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "action_type": "programmatic_subcall",
            "action_name": action_id,
            "command_index": subcall_index,
            "program_subcall_index": subcall_index,
            "parent_action_call_id": parent_action_call_id,
            "planning_protocol": "programmatic",
            "command": {
                "action_id": action_id,
                "action_input": dict(arguments),
                "source_protocol": "programmatic",
            },
        }
        if record is not None:
            payload["record"] = record
        await self.action._async_emit_action_flow_observation(
            {
                "kind": kind,
                "source": "ProgrammaticActionExecutor",
                "level": "WARNING" if kind not in {"action_started", "action_completed"} else "INFO",
                "message": f"Programmatic Action subcall '{action_id}' {kind.removeprefix('action_')}.",
                "payload": payload,
                "error": error,
                "run": run,
                "compat_event_family": None,
            }
        )

    def _bindings(
        self,
        *,
        catalog: dict[str, Any],
        settings: Any,
        parent_action_call_id: str,
    ) -> tuple[tuple[CodeExecutionBinding, ...], list[ActionResult]]:
        artifact_scope = self.action._artifact_manager.current_artifact_scope()
        if not isinstance(artifact_scope, dict):
            raise RuntimeError("Programmatic Action requires the enclosing Action artifact scope.")
        counter = 0
        bindings: list[CodeExecutionBinding] = []
        subcall_records: list[ActionResult] = []
        parent_run = get_current_tool_phase_run_context()

        for raw_entry in catalog.get("entries", []):
            entry = dict(raw_entry)
            action_id = str(entry["action_id"])
            registration_versions = catalog.get("_registration_versions", {})
            expected_registration_version = (
                registration_versions.get(action_id) if isinstance(registration_versions, dict) else None
            )

            async def dispatch(
                arguments: Mapping[str, Any],
                *,
                _action_id: str = action_id,
                _expected_registration_version: Any = expected_registration_version,
            ) -> Any:
                nonlocal counter
                counter += 1
                subcall_index = counter
                subcall_run = (
                    parent_run.create_child(
                        run_kind="action",
                        meta={
                            "action_type": "programmatic_subcall",
                            "action_name": _action_id,
                            "program_subcall_index": subcall_index,
                            "parent_action_call_id": parent_action_call_id,
                            "planning_protocol": "programmatic",
                        },
                    )
                    if parent_run is not None
                    else None
                )
                await self._emit_subcall_observation(
                    kind="action_started",
                    action_id=_action_id,
                    arguments=arguments,
                    subcall_index=subcall_index,
                    parent_action_call_id=parent_action_call_id,
                    run=subcall_run,
                )
                started = time.monotonic()
                try:
                    if (
                        not isinstance(_expected_registration_version, int)
                        or self.action.action_registry._registration_version(_action_id)
                        != _expected_registration_version
                    ):
                        raise CodeExecutionBindingError(
                            f"Action '{_action_id}' changed after the program was planned.",
                            status="blocked",
                        )
                    value, record = await self.action._async_execute_program_binding_action(
                        _action_id,
                        dict(arguments),
                        settings=settings,
                        purpose=f"Programmatic subcall {subcall_index}: {_action_id}",
                        artifact_scope=artifact_scope,
                    )
                except BaseException as error:
                    await self._emit_subcall_observation(
                        kind="action_failed",
                        action_id=_action_id,
                        arguments=arguments,
                        subcall_index=subcall_index,
                        parent_action_call_id=parent_action_call_id,
                        run=subcall_run,
                        error=error,
                    )
                    raise

                meta = record.get("meta")
                if not isinstance(meta, dict):
                    meta = {}
                    record["meta"] = meta
                meta["programmatic_parent_action_call_id"] = parent_action_call_id
                meta["programmatic_subcall_index"] = subcall_index
                meta["programmatic_elapsed_ms"] = int((time.monotonic() - started) * 1000)
                context = get_current_agent_execution_context()
                record_action_records = getattr(context, "record_action_records", None)
                if callable(record_action_records):
                    bounded_records = self.action._to_action_flow_return_records([record])
                    evidence_record = bounded_records[0] if bounded_records else record
                    record_action_records(
                        [
                            {
                                **evidence_record,
                                "command_index": subcall_index,
                            }
                        ],
                        source="ProgrammaticActionExecutor",
                    )
                else:
                    bounded_records = self.action._to_action_flow_return_records([record])
                    evidence_record = bounded_records[0] if bounded_records else record
                subcall_records.append(evidence_record)
                status = str(record.get("status", "error"))
                success = bool(record.get("success", record.get("ok", False)))
                if success:
                    event_kind = "action_completed"
                elif status == "approval_required":
                    event_kind = "action_approval_required"
                elif status == "blocked":
                    event_kind = "action_blocked"
                else:
                    event_kind = "action_failed"
                await self._emit_subcall_observation(
                    kind=event_kind,
                    action_id=_action_id,
                    arguments=arguments,
                    subcall_index=subcall_index,
                    parent_action_call_id=parent_action_call_id,
                    run=subcall_run,
                    record=record,
                )
                if not success:
                    error_status = cast(
                        Literal[
                            "error",
                            "blocked",
                            "approval_required",
                            "timed_out",
                            "cancelled",
                        ],
                        (
                            status
                            if status
                            in {
                                "blocked",
                                "approval_required",
                                "timed_out",
                                "cancelled",
                            }
                            else "error"
                        ),
                    )
                    raise CodeExecutionBindingError(
                        str(record.get("error") or f"Action '{_action_id}' failed."),
                        status=error_status,
                    )
                return value

            bindings.append(
                CodeExecutionBinding(
                    binding_key=str(entry["binding_key"]),
                    async_handler=dispatch,
                    input_schema=entry["input_schema"],
                    output_schema=entry["output_schema"],
                )
            )
        return tuple(bindings), subcall_records

    def _subcall_evidence_projection(
        self,
        records: list[ActionResult],
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for record in records:
            visible_records = self.action.to_model_visible_records([record])
            visible = visible_records[0] if visible_records else {}
            projected.append(
                {
                    "action_call_id": str(record.get("action_call_id", "")),
                    "action_id": str(record.get("action_id", "")),
                    "status": str(record.get("status", "")),
                    "success": bool(record.get("success", record.get("ok", False))),
                    "artifact_refs": (
                        list(visible.get("artifact_refs", []))
                        if isinstance(visible, dict) and isinstance(visible.get("artifact_refs"), list)
                        else []
                    ),
                }
            )
        return projected

    @staticmethod
    def _provider_facts(action_call: Mapping[str, Any]) -> dict[str, Any]:
        handles = action_call.get("execution_resource_handles", {})
        handle = handles.get(PROGRAMMATIC_ACTION_TRANSPORT_ID) if isinstance(handles, dict) else None
        if not isinstance(handle, dict):
            return {}
        meta = handle.get("meta")
        return {
            "provider_id": str(handle.get("provider_id", "")),
            "provider_probes": list(meta.get("provider_probes", []))[:20] if isinstance(meta, dict) else [],
        }

    @staticmethod
    def _safe_program_error(result: Mapping[str, Any]) -> str:
        diagnostics = result.get("diagnostics", [])
        if isinstance(diagnostics, list):
            for diagnostic in reversed(diagnostics):
                if not isinstance(diagnostic, dict):
                    continue
                if str(diagnostic.get("code", "")) == "docker_runtime.binding_program_failed":
                    message = str(diagnostic.get("message", "")).strip()
                    if message:
                        return message[:1000]
        status = str(result.get("status", "error") or "error")
        return f"Programmatic Action execution failed with status '{status}'."

    @staticmethod
    def _safe_diagnostics(result: Mapping[str, Any]) -> list[dict[str, Any]]:
        diagnostics = result.get("diagnostics", [])
        if not isinstance(diagnostics, list):
            return []
        projected: list[dict[str, Any]] = []
        for item in diagnostics[:20]:
            if not isinstance(item, dict):
                continue
            projected.append(
                {
                    key: str(item.get(key, ""))[:1000]
                    for key in ("code", "status", "message")
                    if item.get(key) is not None
                }
            )
        return projected

    @staticmethod
    def _outer_action_status(program_status: str, *, ok: bool) -> str:
        if ok and program_status == "success":
            return "success"
        if program_status in {
            "partial_success",
            "error",
            "blocked",
            "approval_required",
            "skipped",
        }:
            return program_status
        return "error"

    @staticmethod
    def _log_artifacts(result: Mapping[str, Any]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for stream_name in ("stdout", "stderr"):
            value = result.get(stream_name)
            if not isinstance(value, str) or not value:
                continue
            artifacts.append(
                {
                    "artifact_type": "program_log",
                    "role": stream_name,
                    "label": f"Programmatic Action {stream_name}",
                    "media_type": "text/plain",
                    "value": value,
                    "available": True,
                }
            )
        return artifacts

    @staticmethod
    def _catalog_artifacts(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "artifact_type": "programmatic_action_sdk",
                "role": "sdk",
                "label": "Programmatic Action Python SDK",
                "media_type": "text/x-python",
                "value": str(catalog.get("sdk", "")),
                "available": True,
            },
            {
                "artifact_type": "programmatic_action_catalog",
                "role": "catalog",
                "label": "Programmatic Action catalog",
                "media_type": "application/json",
                "value": {
                    "renderer_version": catalog.get("renderer_version", ""),
                    "catalog_revision": catalog.get("catalog_revision", ""),
                    "entries": list(catalog.get("entries", [])),
                },
                "available": True,
            },
        ]

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = spec
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            raise TypeError("Programmatic Action input must be an object.")
        program = action_input.get("program")
        description = action_input.get("description")
        revision = str(action_input.get("catalog_revision", ""))
        if not isinstance(program, str) or not program.strip():
            raise ValueError("Programmatic Action program must be non-empty.")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Programmatic Action description must be non-empty.")
        catalog = self._resolve_catalog(revision)
        wrapper_source = build_programmatic_python_source(program)

        workspace = action_call.get("task_workspace")
        if not isinstance(workspace, TaskWorkspace):
            raise TypeError("Programmatic Action requires a TaskWorkspace binding.")
        grants = action_call.get("task_workspace_access_grants", {})
        grant = grants.get(PROGRAMMATIC_ACTION_TRANSPORT_ID) if isinstance(grants, dict) else None
        if not isinstance(grant, TaskWorkspaceAccessGrant):
            raise TypeError("Programmatic Action requires a TaskWorkspace execution grant.")
        resources = action_call.get("execution_resource_resources", {})
        resource = resources.get(PROGRAMMATIC_ACTION_TRANSPORT_ID) if isinstance(resources, dict) else None
        if resource is None or not hasattr(resource, "async_execute_code"):
            raise RuntimeError("Binding-capable code execution resource is unavailable.")

        request = CodeExecutionRequest.create(
            language="python",
            source_code=wrapper_source,
            provenance={
                "kind": "programmatic_action",
                "catalog_revision": revision,
                "description": description[:1024],
            },
        )
        bundle = self.adapter.prepare(
            request,
            policy={"dependency_install": "deny"},
        )
        manifest = await workspace.materialize_execution_bundle(grant, bundle)
        limits = self._binding_limits(
            settings,
            program_source_bytes=len(wrapper_source.encode("utf-8")),
        )
        bindings, subcall_records = self._bindings(
            catalog=catalog,
            settings=settings,
            parent_action_call_id=str(action_call.get("action_call_id", "")),
        )
        configured_timeout = self._positive_int(
            settings,
            "action.programmatic.timeout",
            self.timeout,
        )
        timeout_raw = policy.get("timeout_seconds", configured_timeout)
        timeout = (
            int(timeout_raw)
            if isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool) and timeout_raw > 0
            else configured_timeout
        )
        try:
            raw_result = await resource.async_execute_code(
                bundle=bundle,
                manifest=manifest,
                grant=grant,
                timeout=timeout,
                bindings=bindings,
                binding_limits=limits,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            data = {
                "value": None,
                "logs": [],
                "logs_truncated": False,
                "subcall_evidence": self._subcall_evidence_projection(subcall_records),
            }
            return {
                "ok": False,
                "status": "error",
                "data": data,
                "result": data,
                "meta": {
                    **self._provider_facts(action_call),
                    "planning_protocol": "programmatic",
                    "program_status": "provider_exception",
                    "program_language": "python",
                    "program_digest": "sha256:" + hashlib.sha256(program.encode("utf-8")).hexdigest(),
                    "program_bytes": len(program.encode("utf-8")),
                    "catalog_revision": revision,
                    "programmatic_subcall_records": subcall_records,
                    "bundle_id": bundle.bundle_id,
                    "bundle_digest": bundle.bundle_digest,
                    "grant_id": grant.grant_id,
                },
                "diagnostics": [
                    {
                        "code": "programmatic_action.provider_exception",
                        "status": "error",
                        "message": (
                            "The isolated program provider failed after dispatch; "
                            "retained subcall evidence remains authoritative."
                        ),
                        "meta": {"exception_type": type(error).__name__},
                    }
                ],
                "artifacts": self._catalog_artifacts(catalog),
                "error": (
                    "The isolated program provider failed; inspect retained "
                    "subcall evidence and provider diagnostics."
                ),
            }
        result = dict(raw_result) if isinstance(raw_result, dict) else {}
        ok = bool(result.get("ok"))
        program_status = str(result.get("status", "success" if ok else "error"))
        outer_status = self._outer_action_status(
            program_status,
            ok=ok,
        )
        value = result.get("value")
        logs = result.get("logs", [])
        if not isinstance(logs, list):
            logs = []
        canonical_lossless_json_bytes(
            value,
            max_bytes=self._positive_int(
                settings,
                "action.programmatic.max_output_bytes",
                1024 * 1024,
            ),
            label="programmatic Action result",
        )
        program_digest = "sha256:" + hashlib.sha256(program.encode("utf-8")).hexdigest()
        data = {
            "value": value,
            "logs": [str(item) for item in logs],
            "logs_truncated": bool(result.get("logs_truncated", False)),
            "subcall_evidence": self._subcall_evidence_projection(subcall_records),
        }
        result_meta = result.get("meta")
        binding_meta = result_meta.get("binding", {}) if isinstance(result_meta, dict) else {}
        if not isinstance(binding_meta, dict):
            binding_meta = {}
        meta = {
            **(dict(result_meta) if isinstance(result_meta, dict) else {}),
            **self._provider_facts(action_call),
            "planning_protocol": "programmatic",
            "program_status": program_status,
            "program_language": "python",
            "program_digest": program_digest,
            "program_bytes": len(program.encode("utf-8")),
            "catalog_revision": revision,
            "binding_summary": result.get(
                "binding_summary",
                binding_meta.get("summary", {}),
            ),
            "binding_calls": result.get(
                "binding_calls",
                binding_meta.get("calls", []),
            ),
            "programmatic_subcall_records": subcall_records,
            "program_log_refs": list(result.get("log_refs", [])) if isinstance(result.get("log_refs"), list) else [],
            "bundle_id": bundle.bundle_id,
            "bundle_digest": bundle.bundle_digest,
            "grant_id": grant.grant_id,
        }
        return {
            "ok": ok,
            "status": outer_status,
            "data": data,
            "result": data,
            "meta": meta,
            "artifacts": [
                *self._catalog_artifacts(catalog),
                *self._log_artifacts(result),
            ],
            "diagnostics": self._safe_diagnostics(result),
            "error": "" if ok else self._safe_program_error(result),
        }


__all__ = ["ProgrammaticActionExecutor"]
