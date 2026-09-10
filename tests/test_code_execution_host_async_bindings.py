from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from agently.builtins.plugins.CodeRuntimeAdapter import PythonCodeRuntimeAdapter
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
    DockerExecutionResourceProvider,
)
from agently.builtins.plugins.ExecutionResourceProvider._code_execution_binding_bridge import (
    CODE_EXECUTION_BINDING_CLIENT_MODULE,
    CODE_EXECUTION_BINDING_PROTOCOL,
    CodeExecutionBindingBridge,
    python_binding_client_source,
)
from agently.builtins.plugins.ExecutionResourceProvider._code_execution_binding_process import (
    run_bounded_binding_process,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import (
    CodeExecutionBinding,
    CodeExecutionBindingLimits,
    CodeExecutionRequest,
    TaskWorkspaceAccessRequirement,
    code_execution_json_bytes,
    validate_code_execution_json_schema_definition,
    validate_code_execution_json_schema,
)


def _limits(**overrides: Any) -> CodeExecutionBindingLimits:
    values = {
        "max_program_bytes": 64 * 1024,
        "max_frame_bytes": 16 * 1024,
        "max_request_bytes": 4 * 1024,
        "max_response_bytes": 4 * 1024,
        "max_total_bytes": 16 * 1024,
        "max_calls": 8,
        "max_protocol_frames": 16,
        "max_parallel_calls": 3,
        "max_log_bytes": 1024,
        "max_log_lines": 16,
        "call_timeout_seconds": 1,
        "frame_timeout_seconds": 1,
        "drain_timeout_seconds": 1,
    }
    values.update(overrides)
    return CodeExecutionBindingLimits(**values)


def _binding(
    handler: Any,
    *,
    key: str = "echo",
    concurrency_mode: Literal["parallel", "exclusive"] = "exclusive",
    output_schema: dict[str, Any] | None = None,
) -> CodeExecutionBinding:
    return CodeExecutionBinding(
        binding_key=key,
        async_handler=handler,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema=output_schema
        or {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        concurrency_mode=concurrency_mode,
    )


async def _send_frame(
    bridge: CodeExecutionBindingBridge,
    frame: dict[str, Any],
) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(
        str(bridge.host_socket_path),
        limit=bridge.limits.max_frame_bytes + 1,
    )
    writer.write(
        json.dumps(
            frame,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return json.loads(raw)


def _call_frame(
    bridge: CodeExecutionBindingBridge,
    *,
    request_id: str,
    binding_key: str = "echo",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": CODE_EXECUTION_BINDING_PROTOCOL,
        "token": bridge.token,
        "request_id": request_id,
        "type": "call",
        "binding_key": binding_key,
        "arguments": arguments or {"value": 1},
    }


def _completion_frame(
    bridge: CodeExecutionBindingBridge,
    *,
    request_id: str = "complete-1",
    value: Any = None,
    logs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": CODE_EXECUTION_BINDING_PROTOCOL,
        "token": bridge.token,
        "request_id": request_id,
        "type": "complete",
        "ok": True,
        "value": value,
        "logs": logs or [],
        "logs_truncated": False,
    }


def test_binding_contract_rejects_implicit_json_conversion_and_unsupported_schema() -> None:
    async def handler(arguments: Any) -> Any:
        return arguments

    with pytest.raises(ValueError, match="unsupported JSON Schema keyword"):
        CodeExecutionBinding(
            binding_key="unsafe",
            async_handler=handler,
            input_schema={"type": "object", "unevaluatedProperties": False},
            output_schema={},
        )

    with pytest.raises(TypeError, match="lossless JSON"):
        validate_code_execution_json_schema((1, 2), {"type": "array"})

    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(TypeError, match="cyclic"):
        code_execution_json_bytes(cyclic)

    with pytest.raises(ValueError, match="minimum"):
        validate_code_execution_json_schema_definition({"type": "integer", "minimum": "zero"})

    limits = _limits()
    with pytest.raises(ValueError, match="max_protocol_frames"):
        CodeExecutionBindingLimits(
            max_calls=8,
            max_protocol_frames=8,
        )
    assert limits.max_calls == 8
    assert limits.max_parallel_calls == 3

    with pytest.raises(ValueError, match="concurrency_mode"):
        _binding(handler, concurrency_mode="unsafe")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_parallel_calls"):
        _limits(max_parallel_calls=0)


@pytest.mark.asyncio
async def test_bridge_serializes_concurrent_calls_and_records_only_bounded_facts(
    tmp_path: Path,
) -> None:
    active = 0
    max_active = 0

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return {"value": arguments["value"] + 1}
        finally:
            active -= 1

    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    try:
        responses = await asyncio.gather(
            _send_frame(
                bridge,
                _call_frame(bridge, request_id="call-1", arguments={"value": 1}),
            ),
            _send_frame(
                bridge,
                _call_frame(bridge, request_id="call-2", arguments={"value": 2}),
            ),
        )
        assert [response["value"] for response in responses] == [
            {"value": 2},
            {"value": 3},
        ]
        assert max_active == 1
        assert [record["sequence"] for record in bridge.call_records] == [1, 2]
        assert all(record["status"] == "success" for record in bridge.call_records)
        assert all("arguments" not in record for record in bridge.call_records)

        completed = await _send_frame(
            bridge,
            _completion_frame(
                bridge,
                value={"total": 5},
                logs=["aggregated"],
            ),
        )
        assert completed["ok"] is True
        assert bridge.program_completion == {
            "ok": True,
            "value": {"total": 5},
            "logs": ["aggregated"],
            "logs_truncated": False,
        }
        assert bridge.summary["successful_calls"] == 2
    finally:
        await bridge.async_close(cancel_active=True)


@pytest.mark.asyncio
async def test_bridge_overlaps_parallel_bindings_and_honors_exclusive_barriers(
    tmp_path: Path,
) -> None:
    active = 0
    max_active = 0
    events: list[str] = []

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        value = arguments["value"]
        active += 1
        max_active = max(max_active, active)
        events.append(f"start:{value}")
        try:
            await asyncio.sleep(0.04 if value < 3 else 0.01)
            return {"value": value + 1}
        finally:
            events.append(f"end:{value}")
            active -= 1

    bridge = CodeExecutionBindingBridge(
        bindings=[
            _binding(handler, key="parallel", concurrency_mode="parallel"),
            _binding(handler, key="exclusive", concurrency_mode="exclusive"),
        ],
        limits=_limits(max_parallel_calls=2),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    try:
        responses = await asyncio.gather(
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id="parallel-1",
                    binding_key="parallel",
                    arguments={"value": 1},
                ),
            ),
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id="parallel-2",
                    binding_key="parallel",
                    arguments={"value": 2},
                ),
            ),
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id="exclusive-3",
                    binding_key="exclusive",
                    arguments={"value": 3},
                ),
            ),
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id="parallel-4",
                    binding_key="parallel",
                    arguments={"value": 4},
                ),
            ),
        )
        assert [response["value"] for response in responses] == [
            {"value": 2},
            {"value": 3},
            {"value": 4},
            {"value": 5},
        ]
        assert max_active == 2
        assert events.index("start:3") > events.index("end:1")
        assert events.index("start:3") > events.index("end:2")
        assert events.index("start:4") > events.index("end:3")
        assert [record["sequence"] for record in bridge.call_records] == [1, 2, 3, 4]
        assert [record["concurrency_mode"] for record in bridge.call_records] == [
            "parallel",
            "parallel",
            "exclusive",
            "parallel",
        ]
    finally:
        await bridge.async_close(cancel_active=True)


