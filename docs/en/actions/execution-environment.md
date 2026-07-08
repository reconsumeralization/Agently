---
title: Execution Resource
description: Managed execution resources for Actions and TriggerFlow.
keywords: Agently, ExecutionResource, Action, TriggerFlow, sandbox, MCP, runtime_resources
---

# Execution Resource

> Languages: **English** · [中文](../../cn/actions/execution-environment.md)

> Renamed in the 4.1.3.8 Workspace/ActionRuntime boundary refactor: the managed
> live-resource seam is now **ExecutionResource** (`ExecutionResourceManager`,
> `ExecutionResourceProvider`, `Agently.execution_resource`). The previous
> `ExecutionEnvironment*` names are removed. This page keeps its URL for link
> stability.

Execution Resource is the framework-level layer that prepares and releases
managed execution resources before an action or workflow step runs.

It owns lifecycle and policy for resources such as MCP transports, command
runners, sandboxes, browsers, SQLite connections, and external process runners.
Action and TriggerFlow can require those environments, but they do not own
environment lifecycle.

## Audience

Most application developers should not start here. Prefer built-in actions and
Agent Component helpers that describe intent, such as enabling Python, shell,
workspace, MCP, SQLite, vector-store, or coding-workspace capabilities.

Read this page when you are:

- writing a custom `ActionExecutor` that depends on a managed live resource
- writing an `ExecutionResourceProvider` plugin
- reviewing how Action or TriggerFlow receives managed resources
- designing a new built-in capability that needs sandbox, process, MCP, client,
  credential, or cleanup lifecycle

Do not expose `Agently.execution_resource` as the default app-development
mental model. It is the core lifecycle layer behind higher-level capabilities.

## Where it sits

```text
Agent Component / built-in Action / custom Action / TriggerFlow / Skills plan
        |
        v
ActionSpec.execution_resources or TriggerFlow execution requirements
        |
        v
ExecutionResourceManager
        |
        v
ExecutionResourceProvider
        |
        v
managed handle / live resource
```

V1 exposes the global manager as:

```python
from agently import Agently

Agently.execution_resource
```

Most application code does not call the manager directly. Built-in MCP, Bash,
Python, Node.js, Docker, Browser, and SQLite actions can declare their
requirements and the Action dispatcher ensures them before executor calls.

For the broader ownership model, see
[Architecture / Extension Boundaries](../architecture/extension-boundaries.md).

## Built-in behavior

The built-in providers are:

| Kind | Used by | Managed resource |
|---|---|---|
| `mcp` | `agent.use_mcp(...)` / MCP actions | MCP transport resource |
| `bash` | `sandbox="trusted_local"` shell actions | configured local command runner |
| `python` | `sandbox="trusted_local"` Python actions | configured in-process Python sandbox |
| `node` | `sandbox="trusted_local"` Node.js actions | configured local Node.js runner |
| `docker` | default `agent.enable_python(...)`, `agent.enable_shell(...)`, `agent.enable_nodejs(...)`, `agent.enable_code_runtime(...)`, and Docker executor actions | Docker CLI runner, image provisioning, and language runtime profiles |
| `browser` | Browse actions that opt into managed browser resources | managed browser/page/session wrapper |
| `sqlite` | `agent.enable_sqlite(...)` / SQLite executor actions | SQLite connection |

Search intentionally is not listed here. It is a stateless Action-native
capability package; proxy, timeout, backend, and region belong to the Search
package/executor configuration rather than ExecutionResource.

These providers are low-level environment implementations. User-facing
capabilities should normally be exposed as Actions, and scenario shortcuts
should be exposed through Agent Components or future `agent.enable_*` helpers.
The Python, shell, Node.js, and common-language code runtime helpers default to
Docker-backed runtime profiles and fail closed when Docker CLI or daemon
preflight fails. Strict profiles report missing images instead of pulling them
implicitly; developer and CI profiles may pull missing images and prepare
standard dependencies as host-selected provisioning work. Explicit
`sandbox="trusted_local"` keeps the legacy local provider path for trusted
compatibility.

