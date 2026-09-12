---
title: Shell
description: Configure Bash, PowerShell, execution environments and approval.
---

# Shell

> Language: **English** · [中文](../../cn/actions/shell.md)

`agent.enable_shell()` exposes one general `run_shell` Action. Models supply
complete `command` source and an optional `workdir`. The Host fixes the language,
interpreter, environment, approval and resource paths. Pipes, redirection and
multiline source use that interpreter's syntax. Authorized Skill scripts need no
per-script Action registration.

```python
from agently import Agently

agent = Agently.create_agent().use_task_workspace("./workspace", mode="read_write")
agent.enable_shell(
    environment="offline",
    approval="all",
    shell="bash",
    read_paths={"report": "./skills/report"},
)
```

Directories must exist. Docker paths are `/workspace` and `/skills/report`;
Windows Sandbox uses `C:\workspace` and `C:\skills\report`. Host mode exposes
actual paths; its `read_paths` declarations cannot enforce read-only access.
The Action description carries the mapping. Skill content cannot grant access.
Windows defaults to PowerShell; other platforms to Bash.

## Independent environment and approval policies

| environment | Boundary |
| --- | --- |
| `offline` | Default; Docker on macOS/Linux, authorized mounts, no external network |
| `online` | Same file/process isolation, with external network; no additional host mounts |
| `host` | Explicit native execution; no framework filesystem/network sandbox; cwd is only a starting point |

The default image is `python:3.12-slim`, subject to `provisioning_profile` and
`image_pull_policy`. Missing Docker, images or interpreters fail explicitly,
without native fallback or silent dependency installation.

| approval | Behavior |
| --- | --- |
| `all` | Default; every call uses PolicyApproval, without an extra risk request |
| `write` | Ask for write, delete, privilege effects or uncertainty |
| `delete` | Ask for delete, privilege effects or uncertainty |
| `none` | No additional risk request or interactive approval; explicit deny rules and hard policy still apply |

Selective approval uses an isolated model request. It can miss risks or ask too
often: it is neither isolation nor proof of safety. A Host `risk_handler` may
replace it, returning `ShellRisk` (`effects`, `uncertainties`, `reason`). Invalid
or unavailable analysis requires approval. `deny` contains exact substring rules;
a match blocks execution, while no match proves nothing. Missing approval handlers
use the existing noninteractive approval result, not a console prompt. See
[Action Runtime](action-runtime.md).

Hard restrictions remain effective: disabled networking conflicts with online/host;
host cannot enforce read-only access or path allowlists. Unsupported granular file
permissions or argv-prefix restrictions are rejected rather than approximated by
model judgment. Changed commands, configuration or Action policy require a fresh
call. Never automatically replay a call whose side effects may already have happened.

## Output and lifetime

`ShellResult` contains `ok`, `returncode`, `stdout`, `stderr`, both
`*_truncated` flags and `timed_out`. `max_output_bytes` defaults to 20,000 per
channel. Excess bytes are drained but not archived; a truncated preview is not a
complete log. The default configurable `timeout` is 20 seconds; tighter parent
limits remain effective. If Windows Sandbox times out before delivering its
completion record, both truncation flags conservatively mark potentially
incomplete output, not proven byte-limit overflow.
Nonzero exits and timeouts are not success. Cancellation
settles the owned process tree before propagating. Interactive stdin, PTY,
persistent sessions and detached background work are not provided.

## Windows and migration

Select an actual interpreter, for example `binary=r"C:\PowerShell7\pwsh.exe"`.
Wine's same-named placeholder is not PowerShell evidence. Current CrossOver tests
use Windows Python 3.14.7 / PowerShell 7.6.6 and cover Unicode, pipes, exit status,
bounded output and Job Object timeout/cancellation cleanup. Jobs are not file or
network sandboxes. Windows offline/online use the installed Windows Sandbox CLI
(`wsb.exe`, Windows 11 24H2+). Missing capabilities fail without host fallback;
the framework never enables Windows features or elevates the Host automatically.
Interpreters and dependencies must exist inside the sandbox. Host PowerShell 7
installations are not mounted automatically; explicitly map an authorized tool
directory and select its guest `binary` when needed. The default guest interpreter
is `powershell.exe`. Clipboard, audio/video, printer and GPU sharing are disabled.
Each call owns a sandbox, stops it before bounded untrusted result readback, and
retains its temporary directory if cleanup fails. Configuration, readback and
failure handling have protocol tests; the controller has real CrossOver tests.
**Native VM isolation remains unverified** because CrossOver lacks Windows Sandbox.
Native Windows and PowerShell 5.1
remain unverified. Windows users are encouraged to test and
[open an issue](https://github.com/AgentEra/Agently/issues) with OS, Python,
PowerShell versions and a minimal reproduction.

Explicit legacy `commands=` or `sandbox=` keeps argv semantics and the default
`run_bash` name. Do not mix legacy and general Shell parameters. Migrate by removing
legacy options, selecting environment/approval explicitly and supplying
`command`/`workdir`. Legacy `Cmd` delegates to the new process owner and is scheduled
for removal in 4.2; old pipe strings do not silently become executable source.

Provider plugins implement `ShellResource.async_run()` and report actual resource
capabilities. Agent, ActionRuntime, PolicyApproval and ExecutionResource retain
their separate entry, dispatch, authorization and lifecycle responsibilities.