@pytest.mark.asyncio
async def test_bridge_bounds_parallel_pressure_and_releases_failed_exclusive_barrier(
    tmp_path: Path,
) -> None:
    active = 0
    peak_active = 0
    failed_exclusive_finished = asyncio.Event()

    async def parallel_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        try:
            await asyncio.sleep(0.015)
            return {"value": arguments["value"] + 1}
        finally:
            active -= 1

    async def exclusive_handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        assert active == 0
        try:
            raise RuntimeError("synthetic exclusive failure")
        finally:
            failed_exclusive_finished.set()

    bridge = CodeExecutionBindingBridge(
        bindings=[
            _binding(parallel_handler, key="parallel", concurrency_mode="parallel"),
            _binding(exclusive_handler, key="exclusive", concurrency_mode="exclusive"),
        ],
        limits=_limits(max_calls=16, max_protocol_frames=40, max_parallel_calls=3),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    try:
        before = [
            asyncio.create_task(
                _send_frame(
                    bridge,
                    _call_frame(
                        bridge,
                        request_id=f"before-{value}",
                        binding_key="parallel",
                        arguments={"value": value},
                    ),
                )
            )
            for value in range(6)
        ]
        exclusive = asyncio.create_task(
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id="exclusive-failure",
                    binding_key="exclusive",
                    arguments={"value": 10},
                ),
            )
        )
        after = asyncio.create_task(
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id="after-11",
                    binding_key="parallel",
                    arguments={"value": 11},
                ),
            )
        )
        before_responses = await asyncio.gather(*before)
        exclusive_response = await exclusive
        after_response = await after

        assert all(response["ok"] is True for response in before_responses)
        assert exclusive_response["ok"] is False
        assert exclusive_response["error"]["status"] == "error"
        assert failed_exclusive_finished.is_set()
        assert after_response["value"] == {"value": 12}
        assert peak_active == 3
        assert active == 0
    finally:
        await bridge.async_close(cancel_active=True)


