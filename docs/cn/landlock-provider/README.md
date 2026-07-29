# Landlock ExecutionResourceProvider

> 分支：`feature/landlock-provider`
> 基于：Agently main (`1317da67`)
> 改动范围：2 个文件，+683 行

---

## 概述

本分支新增 `LandlockExecutionResourceProvider`，使用 Linux 内核 Landlock LSM 提供文件系统访问控制。

Landlock 是 Linux 5.13+ 引入的安全模块，允许非特权进程限制自己的文件系统访问权限。与 SELinux/AppArmor 不同，Landlock 可以由应用程序自主创建沙箱规则。

---

## 架构设计

### 符合 4.1.4.2 契约

```
provider_id = "landlock"
supported_kinds = ("code_execution",)
```

实现了所有必需接口：
- `async_probe` — 检测 Landlock ABI 版本
- `async_ensure` — 创建执行资源
- `async_health_check` — 健康检查
- `async_release` — 释放资源
- `async_execute_code` — 执行代码（bundle/manifest/grant 验证模式）

### 条件加载

仅在 Linux 系统加载：

```python
# __init__.py
if platform.system() == "Linux":
    from .LandlockExecutionResourceProvider import LandlockExecutionResourceProvider
```

---

## 配置方式

### 通过 `code_execution.providers` 配置

```python
settings.set("code_execution.providers", [
    {"provider_id": "landlock", "config": {
        "allowed_read_dirs": ["/usr/lib"],
        "allowed_write_dirs": ["/tmp/work"],
    }},
    {"provider_id": "docker", "config": {}},  # fallback
])
```

### 配置选项

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allowed_read_dirs` | list[str] | `[]` | 允许读取的目录列表 |
| `allowed_write_dirs` | list[str] | `[]` | 允许写入的目录列表 |
| `abi_version` | int | `0` (自动检测) | Landlock ABI 版本 |

---

## 隔离能力

| 能力 | 支持 |
|------|------|
| 文件系统读控制 | ✅ |
| 文件系统写控制 | ✅ |
| 文件执行控制 | ✅ |
| 进程隔离 | ❌ |
| 网络隔离 | ❌ |
| 系统调用过滤 | ❌ |

---

## 重要特性

### 不可逆性

Landlock 的限制一旦应用就**不可撤销**。本 provider 使用 fork-based 隔离：
- 父进程不受影响
- 子进程应用 Landlock 限制
- 通过 ctypes 直接调用 syscall

### ABI 版本支持

| ABI 版本 | Linux 版本 | 新增特性 |
|----------|-----------|----------|
| v1 | 5.13 | 基础文件访问 |
| v2 | 5.19 | REFER (重命名/链接) |
| v3 | 6.2 | TRUNCATE |

---

## 与 Bubblewrap 对比

| 特性 | Landlock | Bubblewrap |
|------|----------|------------|
| 隔离机制 | LSM (内核级) | User namespace |
| 进程隔离 | ❌ | ✅ |
| 文件系统隔离 | ✅ (细粒度) | ✅ (挂载点级) |
| 网络隔离 | ❌ | ✅ |
| 性能开销 | 极低 | 低 |
| 内核要求 | 5.13+ | 3.8+ |

---

## 系统要求

- Linux 内核 5.13+
- 内核配置 `CONFIG_SECURITY_LANDLOCK=y`

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `LandlockExecutionResourceProvider.py` | 完整 provider 实现 (678 行) |
| `__init__.py` | Linux 条件导入 |
