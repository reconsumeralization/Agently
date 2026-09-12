from __future__ import annotations

from pathlib import Path
import asyncio
from typing import Any

import pytest

from agently import Agently
from agently.types.data.shell import ShellRisk
from agently.builtins.plugins.ActionExecutor.ShellActionExecutor import ShellActionExecutor
from agently.builtins.plugins.ExecutionResourceProvider.ShellProvider import ShellProvider, _ShellResource


def agent_at(root: Path):
    agent = Agently.create_agent().use_task_workspace(root, mode="read_write")
    agent.set_settings("policy_approval.handler", "fail_closed")
    return agent


@pytest.mark.asyncio
async def test_general_shell_is_real_source_and_bounded(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none", max_output_bytes=8)
    result = await agent.action.async_execute_action("run_shell", {
        "command": "printf 'abcdefghijk' | tr a-z A-Z; printf 'err' >&2; exit 7",
    })
    assert result.get("ok") is False
    assert result.get("result") == {
        "ok": False, "returncode": 7, "stdout": "ABCDEFGH", "stderr": "err",
        "stdout_truncated": True, "stderr_truncated": False, "timed_out": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["all", "none"])
async def test_all_and_none_do_not_request_risk_model(tmp_path: Path, mode: Any) -> None:
    def forbidden(_facts):
        raise AssertionError("unnecessary risk request")

    agent = agent_at(tmp_path).enable_shell(environment="host", approval=mode, risk_handler=forbidden)
    result = await agent.action.async_execute_action("run_shell", {"command": "printf data > marker"})
    assert (tmp_path / "marker").exists() == (mode == "none")
    assert result.get("ok") is (mode == "none")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,effects,unknown,expected", [
    ("write", ["read"], [], True), ("write", ["write"], [], False),
    ("write", ["delete"], [], False), ("delete", ["write"], [], True),
    ("delete", ["delete"], [], False), ("delete", ["privilege"], [], False),
    ("write", ["read"], ["indirect script"], False),
])
async def test_selective_policy_consumes_handler_once(tmp_path: Path, mode: Any, effects, unknown, expected) -> None:
    calls = []

    def risk(facts) -> ShellRisk:
        calls.append(facts)
        return {"effects": effects, "uncertainties": unknown, "reason": "Controlled policy fixture, not semantic evidence"}

    agent = agent_at(tmp_path).enable_shell(environment="host", approval=mode, risk_handler=risk)
    result = await agent.action.async_execute_action("run_shell", {"command": "printf data > marker"})
    assert result.get("ok") is expected
    assert (tmp_path / "marker").exists() is expected
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, {}, {"effects": "read"}, RuntimeError("model unavailable")])
async def test_invalid_risk_never_silently_approves(tmp_path: Path, failure: Any) -> None:
    async def risk(_facts):
        if isinstance(failure, Exception):
            raise failure
        return failure

    agent = agent_at(tmp_path).enable_shell(environment="host", approval="write", risk_handler=risk)
    result = await agent.action.async_execute_action("run_shell", {"command": "touch marker"})
    assert result.get("ok") is False and not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_deny_precedes_all_grants_and_model(tmp_path: Path) -> None:
    def forbidden(_facts):
        raise AssertionError("denied source must not trigger a model")

    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none", deny=["touch marker"], risk_handler=forbidden)
    result = await agent.action.async_execute_action("run_shell", {"command": "touch marker"})
    assert result.get("status") == "blocked"
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_mutation_after_policy_check_rejected(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none")
    executor = agent.action.action_registry.get_executor("run_shell")
    assert isinstance(executor, ShellActionExecutor)
    call: Any = {"action_input": {"command": "printf original"}}
    await executor.needs_approval(call)
    call["action_input"]["command"] = "touch marker"
    with pytest.raises(PermissionError, match="changed"):
        await executor.execute(spec={"action_id": "run_shell"}, action_call=call, policy={}, settings=agent.settings)
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["policy", "registration"])
async def test_revoke_while_risk_is_pending(tmp_path: Path, change: str) -> None:
    started, proceed = asyncio.Event(), asyncio.Event()

    async def risk(_facts) -> ShellRisk:
        started.set()
        await proceed.wait()
        return {"effects": [], "uncertainties": [], "reason": "Protocol fixture"}

    agent = agent_at(tmp_path).enable_shell(environment="host", approval="write", risk_handler=risk)
    task = asyncio.create_task(agent.action.async_execute_action("run_shell", {"command": "touch marker"}))
    await asyncio.wait_for(started.wait(), 5)
    if change == "policy":
        agent.set_settings("action.policy.agent", {"read_only": True})
    else:
        agent.action.unregister_action("run_shell")
    proceed.set()
    result = await asyncio.wait_for(task, 5)
    assert result.get("ok") is False and not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_resource_required_no_executor_native_fallback(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none")
    executor = agent.action.action_registry.get_executor("run_shell")
    assert isinstance(executor, ShellActionExecutor)
    call: Any = {"action_input": {"command": "touch marker"}}
    await executor.needs_approval(call)
    with pytest.raises(RuntimeError, match="managed ShellResource"):
        await executor.execute(spec={"action_id": "run_shell"}, action_call=call, policy={}, settings=agent.settings)
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_no_secret_env_inherited_or_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL_TEST_SECRET", "must-not-inherit")
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none", env={"PUBLIC_VALUE": "explicit"})
    result = await agent.action.async_execute_action("run_shell", {"command": "printf '%s:%s' \"$SHELL_TEST_SECRET\" \"$PUBLIC_VALUE\""})
    assert result.get("result", {})["stdout"] == ":explicit"
    assert "explicit" not in str(agent.action.get_action_info(tags=[f"agent-{agent.name}"]))


@pytest.mark.asyncio
async def test_missing_docker_never_runs_host(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="offline", approval="none", docker_binary=str(tmp_path / "missing-docker"))
    result = await agent.action.async_execute_action("run_shell", {"command": "touch marker"})
    assert result.get("ok") is False and not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_resource_close_and_workdir_containment(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none")
    spec = agent.action.action_registry.get_spec("run_shell")
    assert spec is not None
    provider = ShellProvider()
    handle = await provider.async_ensure(requirement=spec.get("execution_resources", [])[0], policy={})
    resource = handle.get("resource")
    assert isinstance(resource, _ShellResource)
    with pytest.raises(ValueError):
        await resource.async_run("true", workdir="..")
    await provider.async_release(handle)
    with pytest.raises(RuntimeError, match="closed"):
        await resource.async_run("true")


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [
    {"network_mode": "disabled"}, {"read_only": True}, {"allow_delete": False},
    {"allow_create": False}, {"allow_update": False}, {"path_allowlist": []},
    {"task_workspace_roots": []}, {"allowed_cmd_prefixes": ["printf"]},
    {"path_denylist": ["/private"]},
])
async def test_host_shell_does_not_bypass_hard_policy(tmp_path: Path, policy: Any) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none")
    agent.set_settings("action.policy.agent", policy)
    result = await agent.action.async_execute_action("run_shell", {"command": "touch marker"})
    assert result.get("ok") is False
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_provider_limits_only_tighten(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none", max_output_bytes=8)
    spec = agent.action.action_registry.get_spec("run_shell")
    assert spec is not None
    requirement = spec.get("execution_resources", [])[0]
    config = ShellProvider._config(requirement, {"timeout_seconds": 500, "max_output_bytes": 500})
    assert config["timeout"] == 20 and config["max_output_bytes"] == 8
    config = ShellProvider._config(requirement, {"timeout_seconds": 1, "max_output_bytes": 2})
    assert config["timeout"] == 1 and config["max_output_bytes"] == 2


@pytest.mark.asyncio
async def test_provider_rejects_mount_outside_policy(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    agent = agent_at(root).enable_shell(environment="offline", approval="none", read_paths={"extra": tmp_path})
    spec = agent.action.action_registry.get_spec("run_shell")
    assert spec is not None
    with pytest.raises(PermissionError, match="outside"):
        ShellProvider._config(spec.get("execution_resources", [])[0], {"path_allowlist": [str(root)]})


@pytest.mark.asyncio
async def test_whole_skill_directory_needs_no_script_action(tmp_path: Path) -> None:
    scripts = tmp_path / "a skill"
    scripts.mkdir()
    (scripts / "run.sh").write_text("printf 'skill-result'", encoding="utf-8")
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none", read_paths={"guide": scripts})
    spec = agent.action.action_registry.get_spec("run_shell")
    assert spec is not None and str(scripts) in spec.get("desc", "")
    result = await agent.action.async_execute_action("run_shell", {"command": f"bash '{scripts}/run.sh'"})
    assert result.get("result", {})["stdout"] == "skill-result"


@pytest.mark.parametrize("kwargs", [
    {"environment": "bad"}, {"approval": "bad"}, {"shell": "bad"}, {"timeout": 0},
    {"max_output_bytes": 0}, {"deny": [""]}, {"commands": ["pwd"], "environment": "host"},
    {"sandbox": "trusted_local", "shell": "powershell"}, {"read_paths": {"../bad": "."}},
])
def test_invalid_config_does_not_register(tmp_path: Path, kwargs: Any) -> None:
    agent = agent_at(tmp_path)
    with pytest.raises(ValueError):
        agent.enable_shell(**kwargs)
    assert agent.action.action_registry.get_spec("run_shell") is None


@pytest.mark.asyncio
async def test_general_shell_timeout_is_not_success(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none", timeout=1)
    result = await agent.action.async_execute_action("run_shell", {"command": "printf ready; sleep 30"})
    assert result.get("ok") is False
    assert result.get("result", {})["timed_out"] and result.get("result", {})["stdout"] == "ready"


@pytest.mark.asyncio
async def test_shared_agent_concurrent_workdirs_are_private(tmp_path: Path) -> None:
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none")
    results = await asyncio.gather(*(agent.action.async_execute_action("run_shell", {
        "command": f"printf '{name}' > marker; cat marker", "workdir": name,
    }) for name in ("first", "second")))
    assert [result.get("result", {})["stdout"] for result in results] == ["first", "second"]
    for name in ("first", "second"):
        assert (tmp_path / name / "marker").read_text() == name
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_cancel_after_write_does_not_replay(tmp_path: Path) -> None:
    agent = agent_at(tmp_path).enable_shell(environment="host", approval="none")
    task = asyncio.create_task(agent.action.async_execute_action("run_shell", {
        "command": "printf x >> marker; sleep 30",
    }))

    async def await_marker():
        while not (tmp_path / "marker").exists():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(await_marker(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert (tmp_path / "marker").read_text() == "x"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
