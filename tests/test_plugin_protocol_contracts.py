from __future__ import annotations

import inspect
from pathlib import Path

from agently.builtins.plugins.ActionExecutor import (
    BashSandboxActionExecutor,
    BrowseActionExecutor,
    DockerActionExecutor,
    LocalFunctionActionExecutor,
    MCPActionExecutor,
    NodeJSActionExecutor,
    PythonSandboxActionExecutor,
    SearchActionExecutor,
    SQLiteActionExecutor,
)
from agently.builtins.plugins.ActionFlow import TriggerFlowActionFlow
from agently.builtins.plugins.ActionRuntime import AgentlyActionRuntime
from agently.builtins.plugins.AgentOrchestrator import AgentlyAgentOrchestrator
from agently.builtins.plugins.ExecutionEnvironmentProvider import (
    BashExecutionEnvironmentProvider,
    BrowserExecutionEnvironmentProvider,
    DockerExecutionEnvironmentProvider,
    MCPExecutionEnvironmentProvider,
    NodeExecutionEnvironmentProvider,
    PythonExecutionEnvironmentProvider,
    SQLiteExecutionEnvironmentProvider,
)
from agently.builtins.agent_extensions.SkillsExtension._SkillsContext import AgentSkillsRuntimeContext
from agently.builtins.plugins.SkillsExecutor import AgentlySkillsExecutor
from agently.types.plugins import (
    ActionExecutor,
    ActionFlow,
    ActionRuntime,
    AgentOrchestrator,
    ExecutionEnvironmentProvider,
    SkillsExecutor,
    SkillsRuntimeContext,
)
from agently.utils.Settings import Settings


def _method_names(protocol: type) -> list[str]:
    return [
        name
        for name, value in protocol.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    ]


def test_builtin_skills_executor_matches_plugin_protocol():
    plugin = AgentlySkillsExecutor(settings=Settings(name="protocol-test"))

    assert isinstance(plugin, SkillsExecutor)
    for method_name in _method_names(SkillsExecutor):
        assert callable(getattr(plugin, method_name))


def test_builtin_skills_executor_has_no_stage_action_defaults():
    assert AgentlySkillsExecutor.DEFAULT_SETTINGS == {}


def test_agent_skills_context_matches_runtime_protocol():
    class FakeAgent:
        settings = Settings(name="fake-skills-agent")

        def input(self, *_args, **_kwargs):  # pragma: no cover - protocol shape only
            raise AssertionError("not used")

        class action:
            action_registry = None

            @staticmethod
            async def async_execute_action(*_args, **_kwargs):  # pragma: no cover - protocol shape only
                return {"status": "success"}

    context = AgentSkillsRuntimeContext(FakeAgent())

    assert isinstance(context, SkillsRuntimeContext)


def test_builtin_execution_environment_providers_match_protocol():
    providers = [
        BashExecutionEnvironmentProvider(),
        BrowserExecutionEnvironmentProvider(),
        DockerExecutionEnvironmentProvider(),
        MCPExecutionEnvironmentProvider(),
        NodeExecutionEnvironmentProvider(),
        PythonExecutionEnvironmentProvider(),
        SQLiteExecutionEnvironmentProvider(),
    ]

    for provider in providers:
        assert isinstance(provider, ExecutionEnvironmentProvider)
        assert provider.kind
        for method_name in _method_names(ExecutionEnvironmentProvider):
            assert callable(getattr(provider, method_name))


def test_builtin_action_executors_match_protocol():
    class FakeSearch:
        async def search(self, *_args, **_kwargs):  # pragma: no cover - protocol shape only
            return {}

    class FakeBrowse:
        async def browse(self, *_args, **_kwargs):  # pragma: no cover - protocol shape only
            return {}

    executors = [
        LocalFunctionActionExecutor(lambda: None),
        MCPActionExecutor(action_id="protocol_mcp", transport={"type": "direct", "tools": []}),
        PythonSandboxActionExecutor(),
        BashSandboxActionExecutor(timeout=1),
        SearchActionExecutor(search=FakeSearch(), method_name="search"),
        BrowseActionExecutor(browse=FakeBrowse()),
        NodeJSActionExecutor(timeout=1),
        DockerActionExecutor(timeout=1),
        SQLiteActionExecutor(),
    ]

    for executor in executors:
        assert isinstance(executor, ActionExecutor)
        assert executor.kind
        for method_name in _method_names(ActionExecutor):
            assert callable(getattr(executor, method_name))


def test_builtin_action_runtime_and_flow_match_protocols():
    settings = Settings(name="protocol-action-runtime")

    class FakeAction:
        pass

    runtime = AgentlyActionRuntime(action=FakeAction(), plugin_manager=None, settings=settings)
    flow = TriggerFlowActionFlow(plugin_manager=None, settings=settings)

    assert isinstance(runtime, ActionRuntime)
    assert isinstance(flow, ActionFlow)
    for method_name in _method_names(ActionRuntime):
        assert callable(getattr(runtime, method_name))
    for method_name in _method_names(ActionFlow):
        assert callable(getattr(flow, method_name))


def test_builtin_agent_orchestrator_matches_protocol():
    orchestrator = AgentlyAgentOrchestrator(plugin_manager=None, settings=Settings(name="protocol-orchestrator"))

    assert isinstance(orchestrator, AgentOrchestrator)
    for method_name in _method_names(AgentOrchestrator):
        assert callable(getattr(orchestrator, method_name))


def test_skills_executor_does_not_embed_business_case_mappings():
    plugin_root = Path(__file__).resolve().parents[1] / "agently" / "builtins" / "plugins" / "SkillsExecutor"
    source = "\n".join(path.read_text(encoding="utf-8").lower() for path in plugin_root.glob("*.py"))

    forbidden_terms = [
        "stock",
        "investment",
        "earnings",
        "travel",
        "itinerary",
        "rain-day",
        "lesson",
        "education",
        "retrieval_practice",
        "webapp",
        "playwright_trace",
        "wanderlog",
    ]
    assert [term for term in forbidden_terms if term in source] == []
