# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agently.types.data import (
    CodeExecutionBinding,
    CodeExecutionBindingError,
    CodeExecutionBindingLimits,
    code_execution_json_bytes,
    normalize_code_execution_json_value,
    validate_code_execution_json_schema,
)


CODE_EXECUTION_BINDING_PROTOCOL = "agently.code-bindings.v1"
CODE_EXECUTION_BINDING_CLIENT_MODULE = "agently_code_bindings.py"
CODE_EXECUTION_BINDING_STDIO_MAGIC = b"\x1eAGENTLY-CODE-BINDING-1:"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESERVED_ENV_KEYS = frozenset(
    {
        "AGENTLY_CODE_BINDING_SOCKET",
        "AGENTLY_CODE_BINDING_TRANSPORT",
        "AGENTLY_CODE_BINDING_TOKEN",
        "AGENTLY_CODE_BINDING_PROTOCOL",
        "AGENTLY_CODE_BINDING_MAX_FRAME_BYTES",
        "AGENTLY_CODE_BINDING_MAX_LOG_BYTES",
        "AGENTLY_CODE_BINDING_MAX_LOG_LINES",
        "AGENTLY_CODE_BINDING_FRAME_TIMEOUT_SECONDS",
        "AGENTLY_CODE_BINDING_RESPONSE_TIMEOUT_SECONDS",
    }
)


def host_async_bindings_supported() -> bool:
    # Docker uses an owned, framed stdin/stdout channel and therefore does not
    # depend on host/container kernel socket sharing or writable host IPC
    # storage. The socket transport exists only for deterministic protocol
    # conformance tests and is not advertised as a production capability.
    return True


def reserved_binding_environment_keys() -> frozenset[str]:
    return _RESERVED_ENV_KEYS