@pytest.mark.asyncio
async def test_bridge_rejects_unknown_invalid_duplicate_and_post_settlement_frames(
    tmp_path: Path,
) -> None:
    handler_calls = 0

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return {"wrong": arguments["value"]}

    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    try:
        unknown = await _send_frame(
            bridge,
            _call_frame(
                bridge,
                request_id="unknown-1",
                binding_key="not-offered",
            ),
        )
        assert unknown["ok"] is False
        assert unknown["error"]["status"] == "rejected"

        invalid = await _send_frame(
            bridge,
            _call_frame(
                bridge,
                request_id="invalid-1",
                arguments={"value": "not-an-integer"},
            ),
        )
        assert invalid["ok"] is False
        assert invalid["error"]["status"] == "rejected"
        assert handler_calls == 0

        bad_output = await _send_frame(
            bridge,
            _call_frame(bridge, request_id="bad-output-1"),
        )
        assert bad_output["ok"] is False
        assert bad_output["error"]["status"] == "error"
        assert bad_output["error"]["message"] == ("Host binding result violates its declared JSON contract.")
        assert handler_calls == 1

        duplicate = await _send_frame(
            bridge,
            _call_frame(bridge, request_id="bad-output-1"),
        )
        assert duplicate["ok"] is False
        assert "Duplicate" in duplicate["error"]["message"]
        assert handler_calls == 1

        forged = _completion_frame(bridge, request_id="forged-complete")
        forged["unexpected"] = True
        forged_response = await _send_frame(bridge, forged)
        assert forged_response["ok"] is False
        assert bridge.program_completion is None

        assert (
            await _send_frame(
                bridge,
                _completion_frame(bridge, request_id="complete-valid", value={}),
            )
        )["ok"] is True
        post_settlement = await _send_frame(
            bridge,
            _call_frame(bridge, request_id="post-settlement"),
        )
        assert post_settlement["ok"] is False
        assert "settled" in post_settlement["error"]["message"]
        assert bridge.summary["rejected_frames"] >= 5
    finally:
        await bridge.async_close(cancel_active=True)


