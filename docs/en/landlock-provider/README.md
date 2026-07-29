# Landlock ExecutionResourceProvider

> Branch: `feature/landlock-provider`
> Based on: Agently main (`1317da67`)
> Changes: 2 files, +683 lines

---

## Overview

This branch adds `LandlockExecutionResourceProvider`, using Linux kernel Landlock LSM to provide filesystem access control.

Landlock is a security module introduced in Linux 5.13+ that allows unprivileged processes to restrict their own filesystem access permissions. Unlike SELinux/AppArmor, Landlock can be used by applications to create sandbox rules autonomously.

---

## Architecture

### Conforms to 4.1.4.2 Contract

```
provider_id = "landlock"
supported_kinds = ("code_execution",)
```

Implements all required interfaces:
- `async_probe` — Detect Landlock ABI version
- `async_ensure` — Create execution resource
- `async_health_check` — Health check
- `async_release` — Release resource
- `async_execute_code` — Execute code (bundle/manifest/grant validation pattern)

### Conditional Loading

Only loaded on Linux systems:

```python
# __init__.py
if platform.system() == "Linux":
    from .LandlockExecutionResourceProvider import LandlockExecutionResourceProvider
```

---

## Configuration

### Via `code_execution.providers`

```python
settings.set("code_execution.providers", [
    {"provider_id": "landlock", "config": {
        "allowed_read_dirs": ["/usr/lib"],
        "allowed_write_dirs": ["/tmp/work"],
    }},
    {"provider_id": "docker", "config": {}},  # fallback
])
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `allowed_read_dirs` | list[str] | `[]` | Allowed read directories |
| `allowed_write_dirs` | list[str] | `[]` | Allowed write directories |
| `abi_version` | int | `0` (auto-detect) | Landlock ABI version |

---

## Isolation Capabilities

| Capability | Support |
|------------|---------|
| Filesystem read control | ✅ |
| Filesystem write control | ✅ |
| File execute control | ✅ |
| Process isolation | ❌ |
| Network isolation | ❌ |
| Syscall filtering | ❌ |

---

## Important Features

### Irreversibility

Landlock restrictions are **irreversible** once applied. This provider uses fork-based isolation:
- Parent process remains unaffected
- Child process applies Landlock restrictions
- Direct syscall invocation via ctypes

### ABI Version Support

| ABI Version | Linux Version | New Features |
|-------------|---------------|--------------|
| v1 | 5.13 | Basic file access |
| v2 | 5.19 | REFER (rename/link) |
| v3 | 6.2 | TRUNCATE |

---

## Comparison with Bubblewrap

| Feature | Landlock | Bubblewrap |
|---------|----------|------------|
| Isolation mechanism | LSM (kernel-level) | User namespace |
| Process isolation | ❌ | ✅ |
| Filesystem isolation | ✅ (fine-grained) | ✅ (mount-point level) |
| Network isolation | ❌ | ✅ |
| Performance overhead | Very low | Low |
| Kernel requirement | 5.13+ | 3.8+ |

---

## System Requirements

- Linux kernel 5.13+
- Kernel config `CONFIG_SECURITY_LANDLOCK=y`

---

## File List

| File | Description |
|------|-------------|
| `LandlockExecutionResourceProvider.py` | Complete provider implementation (678 lines) |
| `__init__.py` | Linux conditional import |
