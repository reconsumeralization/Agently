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

from collections.abc import Iterable
from pathlib import Path

from agently.core.operation.ExecutionResource import ExecutionResourceError


def materialize_task_workspace_boundary(
    candidates: Iterable[object | None],
    *,
    label: str,
) -> str | None:
    """Materialize a TaskWorkspace-issued file boundary into the provider context.

    Resolves the first usable candidate root, ensures it exists and is a writable
    directory, and returns it as an execution-ready working directory. The
    failure for a missing or unusable boundary belongs at the ExecutionResource
    provider boundary, not inside each Bash/Node/Python executor (spec section
    8.6): if a candidate root is supplied but cannot be materialized safely, this
    raises ``ExecutionResourceError`` before the operation starts.

    Returns ``None`` only when no candidate boundary was supplied at all; the
    caller decides whether that is allowed (in-process sandboxes that do not
    touch the filesystem) or must itself fail closed (file-touching executors).
    """

    primary: Path | None = None
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        if isinstance(candidate, (list, tuple)):
            for entry in candidate:
                if entry not in (None, ""):
                    primary = Path(str(entry)).expanduser().resolve()
                    break
        else:
            primary = Path(str(candidate)).expanduser().resolve()
        if primary is not None:
            break
    if primary is None:
        return None
    try:
        primary.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExecutionResourceError(
            f"{label} could not materialize the TaskWorkspace file boundary at {primary}: {exc}",
            code="execution_resource.file_boundary_unavailable",
            payload={"boundary": str(primary)},
        ) from exc
    if not primary.is_dir():
        raise ExecutionResourceError(
            f"{label} TaskWorkspace file boundary is not a directory: {primary}",
            code="execution_resource.file_boundary_invalid",
            payload={"boundary": str(primary)},
        )
    return str(primary)