@pytest.mark.asyncio
async def test_bridge_cancellation_propagates_and_drains_started_handler(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Future()
            raise AssertionError("unreachable")
        finally:
            cancelled.set()

    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(call_timeout_seconds=10),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    call = asyncio.create_task(_send_frame(bridge, _call_frame(bridge, request_id="call-cancel")))
    await asyncio.wait_for(started.wait(), timeout=1)
    await bridge.async_close(cancel_active=True)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.gather(call, return_exceptions=True)
    assert bridge.call_records[0]["status"] == "cancelled"
    assert not bridge.socket_path.exists()


@pytest.mark.asyncio
async def test_bridge_cancellation_marks_active_parallel_and_queued_exclusive_calls(
    tmp_path: Path,
) -> None:
    parallel_started = asyncio.Event()
    parallel_cancelled = 0
    exclusive_started = False
    active = 0

    async def parallel_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, parallel_cancelled
        active += 1
        if active == 2:
            parallel_started.set()
        try:
            await asyncio.Future()
            raise AssertionError("unreachable")
        finally:
            parallel_cancelled += 1
            active -= 1

    async def exclusive_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal exclusive_started
        exclusive_started = True
        return arguments

    bridge = CodeExecutionBindingBridge(
        bindings=[
            _binding(parallel_handler, key="parallel", concurrency_mode="parallel"),
            _binding(exclusive_handler, key="exclusive", concurrency_mode="exclusive"),
        ],
        limits=_limits(call_timeout_seconds=10, max_parallel_calls=2),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    calls = [
        asyncio.create_task(
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id=f"parallel-{value}",
                    binding_key="parallel",
                    arguments={"value": value},
                ),
            )
        )
        for value in (1, 2)
    ]
    await asyncio.wait_for(parallel_started.wait(), timeout=1)
    calls.append(
        asyncio.create_task(
            _send_frame(
                bridge,
                _call_frame(
                    bridge,
                    request_id="exclusive-3",
                    binding_key="exclusive",
                    arguments={"value": 3},
                ),
            )
        )
    )
    while len(bridge.call_records) < 3:
        await asyncio.sleep(0)

    await bridge.async_close(cancel_active=True)
    await asyncio.gather(*calls, return_exceptions=True)

    assert parallel_cancelled == 2
    assert active == 0
    assert exclusive_started is False
    assert [record["status"] for record in bridge.call_records] == [
        "cancelled",
        "cancelled",
        "cancelled",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [True, False])
async def test_bridge_close_drains_within_budget_or_cancels(tmp_path: Path, finish: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        started.set()
        try:
            await release.wait()
            return arguments
        finally:
            settled.set()

    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(call_timeout_seconds=10, drain_timeout_seconds=0.05),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    call = asyncio.create_task(_send_frame(bridge, _call_frame(bridge, request_id="drain")))
    await asyncio.wait_for(started.wait(), timeout=1)
    closing = asyncio.create_task(bridge.async_close(cancel_active=False))
    if finish:
        release.set()
    await asyncio.wait_for(closing, timeout=1)
    await asyncio.gather(call, return_exceptions=True)
    assert settled.is_set()
    assert bridge.call_records[0]["status"] == ("success" if finish else "cancelled")
    assert not bridge.socket_path.exists()


@pytest.mark.asyncio
async def test_bridge_close_cleans_up_idle_connections(tmp_path: Path) -> None:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return arguments

    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(frame_timeout_seconds=10),
        socket_path=tmp_path / "runtime" / "bridge.sock",
        container_socket_path="/workspace/build/runtime/bridge.sock",
    )
    await bridge.async_start()
    reader, writer = await asyncio.open_unix_connection(str(bridge.host_socket_path))

    async def wait_for_client() -> None:
        while not bridge._client_tasks:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_client(), timeout=1)
    await asyncio.wait_for(bridge.async_close(cancel_active=True), timeout=1)
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    assert not bridge._client_tasks
    assert not bridge.socket_path.exists()