def python_binding_client_source() -> str:
    """Return the provider-owned, Action-agnostic Python IPC client.

    A program bundle still materializes its own immutable wrapper and Action SDK.
    That wrapper imports ``call_binding`` and ``execute_program`` from this
    injected runtime module. The provider never generates an ``actions`` object
    and therefore never learns Action ids, policies, or result envelopes.
    """

    return r'''from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import secrets
import sys
import threading
from typing import Any

_TRANSPORT = os.environ.get("AGENTLY_CODE_BINDING_TRANSPORT", "unix_socket")
_SOCKET = os.environ.get("AGENTLY_CODE_BINDING_SOCKET", "")
_TOKEN = os.environ["AGENTLY_CODE_BINDING_TOKEN"]
_PROTOCOL = os.environ["AGENTLY_CODE_BINDING_PROTOCOL"]
_MAX_FRAME_BYTES = int(os.environ["AGENTLY_CODE_BINDING_MAX_FRAME_BYTES"])
_MAX_LOG_BYTES = int(os.environ["AGENTLY_CODE_BINDING_MAX_LOG_BYTES"])
_MAX_LOG_LINES = int(os.environ["AGENTLY_CODE_BINDING_MAX_LOG_LINES"])
_FRAME_TIMEOUT_SECONDS = float(os.environ["AGENTLY_CODE_BINDING_FRAME_TIMEOUT_SECONDS"])
_RESPONSE_TIMEOUT_SECONDS = float(os.environ["AGENTLY_CODE_BINDING_RESPONSE_TIMEOUT_SECONDS"])
_STDIO_MAGIC = b"\x1eAGENTLY-CODE-BINDING-1:"
_PROTOCOL_INPUT = sys.stdin.buffer
_PROTOCOL_OUTPUT = sys.stdout.buffer
_STDIO_LOOP: asyncio.AbstractEventLoop | None = None
_STDIO_READER_THREAD: threading.Thread | None = None
_STDIO_WRITE_LOCK = threading.Lock()
_STDIO_PENDING: dict[str, asyncio.Future[bytes]] = {}
_STDIO_TERMINAL_REQUEST_ID: str | None = None


class BindingCallError(RuntimeError):
    def __init__(self, *, binding_key: str, status: str, message: str) -> None:
        self.binding_key = binding_key
        self.status = status
        self.public_message = message
        super().__init__(message)


def _normalize_json(value: Any) -> Any:
    active: set[int] = set()

    def normalize(item: Any, depth: int) -> Any:
        if depth > 256:
            raise TypeError("binding value exceeds the JSON nesting limit")
        item_type = type(item)
        if item is None or item_type in {str, bool, int}:
            return item
        if item_type is float:
            if item != item or item in {float("inf"), float("-inf")}:
                raise TypeError("binding values must not contain non-finite numbers")
            return item
        if item_type not in {list, dict}:
            raise TypeError("binding values must contain only exact JSON values")
        identity = id(item)
        if identity in active:
            raise TypeError("binding values must not contain cyclic containers")
        active.add(identity)
        try:
            if item_type is list:
                return [normalize(child, depth + 1) for child in item]
            if any(type(key) is not str for key in item):
                raise TypeError("binding object keys must be exact strings")
            return {key: normalize(child, depth + 1) for key, child in item.items()}
        finally:
            active.discard(identity)

    return normalize(value, 0)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


async def _unix_request(raw: bytes) -> bytes:
    if not _SOCKET:
        raise RuntimeError("binding Unix socket is not configured")
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(_SOCKET, limit=_MAX_FRAME_BYTES + 1),
        timeout=_FRAME_TIMEOUT_SECONDS,
    )
    try:
        writer.write(raw)
        await asyncio.wait_for(writer.drain(), timeout=_FRAME_TIMEOUT_SECONDS)
        return await asyncio.wait_for(
            reader.readline(),
            timeout=_RESPONSE_TIMEOUT_SECONDS,
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def _stdio_frame(payload: bytes) -> bytes:
    return _STDIO_MAGIC + f"{len(payload):08x}".encode("ascii") + b":" + payload


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("binding stdio response ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _blocking_stdio_write(raw: bytes) -> None:
    with _STDIO_WRITE_LOCK:
        _PROTOCOL_OUTPUT.write(_stdio_frame(raw))
        _PROTOCOL_OUTPUT.flush()


def _blocking_stdio_read() -> bytes:
    header = _read_exact(_PROTOCOL_INPUT, len(_STDIO_MAGIC) + 9)
    if not header.startswith(_STDIO_MAGIC) or header[-1:] != b":":
        raise RuntimeError("binding stdio response header is invalid")
    try:
        size = int(header[len(_STDIO_MAGIC):-1], 16)
    except ValueError as error:
        raise RuntimeError("binding stdio response length is invalid") from error
    if size < 1 or size > _MAX_FRAME_BYTES:
        raise RuntimeError("binding stdio response exceeds the frame limit")
    return _read_exact(_PROTOCOL_INPUT, size)


def _resolve_stdio_response(request_id: str, payload: bytes) -> None:
    future = _STDIO_PENDING.pop(request_id, None)
    if future is not None and not future.done():
        future.set_result(payload)


def _fail_stdio_responses(error: BaseException) -> None:
    for request_id, future in list(_STDIO_PENDING.items()):
        _STDIO_PENDING.pop(request_id, None)
        if not future.done():
            future.set_exception(RuntimeError(f"binding stdio response reader failed: {type(error).__name__}"))


def _stdio_reader_main() -> None:
    loop = _STDIO_LOOP
    if loop is None:
        return
    try:
        while True:
            payload = _blocking_stdio_read()
            parsed = json.loads(payload)
            request_id = parsed.get("request_id") if isinstance(parsed, dict) else None
            if not isinstance(request_id, str):
                raise RuntimeError("binding stdio response identity is invalid")
            loop.call_soon_threadsafe(_resolve_stdio_response, request_id, payload)
            if request_id == _STDIO_TERMINAL_REQUEST_ID:
                return
    except BaseException as error:
        loop.call_soon_threadsafe(_fail_stdio_responses, error)


def _ensure_stdio_reader() -> None:
    global _STDIO_LOOP, _STDIO_READER_THREAD
    loop = asyncio.get_running_loop()
    if _STDIO_LOOP is None:
        _STDIO_LOOP = loop
    elif _STDIO_LOOP is not loop:
        raise RuntimeError("binding stdio client cannot span event loops")
    if _STDIO_READER_THREAD is None:
        _STDIO_READER_THREAD = threading.Thread(
            target=_stdio_reader_main,
            name="agently-code-binding-reader",
            daemon=True,
        )
        _STDIO_READER_THREAD.start()


async def _stdio_request(raw: bytes, request_id: str, *, terminal: bool) -> bytes:
    global _STDIO_TERMINAL_REQUEST_ID
    _ensure_stdio_reader()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()
    if request_id in _STDIO_PENDING:
        raise RuntimeError("duplicate binding stdio request id")
    _STDIO_PENDING[request_id] = future
    if terminal:
        _STDIO_TERMINAL_REQUEST_ID = request_id
    try:
        await asyncio.to_thread(_blocking_stdio_write, raw)
        payload = await asyncio.wait_for(future, timeout=_RESPONSE_TIMEOUT_SECONDS)
        if terminal and _STDIO_READER_THREAD is not None:
            await asyncio.to_thread(_STDIO_READER_THREAD.join, _FRAME_TIMEOUT_SECONDS)
            if _STDIO_READER_THREAD.is_alive():
                raise RuntimeError("binding stdio response reader did not settle")
        return payload + b"\n"
    finally:
        _STDIO_PENDING.pop(request_id, None)


async def _request(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = secrets.token_hex(16)
    frame = {
        "protocol": _PROTOCOL,
        "token": _TOKEN,
        "request_id": request_id,
        **payload,
    }
    raw = _json_bytes(frame) + b"\n"
    if len(raw) > _MAX_FRAME_BYTES:
        raise BindingCallError(
            binding_key=str(payload.get("binding_key", "")),
            status="error",
            message="Binding protocol request exceeds the frame limit.",
        )
    if _TRANSPORT == "unix_socket":
        response_raw = await _unix_request(raw)
    elif _TRANSPORT == "stdio_framed":
        response_raw = await _stdio_request(
            raw,
            request_id,
            terminal=payload.get("type") == "complete",
        )
    else:
        raise BindingCallError(
            binding_key=str(payload.get("binding_key", "")),
            status="error",
            message="Binding protocol transport is invalid.",
        )
    if not response_raw.endswith(b"\n") or len(response_raw) > _MAX_FRAME_BYTES:
        raise BindingCallError(
            binding_key=str(payload.get("binding_key", "")),
            status="error",
            message="Binding protocol response is malformed or oversized.",
        )
    try:
        response = json.loads(response_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BindingCallError(
            binding_key=str(payload.get("binding_key", "")),
            status="error",
            message="Binding protocol response is not valid JSON.",
        ) from error
    if (
        not isinstance(response, dict)
        or response.get("protocol") != _PROTOCOL
        or response.get("request_id") != request_id
        or not isinstance(response.get("ok"), bool)
    ):
        raise BindingCallError(
            binding_key=str(payload.get("binding_key", "")),
            status="error",
            message="Binding protocol response identity is invalid.",
        )
    if response["ok"]:
        return response
    error = response.get("error")
    error = error if isinstance(error, dict) else {}
    raise BindingCallError(
        binding_key=str(error.get("binding_key") or payload.get("binding_key") or ""),
        status=str(error.get("status") or "error"),
        message=str(error.get("message") or "Host binding failed.")[:1000],
    )


async def call_binding(binding_key: str, arguments: dict[str, Any]) -> Any:
    if not isinstance(binding_key, str) or not binding_key:
        raise TypeError("binding_key must be a non-empty string")
    if not isinstance(arguments, dict):
        raise TypeError("binding arguments must be an object")
    response = await _request(
        {
            "type": "call",
            "binding_key": binding_key,
            "arguments": arguments,
        }
    )
    return response.get("value")


class _BindingCallable:
    def __init__(self, binding_key: str) -> None:
        self._binding_key = binding_key

    async def __call__(self, arguments: dict[str, Any]) -> Any:
        return await call_binding(self._binding_key, arguments)


class _BindingNamespace:
    def __getitem__(self, binding_key: str) -> _BindingCallable:
        return _BindingCallable(binding_key)

    def __getattr__(self, binding_key: str) -> _BindingCallable:
        if binding_key.startswith("_"):
            raise AttributeError(binding_key)
        return _BindingCallable(binding_key)


bindings = _BindingNamespace()


class _BoundedLogCapture(io.TextIOBase):
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._bytes = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        text = str(value)
        encoded = text.encode("utf-8")
        remaining = _MAX_LOG_BYTES - self._bytes
        if remaining > 0:
            retained = encoded[:remaining].decode("utf-8", errors="ignore")
            self._parts.append(retained)
            self._bytes += len(retained.encode("utf-8"))
        if len(encoded) > remaining:
            self.truncated = True
        return len(text)

    def flush(self) -> None:
        return None

    def logs(self) -> list[str]:
        lines = "".join(self._parts).splitlines()
        if len(lines) > _MAX_LOG_LINES:
            self.truncated = True
            lines = lines[:_MAX_LOG_LINES]
        return lines


async def _complete(
    *,
    ok: bool,
    value: Any = None,
    error: dict[str, str] | None = None,
    capture: _BoundedLogCapture,
) -> None:
    payload: dict[str, Any] = {
        "type": "complete",
        "ok": ok,
        "logs": capture.logs(),
        "logs_truncated": capture.truncated,
    }
    if ok:
        payload["value"] = value
    else:
        payload["error"] = error or {
            "status": "error",
            "message": "Program execution failed.",
        }
    await _request(payload)


async def execute_program(program: Any) -> Any:
    """Execute one zero-argument async callable and report its JSON result."""

    if not callable(program):
        raise TypeError("program must be callable")
    capture = _BoundedLogCapture()
    try:
        with contextlib.redirect_stdout(capture):
            result = await program()
    except Exception as error:
        status = error.status if isinstance(error, BindingCallError) else "error"
        if status not in {
            "error",
            "blocked",
            "approval_required",
            "timed_out",
            "cancelled",
        }:
            status = "error"
        message = (
            error.public_message
            if isinstance(error, BindingCallError)
            else f"Program raised {type(error).__name__}."
        )
        try:
            await _complete(
                ok=False,
                error={"status": status, "message": str(message)[:1000]},
                capture=capture,
            )
        finally:
            raise
    await _complete(ok=True, value=result, capture=capture)
    return result
'''


