# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Apply a host-generated Landlock rule manifest, then exec one argv."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14

LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_CREATE_RULESET_VERSION = 1

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
PR_SET_NO_NEW_PRIVS = 38


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _libc() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("c")
    if not name:
        return None
    library = ctypes.CDLL(name, use_errno=True)
    library.syscall.restype = ctypes.c_long
    return library


def probe_abi_version() -> int:
    if platform.system() != "Linux":
        return 0
    library = _libc()
    if library is None:
        return 0
    result = int(
        library.syscall(
            SYS_LANDLOCK_CREATE_RULESET,
            None,
            0,
            LANDLOCK_CREATE_RULESET_VERSION,
        )
    )
    return max(0, result)


def supported_access(abi_version: int) -> int:
    mask = (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_WRITE_FILE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_READ_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_FILE
        | LANDLOCK_ACCESS_FS_MAKE_CHAR
        | LANDLOCK_ACCESS_FS_MAKE_DIR
        | LANDLOCK_ACCESS_FS_MAKE_REG
        | LANDLOCK_ACCESS_FS_MAKE_SOCK
        | LANDLOCK_ACCESS_FS_MAKE_FIFO
        | LANDLOCK_ACCESS_FS_MAKE_BLOCK
        | LANDLOCK_ACCESS_FS_MAKE_SYM
    )
    if abi_version >= 2:
        mask |= LANDLOCK_ACCESS_FS_REFER
    if abi_version >= 3:
        mask |= LANDLOCK_ACCESS_FS_TRUNCATE
    return mask


def read_access(abi_version: int) -> int:
    return (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_READ_DIR
    ) & supported_access(abi_version)


def write_access(abi_version: int) -> int:
    return supported_access(abi_version)


def _load_manifest(path: Path) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Landlock manifest version must be 1")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("Landlock manifest rules must be a non-empty list")
    rules: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise TypeError(f"rules[{index}] must be a mapping")
        raw_path = item.get("path")
        access = item.get("access")
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise ValueError(f"rules[{index}].path must be absolute")
        resolved = str(Path(raw_path).resolve())
        if resolved != raw_path or not Path(resolved).exists():
            raise ValueError(f"rules[{index}].path must be canonical and existing")
        if access not in {"read", "write"}:
            raise ValueError(f"rules[{index}].access must be read or write")
        if resolved in seen:
            raise ValueError(f"rules[{index}].path is duplicated")
        seen.add(resolved)
        rules.append((resolved, str(access)))
    return rules


def apply_manifest(path: Path) -> int:
    abi = probe_abi_version()
    if abi <= 0:
        raise RuntimeError("Landlock ABI is unavailable")
    library = _libc()
    if library is None:
        raise RuntimeError("libc is unavailable")

    handled = supported_access(abi)
    ruleset_attr = _RulesetAttr(handled_access_fs=handled)
    ruleset_fd = int(
        library.syscall(
            SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(ruleset_attr),
            ctypes.sizeof(ruleset_attr),
            0,
        )
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

    try:
        for rule_path, mode in _load_manifest(path):
            path_fd = os.open(rule_path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule_attr = _PathBeneathAttr(
                    allowed_access=write_access(abi) if mode == "write" else read_access(abi),
                    parent_fd=path_fd,
                )
                result = int(
                    library.syscall(
                        SYS_LANDLOCK_ADD_RULE,
                        ruleset_fd,
                        LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule_attr),
                        0,
                    )
                )
                if result < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {rule_path}")
            finally:
                os.close(path_fd)

        if int(library.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        if int(library.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)
    return abi


def _parse(argv: list[str]) -> tuple[Path, list[str]]:
    if len(argv) < 4 or argv[0] != "--manifest" or "--" not in argv:
        raise ValueError("usage: helper --manifest PATH -- COMMAND [ARG ...]")
    separator = argv.index("--")
    manifest = Path(argv[1])
    command = argv[separator + 1 :]
    if not command:
        raise ValueError("Landlock helper command is required")
    return manifest, command


def main(argv: list[str] | None = None) -> int:
    try:
        manifest, command = _parse(list(argv if argv is not None else sys.argv[1:]))
        apply_manifest(manifest)
        os.execvpe(command[0], command, os.environ)
    except BaseException as error:
        print(f"landlock-helper:{type(error).__name__}:{error}", file=sys.stderr)
        return 120
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

