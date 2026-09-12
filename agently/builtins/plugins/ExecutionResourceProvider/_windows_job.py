"""Owned Windows process trees. This is lifecycle containment, not a sandbox.

The tiny bootstrap joins before starting user code. Assigning the already
running target would leave a race in which its children escape membership.
This file is also executed with Python -I -S and uses only the standard library.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any


class _Basic(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _Limits(ctypes.Structure):
    _fields_ = [
        ("basic", _Basic), ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
    ]


def _kernel() -> Any:
    if os.name != "nt":
        raise OSError("Windows Job Objects require Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _error() -> OSError:
    if os.name == "nt":
        return ctypes.WinError(ctypes.get_last_error())
    return OSError("Windows Job Objects require Windows")


class WindowsJob:
    def __init__(self) -> None:
        self.api = _kernel()
        self.name = "Agently-Shell-" + uuid.uuid4().hex
        self.handle = self.api.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise _error()
        limits = _Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = _error()
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise error
        try:
            self._directory = tempfile.TemporaryDirectory(prefix="agently-shell-start-")
        except BaseException:
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise
        self._error_path = Path(self._directory.name) / "error.json"

    def argv(self, target: list[str]) -> list[str]:
        return [sys.executable, "-I", "-S", str(Path(__file__).resolve()), self.name, str(self._error_path), *target]

    def startup_error(self) -> OSError | None:
        try:
            status = self._error_path.lstat()
            if not stat.S_ISREG(status.st_mode) or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError("Windows startup diagnostic must be a regular non-reparse file")
            with self._error_path.open("rb") as source:
                raw = source.read(8193)
        except FileNotFoundError:
            return None
        if len(raw) > 8192:
            raise RuntimeError("Windows process startup diagnostic exceeds its limit")
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("errno")) is not int or not isinstance(value.get("message"), str) or (
            value.get("filename") is not None and not isinstance(value["filename"], str)
        ) or (value.get("winerror") is not None and type(value["winerror"]) is not int):
            raise RuntimeError("Invalid Windows process startup diagnostic")
        return OSError(value["errno"], value["message"], value.get("filename"), value.get("winerror"))

    def terminate(self) -> None:
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise _error()

    def close(self) -> None:
        if self.handle:
            if not self.api.CloseHandle(self.handle):
                raise _error()
            self.handle = None
        self._directory.cleanup()


def _main() -> int:
    api = _kernel()
    handle = api.OpenJobObjectW(0x0001, False, sys.argv[1])  # JOB_OBJECT_ASSIGN_PROCESS
    if not handle:
        raise _error()
    try:
        if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
            raise _error()
    finally:
        api.CloseHandle(handle)
    return subprocess.call(sys.argv[3:])


if __name__ == "__main__":
    try:
        sys.exit(_main())
    except OSError as error:
        payload = {"errno": error.errno, "winerror": getattr(error, "winerror", None),
                   "message": str(error.strerror or error)[:1024], "filename": error.filename}
        Path(sys.argv[2]).write_text(json.dumps(payload), encoding="utf-8")
        sys.exit(126)
