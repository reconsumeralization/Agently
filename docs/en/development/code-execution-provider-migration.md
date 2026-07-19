---
title: Code Execution Provider Migration
description: Provider-neutral Workspace-backed code execution contract and migration checklist for external isolation providers.
keywords: Agently, code execution, ExecutionResourceProvider, TaskWorkspace, provider migration
---

# Code Execution Provider Migration

> Languages: **English** · [中文](../../cn/development/code-execution-provider-migration.md)

This guide is for provider contributors. Agently's base implementation owns the
provider-neutral execution contract; an isolation implementation remains owned,
reviewed, and tested in the contributor's pull request.

## Fixed ownership and call order

`code_execution` is the resource kind. Docker, a container runtime variant, a
host policy mechanism, a remote worker, and the explicit unsafe local runner are
provider implementations, not new resource kinds.

Every call follows this order:

```text
TaskWorkspace
  -> issue TaskWorkspaceAccessGrant
  -> select and bind ExecutionResourceProvider
  -> materialize immutable CodeExecutionBundle
  -> execute the adapter-owned argv plan
  -> collect declared outputs into TaskWorkspace
  -> release provider
  -> close grant
```

The provider receives the exact grant, bundle, and materialization manifest. It
must not accept an inline-source bypass or construct mounts/policies from
model-produced paths.

## Required provider surface

A preferred provider exposes:

- a stable `provider_id` and `supported_kinds = ("code_execution",)`;
- `async_probe(...)` with observed availability and capability facts;
- `async_ensure(...)`, `async_health_check(...)`, and `async_release(...)`;
- a resource implementing `async_execute_code(bundle, manifest, grant, timeout)`;
- Workspace-root translation derived only from `TaskWorkspaceAccessGrant`;
- argv-only execution, bounded output, declared-output readback, and cleanup.

`async_probe(...)` should report supported languages, observed toolchain
versions, Workspace access modes, isolation, safety class, network behavior,
and mechanism-specific facts. Provider selection is ordered and deterministic;
unavailable, version-ineligible, or capability-ineligible candidates are
recorded and skipped. A hard `isolation="required"` request can never select
`trusted_local`. The selected provider facts are attached to Action result
metadata; do not infer safety from provider names.

For `code_execution`, `capabilities["isolation"]` is a mapping of observed
boolean axes (`process_contained`, `host_filesystem_restricted`,
`privilege_escalation_blocked`, and `syscalls_restricted`) plus optional
mechanism facts. Legacy string labels are rejected. Providers must also bound
captured output, stop their process/container on timeout and coroutine
cancellation, and make `async_release(...)` failures visible; the manager
quarantines a failed handle instead of declaring it released.

Application configuration may provide strings or candidate descriptors:

```python
agent.settings.set(
    "code_execution.providers",
    [
        {"provider_id": "preferred-provider", "config": {"profile": "strict"}},
        "docker",
    ],
)

agent.enable_code_runtime(language="python")
```

Candidate configuration is merged only for the candidate being probed or
ensured. This lets two candidates use different mechanisms without leaking
provider-specific settings into the core Action contract.

Container-runtime variants can subclass `DockerExecutionResourceProvider` and
override only `create_resource(...)` to construct their own
`DockerExecutionResource` subclass. `ExecutionResourceManager` still owns
ordered selection and the mandatory re-probe before ensure; the inherited
Docker provider reuses Workspace-grant binding, image preparation, health
checks and cleanup. The variant still owns and must test its runtime-specific
probe facts and command construction; the factory is not permission to inject
model-produced Docker arguments.

## Refactor target for PR #325

The gVisor contribution remains owned by its contributor. Rebase or retarget the
PR after the base contract lands, then adapt it as a `code_execution` provider
or a composition of the Docker resource. The refactored PR should:

- probe the configured Docker binary, daemon, and requested runtime for real;
- report the active runtime in probe/handle facts;
- construct the runtime-specific resource through `create_resource(...)`
  instead of copying the Docker provider lifecycle;
- derive mounts from the Workspace grant;
- consume adapter-produced build/run steps and immutable source bytes;
- fail closed when the requested runtime is absent, without silently using the
  default Docker runtime;
- retain its concrete runtime implementation and real tests in PR #325.

The base branch intentionally contains no copied gVisor commands or provider
implementation.

## Refactor target for PR #327

The Seatbelt contribution also remains contributor-owned. Refactor it as a
`code_execution` provider with its own stable `provider_id`. The PR should:

- probe the actual platform and policy executable;
- generate policy from resolved grant roots, never from arbitrary extra rules;
- keep source read-only and build/output/log roots writable as granted;
- use async argv-only process execution with bounded stdout/stderr;
- validate realpath containment and remove temporary policy files on every
  success, failure, timeout, and cancellation path;
- retain its concrete profile implementation and real tests in PR #327.

The base branch intentionally contains no copied Seatbelt profile or provider
implementation.

## Pull-request acceptance checklist

- No alternate Workspace, sandbox manager, session lifecycle, or resource kind.
- No raw source path, raw command, or provider-specific mount in model-visible
  Action input.
- Probe facts are observed; synthetic fixtures are labeled as such.
- Toolchain facts use canonical tool ids (`python`, `node`, `go`, `c++`) and
  normalized observed versions so minimum/exact constraints can be enforced.
- The provider passes the generic external-provider contract tests plus its own
  real mechanism tests.
- Documentation names the safety class and fallback behavior explicitly.
