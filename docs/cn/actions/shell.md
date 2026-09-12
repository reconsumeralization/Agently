---
title: Shell
description: 配置 Bash、PowerShell、执行环境和审批。
---

# Shell

> 语言：[English](../../en/actions/shell.md) · **中文**

`agent.enable_shell()` 提供一个通用 `run_shell` Action。模型提交对应语言的完整
`command`，可选 `workdir`；解释器、环境、审批和授权路径由开发者配置，不是模型参数。
可以运行管道、重定向、多行脚本，也可以执行已授权 Skill 目录中的脚本，不必逐脚本注册 Action。

```python
from agently import Agently

agent = Agently.create_agent().use_task_workspace("./workspace", mode="read_write")
agent.enable_shell(
    environment="offline",  # 默认：有隔离、无外网
    approval="all",         # 默认：每次都需要批准
    shell="bash",           # Windows 默认 powershell，其他平台默认 bash
    read_paths={"report": "./skills/report"},
)
```

目录必须真实存在。Docker 内工作根为 `/workspace`，上述只读资源为 `/skills/report`；
Windows Sandbox 对应 `C:\workspace`、`C:\skills\report`。本机模式使用真实宿主路径，
`read_paths` 在本机只是资源位置声明，不能强制只读。路径映射会进入 Action 描述。
Skill 内容本身不能授予权限。

## 环境与审批相互独立

| environment | 能力与边界 |
| --- | --- |
| `offline` | macOS/Linux 内置使用 Docker，授权目录挂载，禁止外网 |
| `online` | 同样的文件/进程隔离，允许外网；不因此挂载更多宿主文件 |
| `host` | 显式本机执行，没有框架文件或网络沙盒，工作目录只是起点 |

镜像默认 `python:3.12-slim`，遵循 `provisioning_profile` 和 `image_pull_policy`。
缺 Docker、镜像或解释器会报错，不自动降级到本机，也不会默默安装依赖。

| approval | 处理 |
| --- | --- |
| `all` | 每次交给统一 PolicyApproval，不额外请求风险模型 |
| `write` | 风险判断含写入、删除、权限变化或未知时请求批准 |
| `delete` | 风险判断含删除、权限变化或未知时请求批准 |
| `none` | 不额外请求模型或交互审批，但仍遵守明确拒绝规则和硬策略 |

选择性审批默认使用隔离的模型请求。它可能漏判或过度询问，不是沙盒或安全证明。
`risk_handler` 可替换风险分析，返回 `ShellRisk` 的 `effects`、`uncertainties`、`reason`；
分析错误或格式无效转人工批准，不静默放行。`deny` 是开发者配置的精确文本包含规则，
命中任何规则即拒绝，未命中不等于安全。需要批准却没有 handler 时按既有审批协议返回，
不会等待控制台输入。统一审批配置见 [Action Runtime](action-runtime.md)。

硬策略不会被软判断解除：例如禁网策略与 `online`/`host` 冲突会拒绝；本机不能强制
只读或目录白名单；任意 Shell 无法可靠执行细分文件操作限制或 argv 前缀策略时也会拒绝。
审批后命令、配置或 Action 策略改变需要重新提交；不能自动重放可能已发生副作用的调用。

## 输出与生命周期

每次返回 `ShellResult`：`ok`、`returncode`、`stdout`、`stderr`、两个 `*_truncated`
标记和 `timed_out`。`max_output_bytes` 默认每通道 20,000 字节，超出部分排空但不保存；
截断输出不是完整日志。`timeout` 默认 20 秒，可由开发者配置，上层更严格限额仍生效。
Windows Sandbox 若在交付完成记录前超时，两个截断标记保守表示输出可能不完整，
不代表已经证实字节超限。
非零退出和超时不是成功；调用取消后先结清自有进程树再传播取消。
不提供交互式 stdin、PTY、持久会话或后台脱离运行。

## Windows 与迁移

Windows 使用真正的 PowerShell；可通过 `binary=r"C:\PowerShell7\pwsh.exe"` 选择
PowerShell 7，不会把 Wine 同名占位程序视为能力证明。当前 CrossOver 测试覆盖
Windows Python 3.14.7 / PowerShell 7.6.6 的中文、管道、退出、输出限额及 Job Object
超时/取消进程树清理。Job Object 不是文件/网络沙盒。
Windows 的 `offline`/`online` 使用系统已安装的 Windows Sandbox CLI（`wsb.exe`，
Windows 11 24H2+）。框架不自动启用系统功能或提权，缺能力直接报错，不回退本机。
解释器和依赖必须在沙盒内可用；本机的 PowerShell 7 安装路径不会自动映射进去。
沙盒默认使用其 `powershell.exe`；需要额外工具可显式只读映射后指定沙盒内 `binary`。
框架关闭剪贴板、音视频、打印机和 GPU 共享，按每次调用创建/停止独立沙盒，
停止后才读取固定、有界、不可信的结果文件。停止失败会报错并保留临时目录供排查。
该后端的配置、结果校验和故障清理经过协议测试，控制脚本经过 CrossOver 实跑；
CrossOver 不支持 Windows Sandbox，**原生虚拟机隔离仍未实测**。
原生 Windows / PowerShell 5.1 未完成实测，请使用者测试反馈；问题请提供系统、Python、
PowerShell 版本及最小复现并 [提交 issue](https://github.com/AgentEra/Agently/issues)。

旧调用显式传 `commands=` 或 `sandbox=` 时仍按 argv 执行，默认工具名 `run_bash`；
不要与新环境/审批参数混用。迁移通用脚本应移除旧参数并明确选择环境和审批，工具参数
由旧 argv 合同换为 `command`/`workdir`。旧 `Cmd` 是过渡适配，反向委托新进程核心，
4.2 清理；不会把历史管道字符串悄悄解释为 Shell 源码。

第三方通过 `ShellResource.async_run()` 与 `ExecutionResourceProvider` 插件接入；
必须报告实际环境能力。Agent、ActionRuntime、PolicyApproval 和资源提供方分别拥有
入口、调用、授权和生命周期，不另建 Skill 专属执行/审批流程。