class _ProtocolFailure(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: str = "rejected",
        binding_key: str = "",
        request_id: str = "",
    ) -> None:
        self.status = status
        self.binding_key = binding_key[:256]
        self.request_id = request_id[:128]
        self.public_message = str(message)[:1000]
        super().__init__(self.public_message)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON numeric constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


class CodeExecutionBindingBridge:
    """One-run hostile-peer JSON binding bridge.

    The random token correlates one program process with one bridge. It is not a
    security boundary: the isolated process receives it and remains untrusted.
    Every frame, binding key, schema, count, byte total, result and settlement is
    independently validated by the host.
    """

    def __init__(
        self,
        *,
        bindings: Sequence[CodeExecutionBinding],
        limits: CodeExecutionBindingLimits,
        socket_path: Path,
        container_socket_path: str,
        transport: str = "unix_socket",
    ) -> None:
        if transport not in {"unix_socket", "stdio_framed"}:
            raise ValueError("unsupported code binding bridge transport")
        if transport == "unix_socket" and not (os.name == "posix" and hasattr(asyncio, "start_unix_server")):
            raise RuntimeError("Host async bindings require POSIX Unix-domain sockets.")
        frozen_bindings = tuple(bindings)
        if not frozen_bindings:
            raise ValueError("binding bridge requires at least one binding")
        if any(not isinstance(item, CodeExecutionBinding) for item in frozen_bindings):
            raise TypeError("bindings must contain CodeExecutionBinding values")
        keys = [item.binding_key for item in frozen_bindings]
        if len(set(keys)) != len(keys):
            raise ValueError("code execution binding keys must be unique")
        self.bindings = {item.binding_key: item for item in frozen_bindings}
        self.limits = limits
        self.socket_path = Path(socket_path)
        self.container_socket_path = str(container_socket_path)
        self.transport = transport
        self._bound_socket_path = self.socket_path
        self._socket_parent_alias: Path | None = None
        self.token = secrets.token_urlsafe(32)
        self._server: asyncio.AbstractServer | None = None
        self._started = False
        self._state_lock = asyncio.Lock()
        self._schedule_condition = asyncio.Condition()
        self._pending_dispatches: dict[int, str] = {}
        self._active_dispatches: set[int] = set()
        self._exclusive_dispatch_active = False
        self._client_tasks: set[asyncio.Task[Any]] = set()
        self._call_tasks: set[asyncio.Task[Any]] = set()
        self._seen_request_ids: set[str] = set()
        self._call_records: dict[int, dict[str, Any]] = {}
        self._accepting = True
        self._closed = False
        self._program_completion: dict[str, Any] | None = None
        self._protocol_frames = 0
        self._rejected_frames = 0
        self._call_count = 0
        self._peak_active_dispatches = 0
        self._request_bytes = 0
        self._response_bytes = 0
        self._transport_error = ""
        self._fatal_protocol_error = ""

    @property
    def environment(self) -> dict[str, str]:
        environment = {
            "AGENTLY_CODE_BINDING_TRANSPORT": self.transport,
            "AGENTLY_CODE_BINDING_TOKEN": self.token,
            "AGENTLY_CODE_BINDING_PROTOCOL": CODE_EXECUTION_BINDING_PROTOCOL,
            "AGENTLY_CODE_BINDING_MAX_FRAME_BYTES": str(self.limits.max_frame_bytes),
            "AGENTLY_CODE_BINDING_MAX_LOG_BYTES": str(self.limits.max_log_bytes),
            "AGENTLY_CODE_BINDING_MAX_LOG_LINES": str(self.limits.max_log_lines),
            "AGENTLY_CODE_BINDING_FRAME_TIMEOUT_SECONDS": str(self.limits.frame_timeout_seconds),
            "AGENTLY_CODE_BINDING_RESPONSE_TIMEOUT_SECONDS": str(
                self.limits.call_timeout_seconds + self.limits.frame_timeout_seconds + self.limits.drain_timeout_seconds
            ),
        }
        if self.transport == "unix_socket":
            environment["AGENTLY_CODE_BINDING_SOCKET"] = self.container_socket_path
        return environment

    @property
    def host_socket_path(self) -> Path:
        """Short host locator used by deterministic local protocol tests."""

        return self._bound_socket_path

    @property
    def program_completion(self) -> dict[str, Any] | None:
        return (
            normalize_code_execution_json_value(self._program_completion)
            if self._program_completion is not None
            else None
        )

    @property
    def call_records(self) -> list[dict[str, Any]]:
        return [dict(self._call_records[key]) for key in sorted(self._call_records)]

    @property
    def summary(self) -> dict[str, int]:
        records = self.call_records
        successful = sum(item.get("status") == "success" for item in records)
        return {
            "protocol_frames": self._protocol_frames,
            "rejected_frames": self._rejected_frames,
            "call_count": len(records),
            "successful_calls": successful,
            "failed_calls": len(records) - successful,
            "request_bytes": self._request_bytes,
            "response_bytes": self._response_bytes,
            "peak_active_calls": self._peak_active_dispatches,
        }

    @property
    def transport_error(self) -> str:
        return self._transport_error

    def record_transport_error(self, error: str) -> None:
        self._transport_error = str(error)[:500]

    async def async_start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("code binding bridge cannot be started twice")
        self._started = True
        if self.transport == "stdio_framed":
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=False)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise FileExistsError(str(self.socket_path))
        # Darwin and Linux cap AF_UNIX path bytes (commonly 104/108). A
        # TaskWorkspace execution root can be much longer. Bind through one
        # short, run-unique parent symlink while leaving the socket inode in the
        # granted build root that is mounted into the container.
        if len(str(self.socket_path).encode("utf-8")) > 96:
            alias = Path("/tmp") / f"agently-bind-{secrets.token_hex(12)}"
            alias.symlink_to(self.socket_path.parent, target_is_directory=True)
            self._socket_parent_alias = alias
            self._bound_socket_path = alias / self.socket_path.name
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._bound_socket_path),
            limit=self.limits.max_frame_bytes + 1,
        )
        self.socket_path.chmod(0o666)

    async def _count_frame(self) -> None:
        async with self._state_lock:
            self._protocol_frames += 1
            if self._protocol_frames > self.limits.max_protocol_frames:
                self._accepting = False
                self._fatal_protocol_error = "protocol_frame_limit_exhausted"
                raise _ProtocolFailure("Binding protocol frame limit exhausted.")

    async def _count_rejection(self) -> None:
        async with self._state_lock:
            self._rejected_frames += 1

    @staticmethod
    def _parse_frame(raw: bytes) -> dict[str, Any]:
        if not raw.endswith(b"\n"):
            raise _ProtocolFailure("Binding protocol frame must end with a newline.")
        try:
            parsed = json.loads(
                raw,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise _ProtocolFailure("Binding protocol frame is not valid canonical JSON.") from error
        if not isinstance(parsed, dict):
            raise _ProtocolFailure("Binding protocol frame must be a JSON object.")
        return parsed

    async def _validate_common(self, frame: Mapping[str, Any]) -> tuple[str, str]:
        if frame.get("protocol") != CODE_EXECUTION_BINDING_PROTOCOL:
            raise _ProtocolFailure("Binding protocol version is invalid.")
        token = frame.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
            raise _ProtocolFailure("Binding protocol correlation is invalid.")
        request_id = frame.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID_PATTERN.fullmatch(request_id):
            raise _ProtocolFailure("Binding protocol request_id is invalid.")
        frame_type = frame.get("type")
        if frame_type not in {"call", "complete"}:
            raise _ProtocolFailure(
                "Binding protocol frame type is invalid.",
                request_id=request_id,
            )
        async with self._state_lock:
            if not self._accepting:
                raise _ProtocolFailure(
                    "Binding protocol run is already settled.",
                    request_id=request_id,
                )
            if request_id in self._seen_request_ids:
                raise _ProtocolFailure(
                    "Duplicate binding protocol request_id.",
                    request_id=request_id,
                )
            self._seen_request_ids.add(request_id)
        return request_id, str(frame_type)

    @staticmethod
    def _error_response(error: _ProtocolFailure) -> dict[str, Any]:
        return {
            "protocol": CODE_EXECUTION_BINDING_PROTOCOL,
            "request_id": error.request_id,
            "ok": False,
            "error": {
                "binding_key": error.binding_key,
                "status": error.status,
                "message": error.public_message,
            },
        }

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        response: Mapping[str, Any],
    ) -> None:
        raw = code_execution_json_bytes(response) + b"\n"
        if len(raw) > self.limits.max_frame_bytes:
            fallback = self._error_response(
                _ProtocolFailure(
                    "Binding protocol response exceeds the frame limit.",
                    request_id=str(response.get("request_id", "")),
                )
            )
            raw = code_execution_json_bytes(fallback) + b"\n"
        writer.write(raw)
        await asyncio.wait_for(
            writer.drain(),
            timeout=self.limits.frame_timeout_seconds,
        )

    async def _record_call_status(
        self,
        sequence: int,
        *,
        status: str,
        response_bytes: int = 0,
        started_at: float,
    ) -> None:
        async with self._state_lock:
            record = self._call_records[sequence]
            record["status"] = status
            record["response_bytes"] = response_bytes
            record["elapsed_ms"] = max(
                0,
                int((time.monotonic() - started_at) * 1000),
            )

    async def _acquire_dispatch(self, sequence: int, mode: str) -> None:
        """Admit one binding call in submission order under safe/exclusive rules."""

        async with self._schedule_condition:
            self._pending_dispatches[sequence] = mode
            self._schedule_condition.notify_all()
            try:
                while True:
                    head = min(self._pending_dispatches, default=None)
                    if head == sequence and not self._exclusive_dispatch_active:
                        if mode == "parallel" and len(self._active_dispatches) < self.limits.max_parallel_calls:
                            self._pending_dispatches.pop(sequence, None)
                            self._active_dispatches.add(sequence)
                            self._peak_active_dispatches = max(
                                self._peak_active_dispatches,
                                len(self._active_dispatches),
                            )
                            self._schedule_condition.notify_all()
                            return
                        if mode == "exclusive" and not self._active_dispatches:
                            self._pending_dispatches.pop(sequence, None)
                            self._active_dispatches.add(sequence)
                            self._peak_active_dispatches = max(
                                self._peak_active_dispatches,
                                len(self._active_dispatches),
                            )
                            self._exclusive_dispatch_active = True
                            self._schedule_condition.notify_all()
                            return
                    await self._schedule_condition.wait()
            except BaseException:
                self._pending_dispatches.pop(sequence, None)
                self._schedule_condition.notify_all()
                raise

    async def _release_dispatch(self, sequence: int, mode: str) -> None:
        async with self._schedule_condition:
            self._active_dispatches.discard(sequence)
            if mode == "exclusive":
                self._exclusive_dispatch_active = False
            self._schedule_condition.notify_all()

    async def _handle_call(
        self,
        frame: Mapping[str, Any],
        *,
        request_id: str,
        request_bytes: int,
    ) -> dict[str, Any]:
        expected_keys = {
            "arguments",
            "binding_key",
            "protocol",
            "request_id",
            "token",
            "type",
        }
        if set(frame) != expected_keys:
            raise _ProtocolFailure(
                "Binding call frame shape is invalid.",
                request_id=request_id,
            )
        binding_key = frame.get("binding_key")
        if not isinstance(binding_key, str) or not binding_key or len(binding_key.encode("utf-8")) > 256:
            raise _ProtocolFailure(
                "Binding call key is invalid.",
                request_id=request_id,
            )
        arguments = frame.get("arguments")
        if not isinstance(arguments, dict):
            raise _ProtocolFailure(
                "Binding call arguments must be a JSON object.",
                binding_key=binding_key,
                request_id=request_id,
            )
        try:
            arguments = normalize_code_execution_json_value(arguments)
        except TypeError as error:
            raise _ProtocolFailure(
                "Binding call arguments are not lossless JSON.",
                binding_key=binding_key,
                request_id=request_id,
            ) from error
        argument_bytes = len(code_execution_json_bytes(arguments))
        if request_bytes > self.limits.max_frame_bytes or argument_bytes > self.limits.max_request_bytes:
            raise _ProtocolFailure(
                "Binding call arguments exceed the request limit.",
                binding_key=binding_key,
                request_id=request_id,
            )
        started_at = time.monotonic()
        async with self._state_lock:
            if self._call_count >= self.limits.max_calls:
                raise _ProtocolFailure(
                    "Binding call limit exhausted.",
                    binding_key=binding_key,
                    request_id=request_id,
                )
            if self._request_bytes + argument_bytes > self.limits.max_total_bytes:
                raise _ProtocolFailure(
                    "Aggregate binding byte limit exhausted.",
                    binding_key=binding_key,
                    request_id=request_id,
                )
            self._call_count += 1
            sequence = self._call_count
            self._request_bytes += argument_bytes
            self._call_records[sequence] = {
                "sequence": sequence,
                "binding_key": binding_key,
                "status": "error",
                "request_bytes": argument_bytes,
                "response_bytes": 0,
                "elapsed_ms": 0,
            }
        binding = self.bindings.get(binding_key)
        if binding is None:
            await self._record_call_status(
                sequence,
                status="rejected",
                started_at=started_at,
            )
            raise _ProtocolFailure(
                "Binding key was not offered for this program run.",
                binding_key=binding_key,
                request_id=request_id,
            )
        concurrency_mode = binding.concurrency_mode
        self._call_records[sequence]["concurrency_mode"] = concurrency_mode
        try:
            validate_code_execution_json_schema(
                arguments,
                binding.input_schema,
                field_name="binding arguments",
            )
        except (TypeError, ValueError) as error:
            await self._record_call_status(
                sequence,
                status="rejected",
                started_at=started_at,
            )
            raise _ProtocolFailure(
                "Binding arguments do not satisfy the declared input schema.",
                binding_key=binding_key,
                request_id=request_id,
            ) from error
        current_task = asyncio.current_task()
        if current_task is not None:
            self._call_tasks.add(current_task)
        dispatch_acquired = False
        try:
            await self._acquire_dispatch(sequence, concurrency_mode)
            dispatch_acquired = True
            try:
                value = await asyncio.wait_for(
                    binding.async_handler(arguments),
                    timeout=self.limits.call_timeout_seconds,
                )
                value = normalize_code_execution_json_value(value)
                validate_code_execution_json_schema(
                    value,
                    binding.output_schema,
                    field_name="binding result",
                )
                value_bytes = len(code_execution_json_bytes(value))
                if value_bytes > self.limits.max_response_bytes:
                    raise _ProtocolFailure(
                        "Binding result exceeds the response limit.",
                        status="error",
                        binding_key=binding_key,
                        request_id=request_id,
                    )
                async with self._state_lock:
                    if self._request_bytes + self._response_bytes + value_bytes > self.limits.max_total_bytes:
                        raise _ProtocolFailure(
                            "Aggregate binding byte limit exhausted.",
                            status="error",
                            binding_key=binding_key,
                            request_id=request_id,
                        )
                    self._response_bytes += value_bytes
            except asyncio.TimeoutError as error:
                await self._record_call_status(
                    sequence,
                    status="timed_out",
                    started_at=started_at,
                )
                raise _ProtocolFailure(
                    "Host binding timed out.",
                    status="timed_out",
                    binding_key=binding_key,
                    request_id=request_id,
                ) from error
            except CodeExecutionBindingError as error:
                await self._record_call_status(
                    sequence,
                    status=error.status,
                    started_at=started_at,
                )
                raise _ProtocolFailure(
                    error.public_message,
                    status=error.status,
                    binding_key=binding_key,
                    request_id=request_id,
                ) from error
            except _ProtocolFailure:
                await self._record_call_status(
                    sequence,
                    status="error",
                    started_at=started_at,
                )
                raise
            except (TypeError, ValueError) as error:
                await self._record_call_status(
                    sequence,
                    status="error",
                    started_at=started_at,
                )
                raise _ProtocolFailure(
                    "Host binding result violates its declared JSON contract.",
                    status="error",
                    binding_key=binding_key,
                    request_id=request_id,
                ) from error
            except Exception as error:
                await self._record_call_status(
                    sequence,
                    status="error",
                    started_at=started_at,
                )
                raise _ProtocolFailure(
                    "Host binding failed.",
                    status="error",
                    binding_key=binding_key,
                    request_id=request_id,
                ) from error
            await self._record_call_status(
                sequence,
                status="success",
                response_bytes=value_bytes,
                started_at=started_at,
            )
            return {
                "protocol": CODE_EXECUTION_BINDING_PROTOCOL,
                "request_id": request_id,
                "ok": True,
                "value": value,
            }
        except asyncio.CancelledError:
            await asyncio.shield(
                self._record_call_status(
                    sequence,
                    status="cancelled",
                    started_at=started_at,
                )
            )
            raise
        finally:
            if dispatch_acquired:
                await asyncio.shield(self._release_dispatch(sequence, concurrency_mode))
            if current_task is not None:
                self._call_tasks.discard(current_task)

    async def _handle_complete(
        self,
        frame: Mapping[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        required = {
            "logs",
            "logs_truncated",
            "ok",
            "protocol",
            "request_id",
            "token",
            "type",
        }
        if not required.issubset(frame) or set(frame) - (required | {"error", "value"}):
            raise _ProtocolFailure(
                "Program completion frame shape is invalid.",
                request_id=request_id,
            )
        ok = frame.get("ok")
        logs = frame.get("logs")
        logs_truncated = frame.get("logs_truncated")
        if not isinstance(ok, bool) or not isinstance(logs, list) or not isinstance(logs_truncated, bool):
            raise _ProtocolFailure(
                "Program completion fields are invalid.",
                request_id=request_id,
            )
        if any(not isinstance(item, str) for item in logs):
            raise _ProtocolFailure(
                "Program logs must be strings.",
                request_id=request_id,
            )
        log_bytes = sum(len(item.encode("utf-8")) for item in logs)
        if len(logs) > self.limits.max_log_lines or log_bytes > self.limits.max_log_bytes:
            raise _ProtocolFailure(
                "Program logs exceed their declared limits.",
                request_id=request_id,
            )
        completion: dict[str, Any]
        if ok:
            if "value" not in frame or "error" in frame:
                raise _ProtocolFailure(
                    "Successful program completion requires only a value.",
                    request_id=request_id,
                )
            try:
                value = normalize_code_execution_json_value(frame["value"])
            except TypeError as error:
                raise _ProtocolFailure(
                    "Program return value is not lossless JSON.",
                    request_id=request_id,
                ) from error
            if len(code_execution_json_bytes(value)) > self.limits.max_response_bytes:
                raise _ProtocolFailure(
                    "Program return value exceeds the response limit.",
                    request_id=request_id,
                )
            completion = {
                "ok": True,
                "value": value,
                "logs": list(logs),
                "logs_truncated": logs_truncated,
            }
        else:
            error = frame.get("error")
            if "value" in frame or not isinstance(error, Mapping):
                raise _ProtocolFailure(
                    "Failed program completion requires only an error.",
                    request_id=request_id,
                )
            status = str(error.get("status") or "error")
            if status not in {
                "error",
                "blocked",
                "approval_required",
                "timed_out",
                "cancelled",
            }:
                raise _ProtocolFailure(
                    "Program failure status is invalid.",
                    request_id=request_id,
                )
            completion = {
                "ok": False,
                "error": {
                    "status": status,
                    "message": str(error.get("message") or "Program execution failed.")[:1000],
                },
                "logs": list(logs),
                "logs_truncated": logs_truncated,
            }
        async with self._state_lock:
            if self._program_completion is not None:
                raise _ProtocolFailure(
                    "Program completion was already recorded.",
                    request_id=request_id,
                )
            self._accepting = False
        active_calls = tuple(task for task in self._call_tasks if task is not asyncio.current_task())
        if active_calls:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_calls),
                    timeout=self.limits.drain_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise _ProtocolFailure(
                    "Program completion could not drain admitted bindings.",
                    request_id=request_id,
                ) from error
        self._program_completion = completion
        return {
            "protocol": CODE_EXECUTION_BINDING_PROTOCOL,
            "request_id": request_id,
            "ok": True,
        }

    async def _process_raw_frame(
        self,
        raw: bytes,
    ) -> Mapping[str, Any]:
        try:
            await self._count_frame()
            if len(raw) > self.limits.max_frame_bytes:
                raise _ProtocolFailure("Binding protocol frame exceeds the byte limit.")
            frame = self._parse_frame(raw)
            request_id, frame_type = await self._validate_common(frame)
            if frame_type == "call":
                return await self._handle_call(
                    frame,
                    request_id=request_id,
                    request_bytes=len(raw),
                )
            return await self._handle_complete(
                frame,
                request_id=request_id,
            )
        except _ProtocolFailure as error:
            await self._count_rejection()
            return self._error_response(error)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            await self._count_rejection()
            return self._error_response(
                _ProtocolFailure(
                    "Binding protocol frame is malformed or oversized.",
                )
            )

    async def async_process_stdio_frame(self, raw: bytes) -> bytes:
        if self.transport != "stdio_framed" or not self._started or self._closed:
            raise RuntimeError("stdio binding bridge is not active")
        response = await self._process_raw_frame(raw)
        if self._fatal_protocol_error:
            self.record_transport_error(self._fatal_protocol_error)
            raise RuntimeError(self._fatal_protocol_error)
        encoded = code_execution_json_bytes(response) + b"\n"
        if len(encoded) > self.limits.max_frame_bytes:
            encoded = (
                code_execution_json_bytes(
                    self._error_response(
                        _ProtocolFailure(
                            "Binding protocol response exceeds the frame limit.",
                            request_id=str(response.get("request_id", "")),
                        )
                    )
                )
                + b"\n"
            )
        return encoded

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._client_tasks.add(current_task)
        response: Mapping[str, Any] | None = None
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readline(),
                    timeout=self.limits.frame_timeout_seconds,
                )
                response = await self._process_raw_frame(raw)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
                await self._count_rejection()
                response = self._error_response(_ProtocolFailure("Binding protocol frame is malformed or oversized."))
            if response is not None:
                await self._write_response(writer, response)
        except (asyncio.TimeoutError, ConnectionError, BrokenPipeError, OSError):
            await self._count_rejection()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            if current_task is not None:
                self._client_tasks.discard(current_task)
                self._call_tasks.discard(current_task)

    async def async_close(self, *, cancel_active: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        if self._server is not None:
            self._server.close()
        call_tasks = tuple(self._call_tasks)
        if cancel_active:
            for task in call_tasks:
                task.cancel()
        if call_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*call_tasks, return_exceptions=True),
                    timeout=self.limits.drain_timeout_seconds,
                )
            except asyncio.TimeoutError:
                for task in call_tasks:
                    task.cancel()
                await asyncio.gather(*call_tasks, return_exceptions=True)
        other_tasks = tuple(task for task in self._client_tasks if not task.done())
        for task in other_tasks:
            task.cancel()
        if other_tasks:
            await asyncio.gather(*other_tasks, return_exceptions=True)
        if self._server is not None:
            # Server shutdown waits for connections; settle their owned tasks first.
            await self._server.wait_closed()
        try:
            self._bound_socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        if self._socket_parent_alias is not None:
            try:
                self._socket_parent_alias.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "CODE_EXECUTION_BINDING_CLIENT_MODULE",
    "CODE_EXECUTION_BINDING_PROTOCOL",
    "CODE_EXECUTION_BINDING_STDIO_MAGIC",
    "CodeExecutionBindingBridge",
    "host_async_bindings_supported",
    "python_binding_client_source",
    "reserved_binding_environment_keys",
]