Action execution flow:

```text
ActionCall
  -> resolve ActionSpec
  -> ensure ActionSpec.execution_resources
  -> inject execution_resource_resources into action_call
  -> ActionExecutor.execute(...)
  -> release action_call-scoped handles
```

Custom `ActionExecutor.execute(...)` signatures do not change. Managed handles
are passed through `action_call["execution_resource_handles"]` and live
resources through `action_call["execution_resource_resources"]`.

## TriggerFlow

TriggerFlow still uses `runtime_resources` as the compatibility surface for
live execution-local resources. ExecutionResource does not rename or replace
that API.

You can pass managed requirements at execution creation or start:

```python
execution = flow.create_execution(
    execution_resources=[
        {
            "kind": "python",
            "scope": "execution",
            "resource_key": "sandbox",
        }
    ],
)
```

The manager ensures the resource, injects it into the execution-local resources,
and releases it when the execution closes. Manual `runtime_resources={...}` are
still unmanaged and are not health-checked or auto-released by the manager.

## Direct manager API

This API is for framework, action, and plugin developers.

The manager supports:

```python
Agently.execution_resource.declare(requirement)
Agently.execution_resource.ensure(requirement_or_id)
await Agently.execution_resource.async_ensure(requirement_or_id)
Agently.execution_resource.release(handle_or_id)
Agently.execution_resource.release_scope("session", owner_id)
Agently.execution_resource.inspect(id)
Agently.execution_resource.list(scope="execution")
Agently.policy_approval.register_handler("my_handler", handler)
Agently.configure_policy_approval(handler="my_handler")
Agently.set_settings("access_control_policy.auto_allow", True)
```

Declaration is lazy. It validates and records a requirement but does not start
anything. `ensure(...)` starts or reuses a handle subject to policy and approval.
Approval is resolved through the framework-wide `Agently.policy_approval`
handler. The default `input_timeout_fail` handler prompts only in an interactive
CLI and denies after timeout or immediately in non-interactive services. Service
wrappers around TriggerFlow executions should register their own handler, for
example one that stores a pending approval and resumes with `continue_with(...)`.
Trusted hosts can set `access_control_policy.auto_allow=True` through settings
to approve policy gates automatically; this does not bypass provider, sandbox,
path, command, or network constraints encoded in the requirement policy.
Before reusing a ready handle, the manager calls
`provider.async_health_check(handle)`. Healthy handles are reused with
`ref_count + 1`; unhealthy handles emit `execution_resource.unhealthy`, are
released, and then a fresh handle is ensured. V2 intentionally does not add a
background scheduler, lease TTL, or automatic reconnect loop.

If you are building an application, first check whether a built-in action or
Agent Component already exposes the capability you need.

## Observation

The manager emits framework events in the `execution_resource.*` family:

- `execution_resource.declared`
- `execution_resource.approval_required`
- `execution_resource.ensuring`
- `execution_resource.ready`
- `execution_resource.unhealthy`
- `execution_resource.releasing`
- `execution_resource.released`
- `execution_resource.failed`

Payloads include stable ids and status metadata only. They must not include raw
credentials, environment variables, command secrets, or live resource objects.

## Examples

Runnable examples are available in
[`examples/execution_resource`](../../../examples/execution_resource/README.md).
Start with the trusted-local `agent.enable_python(..., sandbox="trusted_local")`
quickstart when no Docker service is available, then move to the Docker-backed
Ollama, DeepSeek, and common-language code runtime examples. The TriggerFlow
example is intended for workflow or framework developers who need managed
execution-local resources.

## See also

- [Action Runtime](action-runtime.md)
- [MCP](mcp.md)
- [TriggerFlow State and Resources](../triggerflow/state-and-resources.md)
