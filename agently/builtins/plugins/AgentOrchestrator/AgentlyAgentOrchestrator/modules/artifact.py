# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING, cast

from agently.types.data import (
    AgentArtifactContext,
    AgentArtifactHandler,
    TaskWorkspaceFileRef,
)
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


def declare_artifact(
    execution: "AgentExecution",
    path: str | os.PathLike[str],
    handler: "AgentArtifactHandler | None",
) -> "AgentExecution":
    target = execution._reconfiguration_target()
    path_text = os.fspath(path)
    if not isinstance(path_text, str) or not path_text.strip():
        raise ValueError("Agent artifact path must be a non-empty string path.")
    if handler is not None and not callable(handler):
        raise TypeError("Agent artifact handler must be callable or None.")
    target.artifact_declarations.append(
        {
            "path": path_text,
            "handler": handler,
        }
    )
    return target


async def run_declared_artifacts(execution: "AgentExecution", result: Any) -> None:
    declarations = list(execution.artifact_declarations)
    for index, declaration in enumerate(declarations, start=1):
        path = str(declaration["path"])
        handler = cast("AgentArtifactHandler | None", declaration.get("handler"))
        artifact_id = f"{execution.id}:artifact:{index}"
        context = AgentArtifactContext(
            execution=execution,
            path=path,
            index=index,
            task_workspace=execution.task_workspace,
        )
        await execution.emit_stream(
            "artifact.started",
            {
                "artifact_id": artifact_id,
                "index": index,
                "requested_path": path,
                "handler": _handler_name(handler),
            },
            route=execution.route_info.get("selected_route"),
            source="agent_artifact",
            meta={"artifact_id": artifact_id, "requested_path": path},
        )
        content = (
            _default_render(result)
            if handler is None
            else await _run_handler(handler, result, context)
        )
        ref = await _materialize_and_verify(execution, path, content)
        execution.artifact_results.append(ref)
        execution.logs.setdefault("artifact_refs", []).append(DataFormatter.sanitize(ref))
        execution._terminal_task_handoff_refs.append(dict(ref))
        _refresh_artifact_diagnostics(execution)
        await execution.emit_stream(
            "artifact.completed",
            ref,
            route=execution.route_info.get("selected_route"),
            source="agent_artifact",
            meta={
                "artifact_id": artifact_id,
                "requested_path": path,
                "path": ref["path"],
                "sha256": ref["sha256"],
            },
        )


async def _run_handler(
    handler: "AgentArtifactHandler",
    result: Any,
    context: AgentArtifactContext,
) -> str | bytes:
    value = handler(result, context)
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, (str, bytes)):
        raise TypeError("Agent artifact handler must return str or bytes.")
    return value


def _default_render(result: Any) -> str:
    if isinstance(result, str):
        return result
    if result is None or isinstance(result, (Mapping, list, tuple, bool, int, float)):
        try:
            return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise TypeError(
                "Agent artifact default rendering requires JSON-compatible data; "
                "provide a handler for custom result types."
            ) from error
    raise TypeError(
        "Agent artifact default rendering supports str or JSON-compatible data; "
        "provide a handler for custom result types."
    )


async def _materialize_and_verify(
    execution: "AgentExecution",
    path: str,
    content: str | bytes,
) -> TaskWorkspaceFileRef:
    expected = content.encode("utf-8") if isinstance(content, str) else content
    expected_sha256 = hashlib.sha256(expected).hexdigest()
    if isinstance(content, str):
        write = await execution.task_workspace.write_file(path, content)
    else:
        write = await execution.task_workspace.materialize_file(
            path,
            content,
            source={"kind": "agent_execution_artifact"},
            overwrite=True,
        )
    readback = await execution.task_workspace.read_file(
        write.path,
        max_bytes=max(1, len(expected) + 1),
    )
    if (
        readback.truncated
        or readback.total_bytes != len(expected)
        or write.bytes != len(expected)
        or write.sha256 != expected_sha256
        or readback.sha256 != expected_sha256
    ):
        raise RuntimeError(
            f"Agent artifact physical readback did not match the rendered content: {path}"
        )
    promoted = await execution.task_workspace._promote_file_identity(
        write.path,
        role="artifact",
    )
    ref = cast(TaskWorkspaceFileRef, dict(promoted))
    ref["complete_readback_verified"] = True
    return ref


def _handler_name(handler: Any) -> str | None:
    if handler is None:
        return None
    return str(getattr(handler, "__name__", None) or handler.__class__.__name__)


def _refresh_artifact_diagnostics(execution: "AgentExecution") -> None:
    execution.diagnostics["artifact"] = {
        "declared": len(execution.artifact_declarations),
        "completed": len(execution.artifact_results),
        "paths": [ref["path"] for ref in execution.artifact_results],
    }


__all__ = ["declare_artifact", "run_declared_artifacts"]