@pytest.mark.asyncio
async def test_injected_python_client_reports_separate_value_and_ordered_logs(
    tmp_path: Path,
) -> None:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments["value"] + 10}

    runtime_dir = tmp_path / "runtime"
    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(),
        socket_path=runtime_dir / "bridge.sock",
        container_socket_path=str(runtime_dir / "bridge.sock"),
    )
    await bridge.async_start()
    (runtime_dir / CODE_EXECUTION_BINDING_CLIENT_MODULE).write_text(
        python_binding_client_source(),
        encoding="utf-8",
    )
    script = tmp_path / "program.py"
    script.write_text(
        """import asyncio
from agently_code_bindings import call_binding, execute_program

async def program():
    print("first")
    result = await call_binding("echo", {"value": 2})
    print("second")
    return result

asyncio.run(execute_program(program))
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        **bridge.environment,
        "AGENTLY_CODE_BINDING_SOCKET": str(bridge.host_socket_path),
        "PYTHONPATH": str(runtime_dir),
    }
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert stdout == b""
        assert bridge.program_completion == {
            "ok": True,
            "value": {"value": 12},
            "logs": ["first", "second"],
            "logs_truncated": False,
        }
    finally:
        await bridge.async_close(cancel_active=True)


@pytest.mark.parametrize(
    ("file_descriptor", "error_text"),
    [(1, "unframed stdout"), (2, "stderr")],
)
@pytest.mark.asyncio
async def test_framed_stdio_transport_terminates_unframed_output_flood(
    tmp_path: Path,
    file_descriptor: int,
    error_text: str,
) -> None:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return arguments

    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(),
        socket_path=tmp_path / "unused.sock",
        container_socket_path="/unused.sock",
        transport="stdio_framed",
    )
    await bridge.async_start()
    try:
        result, transport_error = await run_bounded_binding_process(
            [
                sys.executable,
                "-c",
                f"import os; os.write({file_descriptor}, b'x' * 4096)",
            ],
            bridge=bridge,
            timeout=3,
            max_output_bytes=128,
        )
    finally:
        await bridge.async_close(cancel_active=True)

    assert result.returncode != 0
    assert len(result.stdout) <= 128
    assert error_text in transport_error


@pytest.mark.asyncio
async def test_framed_stdio_client_returns_value_without_host_storage_channel(
    tmp_path: Path,
) -> None:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments["value"] + 30}

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / CODE_EXECUTION_BINDING_CLIENT_MODULE).write_text(
        python_binding_client_source(),
        encoding="utf-8",
    )
    script = tmp_path / "stdio-program.py"
    script.write_text(
        """import asyncio
from agently_code_bindings import call_binding, execute_program

async def program():
    print("stdio-log")
    return await call_binding("echo", {"value": 2})

asyncio.run(execute_program(program))
""",
        encoding="utf-8",
    )
    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(),
        socket_path=tmp_path / "unused.sock",
        container_socket_path="/unused.sock",
        transport="stdio_framed",
    )
    await bridge.async_start()
    try:
        result, transport_error = await run_bounded_binding_process(
            [sys.executable, str(script)],
            bridge=bridge,
            timeout=5,
            max_output_bytes=1024,
            env={
                **os.environ,
                **bridge.environment,
                "PYTHONPATH": str(runtime_dir),
            },
        )
    finally:
        await bridge.async_close(cancel_active=True)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert transport_error == ""
    assert bridge.program_completion is not None
    assert bridge.program_completion["value"] == {"value": 32}
    assert bridge.program_completion["logs"] == ["stdio-log"]


@pytest.mark.asyncio
async def test_framed_stdio_client_multiplexes_parallel_binding_responses(
    tmp_path: Path,
) -> None:
    active = 0
    max_active = 0

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.04)
            return {"value": arguments["value"] + 100}
        finally:
            active -= 1

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / CODE_EXECUTION_BINDING_CLIENT_MODULE).write_text(
        python_binding_client_source(),
        encoding="utf-8",
    )
    script = tmp_path / "stdio-parallel-program.py"
    script.write_text(
        """import asyncio
from agently_code_bindings import call_binding, execute_program

async def program():
    return await asyncio.gather(*[
        call_binding("parallel", {"value": value})
        for value in range(3)
    ])

asyncio.run(execute_program(program))
""",
        encoding="utf-8",
    )
    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler, key="parallel", concurrency_mode="parallel")],
        limits=_limits(max_parallel_calls=3),
        socket_path=tmp_path / "unused.sock",
        container_socket_path="/unused.sock",
        transport="stdio_framed",
    )
    await bridge.async_start()
    try:
        result, transport_error = await run_bounded_binding_process(
            [sys.executable, str(script)],
            bridge=bridge,
            timeout=5,
            max_output_bytes=1024,
            env={
                **os.environ,
                **bridge.environment,
                "PYTHONPATH": str(runtime_dir),
            },
        )
    finally:
        await bridge.async_close(cancel_active=True)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert transport_error == ""
    assert max_active == 3
    assert bridge.program_completion is not None
    assert bridge.program_completion["value"] == [
        {"value": 100},
        {"value": 101},
        {"value": 102},
    ]


@pytest.mark.asyncio
async def test_framed_stdio_frame_limit_becomes_a_terminal_transport_error(
    tmp_path: Path,
) -> None:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return arguments

    bridge = CodeExecutionBindingBridge(
        bindings=[_binding(handler)],
        limits=_limits(max_calls=1, max_protocol_frames=2),
        socket_path=tmp_path / "unused.sock",
        container_socket_path="/unused.sock",
        transport="stdio_framed",
    )
    await bridge.async_start()

    def raw_frame(request_id: str) -> bytes:
        return (
            json.dumps(
                _call_frame(
                    bridge,
                    request_id=request_id,
                    binding_key="not-offered",
                ),
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    try:
        assert await bridge.async_process_stdio_frame(raw_frame("frame-1"))
        assert await bridge.async_process_stdio_frame(raw_frame("frame-2"))
        with pytest.raises(RuntimeError, match="protocol_frame_limit_exhausted"):
            await bridge.async_process_stdio_frame(raw_frame("frame-3"))
    finally:
        await bridge.async_close(cancel_active=True)


@pytest.mark.asyncio
async def test_docker_probe_reports_concrete_host_async_binding_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_availability",
        lambda self: {"available": True, "reason": "synthetic"},
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_image",
        lambda self, image: {"exists": True, "image": image},
    )

    probe = await DockerExecutionResourceProvider().async_probe(
        requirement={
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
        },
        policy={},
    )

    capabilities = probe.get("capabilities", {})
    meta = probe.get("meta", {})
    assert capabilities["host_async_bindings"] is True
    assert meta["host_async_binding_protocol"] == (CODE_EXECUTION_BINDING_PROTOCOL)


@pytest.mark.asyncio
async def test_real_docker_binding_path_when_local_runtime_is_available(
    tmp_path: Path,
) -> None:
    probe_resource = DockerExecutionResource()
    availability = probe_resource.inspect_availability()
    if not availability.get("available"):
        pytest.skip("local Docker daemon is unavailable")
    image = "python:3.12-slim"
    if not probe_resource.inspect_image(image).get("exists"):
        pytest.skip(f"required local Docker image is unavailable: {image}")

    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="binding-run")
    grant = workspace.issue_execution_access(
        action_call_id="program",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    source = """import asyncio
from agently_code_bindings import call_binding, execute_program

async def program():
    print("inside-container")
    return await call_binding("echo", {"value": 7})

asyncio.run(execute_program(program))
"""
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(language="python", source_code=source),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments["value"] + 1}

    resource = DockerExecutionResource(
        workspace_grant=grant,
        runtime_profile={
            "image": image,
            "image_pull_policy": "never",
            "network_mode": "disabled",
        },
    )
    try:
        result = await resource.async_execute_code(
            bundle=bundle,
            manifest=manifest,
            grant=grant,
            timeout=10,
            bindings=[_binding(handler)],
            binding_limits=_limits(max_log_bytes=12_000),
        )
    finally:
        await resource.async_close()
        workspace.close_execution_access(grant.grant_id)

    assert result["ok"] is True, json.dumps(result, indent=2, ensure_ascii=False)
    assert result["value"] == {"value": 8}
    assert result["logs"] == ["inside-container"]
    assert result["meta"]["binding"]["summary"]["successful_calls"] == 1
    assert result["meta"]["binding"]["transport"] == "stdio_framed"
    assert resource._active_containers == set()
    assert resource._active_binding_bridges == set()
