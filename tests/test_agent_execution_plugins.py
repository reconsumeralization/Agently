from typing import Any

import pytest

from agently import Agently
from agently.builtins.plugins.AgentExecution import AgentExecution
from agently.core import PluginManager
from agently.utils import Settings


def create_agent(tmp_path):
    settings = Settings(parent=Agently.settings)
    plugins = PluginManager(settings, parent=Agently.plugin_manager)
    return Agently.AgentType(plugins, parent_settings=settings).use_task_workspace(tmp_path)


def test_registered_plugin_is_the_returned_run_owner(tmp_path):
    class CustomExecution(AgentExecution):
        name = "custom_execution"

    agent = create_agent(tmp_path)
    agent.plugin_manager.register("AgentExecution", CustomExecution, activate=False)
    execution = agent.create_execution("custom_execution", limits={"max_model_requests": 2})

    assert type(execution) is CustomExecution
    assert execution.agent is agent
    assert execution.plugin_manager is agent.plugin_manager
    assert execution.settings is agent.settings
    assert execution.limits["max_model_requests"] == 2
    assert execution.input("task") is execution
    assert type(agent.create_execution()) is AgentExecution


def test_configured_plugin_and_explicit_name_precedence(tmp_path):
    class CustomExecution(AgentExecution):
        name = "configured_execution"

    agent = create_agent(tmp_path)
    agent.plugin_manager.register("AgentExecution", CustomExecution)
    assert type(agent.create_execution()) is CustomExecution
    assert type(agent.create_execution("auto")) is AgentExecution


def test_released_orchestrator_activation_is_a_creation_compatibility_only(tmp_path):
    class LegacyOrchestrator:
        name = "legacy_creation"
        DEFAULT_SETTINGS: dict[str, Any] = {}

        @staticmethod
        def _on_register() -> None:
            pass

        @staticmethod
        def _on_unregister() -> None:
            pass

        def __init__(self, *, plugin_manager, settings):
            self.plugin_manager = plugin_manager
            self.settings = settings

        def create_execution(self, agent, **kwargs):
            execution = AgentExecution(agent, **kwargs)
            execution.logs["legacy_creation"] = True
            return execution

    class CustomExecution(AgentExecution):
        name = "new_creation"

    agent = create_agent(tmp_path)
    agent.plugin_manager.register("AgentOrchestrator", LegacyOrchestrator)
    assert agent.create_execution().logs["legacy_creation"] is True
    assert "legacy_creation" not in agent.create_execution("auto").logs
    agent.plugin_manager.register("AgentExecution", CustomExecution)
    assert type(agent.create_execution()) is CustomExecution


@pytest.mark.parametrize("name", ["", " ", "unregistered"])
def test_invalid_plugin_name_fails_before_production(tmp_path, name):
    agent = create_agent(tmp_path)
    with pytest.raises(ValueError, match="AgentExecution"):
        agent.create_execution(name)


def test_agent_named_plugin_does_not_replace_fluent_method(tmp_path):
    class InputExecution(AgentExecution):
        name = "input"

    agent = create_agent(tmp_path)
    agent.plugin_manager.register("AgentExecution", InputExecution, activate=False)
    assert type(agent.input("ordinary")) is AgentExecution
    assert type(agent.create_execution("input")) is InputExecution


@pytest.mark.asyncio
@pytest.mark.parametrize("name,route", [
    ("request", "model_request"), ("long_task", "agent_task"),
    ("plan", "plan"), ("long_content", "long_content"),
])
@pytest.mark.parametrize("enabled", [False, True])
async def test_named_execution_is_not_replaced_by_goal_convenience(tmp_path, name, route, enabled):
    agent = create_agent(tmp_path)
    execution = agent.create_execution(name)
    identity = execution.id
    assert execution.goal("Complete the request", turn_on_long_task=enabled) is execution
    assert (await execution.select_route())[0] == route
    assert execution.id == identity
    assert execution.name == name
    assert execution.execution_context.model_request_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name,strategy", [
    ("request", "flat"), ("request", "taskboard"), ("long_task", "direct"),
    ("plan", "direct"), ("plan", "flat"), ("long_content", "taskboard"),
])
async def test_named_execution_rejects_incompatible_strategy_before_dispatch(tmp_path, name, strategy):
    execution = create_agent(tmp_path).create_execution(name).strategy(strategy)
    with pytest.raises(ValueError, match="does not support strategy"):
        await execution.async_run()
    assert execution.execution_context.model_request_count == 0
    assert execution.logs["action_logs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["request", "long_task", "plan", "long_content"])
async def test_named_execution_cannot_fallback_through_route_policy(tmp_path, name):
    execution = create_agent(tmp_path).create_execution(
        name, options={"route_policy": {"allowed_routes": ["unavailable"], "on_violation": "fallback"}},
    )
    result = await execution.async_run()
    assert result["status"] == "blocked"
    assert execution.route_info["selected_route"] == "route_policy_blocked"
    assert execution.execution_context.model_request_count == 0
