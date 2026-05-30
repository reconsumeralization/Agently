import asyncio
import logging
from typing import Any, cast

import pytest
import yaml
from agently import Agently
from agently.compatibility import (
    get_current_release_manifest,
    get_devtools_compatibility_manifest,
    get_skills_compatibility_manifest,
)
from agently.types.data import StreamingData
from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.routing import HybridRoutePlanner


_RUNTIME_LOG_KEYS = (
    "debug",
    "runtime.show_model_logs",
    "runtime.show_action_logs",
    "runtime.show_tool_logs",
    "runtime.show_trigger_flow_logs",
    "runtime.show_runtime_logs",
    "runtime.httpx_log_level",
)


def _snapshot_runtime_log_settings():
    return {key: Agently.settings.get(key, None) for key in _RUNTIME_LOG_KEYS}


def _restore_runtime_log_settings(snapshot):
    for key, value in snapshot.items():
        Agently.settings.set(key, value)
    level_name = Agently.settings.get("runtime.httpx_log_level", "WARNING")
    level = getattr(logging, str(level_name).upper(), logging.WARNING)
    logging.getLogger("httpx").setLevel(level)
    logging.getLogger("httpcore").setLevel(level)


@pytest.mark.asyncio
async def test_settings():
    Agently.set_settings("test", "test")
    assert Agently.settings["test"] == "test"


def test_agently_set_api_key_and_alias_mapping():
    original_api_key = Agently.settings.get("agently.api_key", None)
    try:
        Agently.set_api_key("official-key")
        assert Agently.settings["agently.api_key"] == "official-key"

        Agently.set_settings("agently_api_key", "official-key-alias")
        assert Agently.settings["agently.api_key"] == "official-key-alias"
    finally:
        Agently.set_settings("agently.api_key", original_api_key)


def test_agent_activate_model_sets_default_model_key_for_requests():
    agent = Agently.create_agent("model-switcher")

    assert agent.activate_model("ollama-qwen2.5") is agent
    assert getattr(agent.request, "_model_key") == "ollama-qwen2.5"
    assert getattr(agent.create_request(), "_model_key") == "ollama-qwen2.5"
    assert getattr(agent.create_temp_request(), "_model_key") == "ollama-qwen2.5"

    assert getattr(agent.create_request(model_key="deepseek-v4"), "_model_key") == "deepseek-v4"

    agent.activate_model(None)
    assert getattr(agent.request, "_model_key") is None
    assert getattr(agent.create_request(), "_model_key") is None

    with pytest.raises(ValueError, match="non-empty model_key"):
        agent.activate_model("")


def test_action_executor_plugins_registered():
    plugin_list = Agently.plugin_manager.get_plugin_list("ActionExecutor")
    assert "LocalFunctionActionExecutor" in plugin_list
    assert "MCPActionExecutor" in plugin_list
    assert "PythonSandboxActionExecutor" in plugin_list
    assert "BashSandboxActionExecutor" in plugin_list


def test_action_runtime_and_flow_plugins_registered():
    runtime_plugins = Agently.plugin_manager.get_plugin_list("ActionRuntime")
    flow_plugins = Agently.plugin_manager.get_plugin_list("ActionFlow")
    plugin_map = Agently.plugin_manager.get_plugin_list()

    assert "AgentlyActionRuntime" in runtime_plugins
    assert "TriggerFlowActionFlow" in flow_plugins
    assert getattr(Agently.action_runtime, "name", "") == "AgentlyActionRuntime"
    assert getattr(Agently.action_flow, "name", "") == "TriggerFlowActionFlow"
    assert "ToolManager" not in plugin_map


def test_dynamic_task_plugin_registered():
    planner_plugins = Agently.plugin_manager.get_plugin_list("TaskDAGPlanner")
    task = Agently.create_dynamic_task(
        "demo",
        plan={
            "graph_id": "registered",
            "tasks": [{"id": "a", "kind": "local", "binding": "local_handler"}],
        },
        handlers={"local_handler": lambda context: context.task.id},
    )

    assert "AgentlyTaskDAGPlanner" in planner_plugins
    assert task.planner.name == "AgentlyTaskDAGPlanner"
    assert "local_handler" in task.resolver.keys()


def test_skills_executor_plugin_has_no_stage_action_resolution_defaults():
    assert Agently.settings.get("plugins.SkillsExecutor.AgentlySkillsExecutor.action_resolution") is None
    framework_default = Agently.settings.get("skills.action_resolution")
    assert isinstance(framework_default, dict)
    aliases = cast(list[Any], framework_default.get("bash_action_aliases", []))
    assert "bash" in aliases


def test_agent_can_create_dynamic_task():
    agent = Agently.create_agent("graph-agent")
    task = agent.create_dynamic_task(
        "demo",
        plan={
            "graph_id": "agent-task",
            "tasks": [{"id": "a", "kind": "local", "binding": "local_handler"}],
        },
        handlers={"local_handler": lambda context: context.task.id},
    )

    assert task.name == "graph-agent-DynamicTask"
    assert task.settings.parent is agent.settings


@pytest.mark.asyncio
async def test_hybrid_route_planner_uses_model_when_optional_candidates_are_ambiguous():
    class FakeRequest:
        def __init__(self):
            self.payload: dict[str, Any] = {}
            self.output_format = None

        def input(self, payload):
            self.payload = payload
            return self

        def output(self, _schema, *, format="auto"):
            self.output_format = format
            return self

        async def async_start(self, **_kwargs):
            assert len(self.payload["route_candidates"]) == 3
            assert self.output_format == "json"
            return {"selected_route": "skills", "reason": "installed Skill is more specific"}

    class FakeAction:
        def get_action_list(self, tags=None):
            return [{"name": "lookup_release"}]

    class FakePrompt:
        def get(self, _key, default=None):
            return "prepare release notes"

    class FakeAgent:
        name = "fake-route-agent"
        action = FakeAction()

        def __init__(self):
            self.request = type("Request", (), {"prompt": FakePrompt()})()
            self._dynamic_task_candidates = [{"mode": "auto", "name": "planner"}]

        def _collect_skill_selectors(self, *, skills, mode):
            return ["release-checklist"] if mode == "model_decision" else []

        def _collect_skills_pack_selectors(self, *, skills_packs, mode):
            return []

        def create_temp_request(self):
            return FakeRequest()

    route, meta = await HybridRoutePlanner(cast(Any, FakeAgent())).select_route()

    assert route == "skills"
    assert meta["mode"] == "model_decision"
    assert meta["selected_by"] == "model"


@pytest.mark.asyncio
async def test_hybrid_route_planner_keeps_required_routes_deterministic():
    class FakePrompt:
        def get(self, _key, default=None):
            return "prepare release notes"

    class FakeAgent:
        name = "fake-route-agent"
        action = None

        def __init__(self):
            self.request = type("Request", (), {"prompt": FakePrompt()})()
            self._dynamic_task_candidates = [{"mode": "auto", "name": "planner"}]

        def _collect_skill_selectors(self, *, skills, mode):
            return ["release-checklist"] if mode == "required" else []

        def _collect_skills_pack_selectors(self, *, skills_packs, mode):
            return []

    route, meta = await HybridRoutePlanner(cast(Any, FakeAgent())).select_route()

    assert route == "skills"
    assert meta["mode"] == "required"
    assert meta["selected_by"] == "deterministic"


@pytest.mark.asyncio
async def test_dynamic_task_runs_submitted_plan():
    async def run_task(context):
        if context.dependency_results:
            return f"{ context.task.id }:{ context.dependency_results['a'] }"
        return f"{ context.task.id }:{ context.graph_input['value'] }"

    graph = {
        "graph_id": "main-package-workflow",
        "tasks": [
            {"id": "a", "kind": "local", "binding": "local_handler"},
            {"id": "b", "kind": "local", "binding": "local_handler", "depends_on": ["a"]},
        ],
        "semantic_outputs": {"final": "b"},
    }
    task = Agently.create_dynamic_task(
        "run planned graph",
        plan=graph,
        handlers={"local_handler": run_task},
    )

    snapshot = await task.async_run(graph_input={"value": "ok"}, timeout=1)

    assert snapshot["task_results"] == {"a": "a:ok", "b": "b:a:ok"}
    assert snapshot["semantic_outputs"]["final"]["task_id"] == "b"


@pytest.mark.asyncio
async def test_dynamic_task_model_output_schema_uses_agently_request_pipeline():
    schema = {
        "brief": (str, "customer-facing briefing", True),
        "next_update": (str, "next update timing", True),
    }

    class FakeModelRequest:
        def __init__(self):
            self.output_schema = None
            self.output_format = None
            self.start_kwargs = None

        def input(self, value):
            return self

        def instruct(self, value):
            return self

        def output(self, value, *, format="auto"):
            self.output_schema = value
            self.output_format = format
            return self

        async def async_start(self, **kwargs):
            self.start_kwargs = kwargs
            return {"brief": "Latency is resolved.", "next_update": "After duplicate checks finish."}

    request = FakeModelRequest()
    task = Agently.create_dynamic_task(
        "brief an incident",
        plan={
            "graph_id": "model-output-contract",
            "task_schema_version": "task_dag/v1",
            "tasks": [{"id": "write_brief", "kind": "model"}],
            "semantic_outputs": {"frontstage": "write_brief"},
        },
        model=request,
        output_schema=schema,
        ensure_keys=["brief", "next_update"],
    )

    snapshot = await task.async_run(timeout=1)

    assert request.output_schema == schema
    assert request.output_format == "auto"
    assert request.start_kwargs == {"ensure_keys": ["brief", "next_update"]}
    assert snapshot["semantic_outputs"]["frontstage"]["result"]["brief"] == "Latency is resolved."


@pytest.mark.asyncio
async def test_dynamic_task_model_task_can_select_output_format():
    schema = {"html": (str, "render-ready HTML", True)}

    class FakeModelRequest:
        def __init__(self):
            self.output_schema = None
            self.output_format = None

        def input(self, _value):
            return self

        def instruct(self, _value):
            return self

        def output(self, value, *, format="auto"):
            self.output_schema = value
            self.output_format = format
            return self

        async def async_start(self, **_kwargs):
            return {"html": "<section>OK</section>"}

    request = FakeModelRequest()
    task = Agently.create_dynamic_task(
        "render a fragment",
        plan={
            "graph_id": "model-output-format",
            "task_schema_version": "task_dag/v1",
            "tasks": [
                {
                    "id": "render_html",
                    "kind": "model",
                    "inputs": {
                        "output_schema": schema,
                        "output_format": "flat_markdown",
                    },
                }
            ],
            "semantic_outputs": {"fragment": "render_html"},
        },
        model=request,
    )

    snapshot = await task.async_run(timeout=1)

    assert request.output_schema == schema
    assert request.output_format == "flat_markdown"
    assert snapshot["semantic_outputs"]["fragment"]["result"]["html"] == "<section>OK</section>"


def test_dynamic_task_can_be_created_without_explicit_model_source():
    task = Agently.create_dynamic_task("needs planning")

    assert "model" in task.resolver.keys()
    assert "action" not in task.resolver.keys()
    assert task.planner.available_bindings == ("model",)


def test_dynamic_task_exposes_actions_only_when_explicit():
    task = Agently.create_dynamic_task("needs action", actions=Agently.action)

    assert "action" in task.resolver.keys()
    assert task.planner.available_bindings == ("model", "action")


@pytest.mark.asyncio
async def test_agent_execution_runs_submitted_dynamic_task_and_streams_process():
    async def run_task(context):
        return {"task_id": context.task.id, "value": context.graph_input["value"]}

    agent = Agently.create_agent("execution-dag-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-execution-dag",
                "task_schema_version": "task_dag/v1",
                "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
                "semantic_outputs": {"final": "extract"},
            },
            handlers={"local_handler": run_task},
            graph_input={"value": "ok"},
        )
        .input("run submitted graph")
        .create_execution()
    )

    stream_items = []
    async for item in execution.get_async_generator(type="instant"):
        if item.is_complete:
            stream_items.append(item)

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["semantic_outputs"]["final"]["result"]["value"] == "ok"
    assert meta["route_plan"]["selected_route"] == "dynamic_task"
    assert any(item.path == "route.selected" and item.route == "dynamic_task" for item in stream_items)
    assert any(item.path == "route.dynamic_task.graph" for item in stream_items)
    assert any(item.path == "task_dag.tasks.extract.start" for item in stream_items)
    assert any(item.path == "task_dag.tasks.extract.complete" for item in stream_items)


def _agent_input_placeholder_graph() -> dict[str, Any]:
    return {
        "graph_id": "agent-input-placeholder",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {
                "id": "echo",
                "kind": "local",
                "binding": "echo_handler",
                "inputs": {"kwargs": {"ticket": "${INIT.ticket}"}},
            }
        ],
        "semantic_outputs": {"final": "echo"},
    }


async def _echo_dynamic_task_kwargs(context):
    return dict((context.task.inputs or {}).get("kwargs") or {})


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_uses_prompt_input_as_graph_input():
    agent = Agently.create_agent("execution-dag-prompt-input-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan=_agent_input_placeholder_graph(),
            handlers={"echo_handler": _echo_dynamic_task_kwargs},
        )
        .input({"ticket": "TICKET-OK"})
        .create_execution()
    )

    data = await execution.async_get_data()

    assert data["semantic_outputs"]["final"]["result"]["ticket"] == "TICKET-OK"


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_explicit_graph_input_wins():
    agent = Agently.create_agent("execution-dag-explicit-graph-input-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan=_agent_input_placeholder_graph(),
            handlers={"echo_handler": _echo_dynamic_task_kwargs},
            graph_input={"ticket": "GRAPH-INPUT"},
        )
        .input({"ticket": "PROMPT-INPUT"})
        .create_execution()
    )

    data = await execution.async_get_data()

    assert data["semantic_outputs"]["final"]["result"]["ticket"] == "GRAPH-INPUT"


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_uses_frozen_prompt_snapshot():
    agent = Agently.create_agent("execution-dag-prompt-snapshot-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan=_agent_input_placeholder_graph(),
            handlers={"echo_handler": _echo_dynamic_task_kwargs},
        )
        .input({"ticket": "SNAPSHOT-INPUT"})
        .create_execution()
    )
    agent.input({"ticket": "MUTATED-INPUT"})

    data = await execution.async_get_data()

    assert data["semantic_outputs"]["final"]["result"]["ticket"] == "SNAPSHOT-INPUT"


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_missing_input_path_names_graph_input_source():
    agent = Agently.create_agent("execution-dag-missing-prompt-input-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan=_agent_input_placeholder_graph(),
            handlers={"echo_handler": _echo_dynamic_task_kwargs},
        )
        .input({"account": "Acme"})
        .create_execution()
    )

    with pytest.raises(ValueError, match=r"\$\{INIT\.ticket\}.*execution prompt snapshot input slot"):
        await asyncio.wait_for(execution.async_get_data(), timeout=2)


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_failure_terminates_stream():
    async def boom_handler(_context):
        raise RuntimeError("intentional handler failure")

    agent = Agently.create_agent("execution-dag-failure-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-execution-dag-failure",
                "task_schema_version": "task_dag/v1",
                "tasks": [{"id": "explode", "kind": "local", "binding": "boom_handler"}],
                "semantic_outputs": {"final": "explode"},
            },
            handlers={"boom_handler": boom_handler},
        )
        .input("run failing graph")
        .create_execution()
    )

    stream_items = []

    async def consume_stream():
        async for item in execution.get_async_generator(type="instant"):
            stream_items.append(item)

    with pytest.raises(RuntimeError, match="intentional handler failure"):
        await asyncio.wait_for(consume_stream(), timeout=2)

    assert execution.status == "error"
    assert any(item.path == "error" for item in stream_items)
    assert any(item.path == "task_dag.tasks.explode.fail" for item in stream_items)


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_can_use_action_candidates():
    def classify_ticket(text: str):
        return {"priority": "high", "text": text}

    agent = Agently.create_agent("execution-action-dag-agent")
    agent.register_action(
        name="classify_ticket",
        desc="Classify support ticket.",
        kwargs={"text": (str, "ticket text")},
        func=classify_ticket,
    )
    execution = (
        agent
        .use_actions(["classify_ticket"])
        .use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-action-dag",
                "task_schema_version": "task_dag/v1",
                "tasks": [
                    {
                        "id": "classify",
                        "kind": "action",
                        "binding": "classify_ticket",
                        "inputs": {"kwargs": {"text": "payment failed"}},
                    }
                ],
                "semantic_outputs": {"final": "classify"},
            },
        )
        .input("run action graph")
        .create_execution()
    )

    data = await execution.async_get_data()

    assert data["semantic_outputs"]["final"]["result"]["priority"] == "high"


@pytest.mark.asyncio
async def test_agent_execution_dynamic_task_streams_model_field_deltas():
    schema = {
        "prethinking": (str, "operator-visible process note", True),
        "reply": (str, "customer-facing reply", True),
    }

    class FakeModelResponse:
        async def get_async_generator(self, type="instant", **_kwargs):
            assert type == "instant"
            yield StreamingData(path="prethinking", value="Check", delta="Check", event_type="delta")
            yield StreamingData(path="prethinking", value="Check billing", delta=" billing", event_type="delta")
            yield StreamingData(path="reply", value="We are", delta="We are", event_type="delta")
            yield StreamingData(path="reply", value="We are investigating.", delta=" investigating.", event_type="delta")
            yield StreamingData(path="prethinking", value="Check billing", event_type="done", is_complete=True)
            yield StreamingData(path="reply", value="We are investigating.", event_type="done", is_complete=True)

        async def async_get_data(self, **kwargs):
            assert kwargs == {"ensure_keys": ["prethinking", "reply"]}
            return {
                "prethinking": "Check billing",
                "reply": "We are investigating.",
            }

    class FakeModelRequest:
        def __init__(self):
            self.output_schema = None
            self.output_format = None

        def input(self, _value):
            return self

        def instruct(self, _value):
            return self

        def output(self, value, *, format="auto"):
            self.output_schema = value
            self.output_format = format
            return self

        def get_response(self, **_kwargs):
            return FakeModelResponse()

        async def async_start(self, **_kwargs):  # pragma: no cover - get_response path owns streaming
            raise AssertionError("streaming model tasks should use get_response()")

    request = FakeModelRequest()
    agent = Agently.create_agent("execution-model-field-stream-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-model-field-stream",
                "task_schema_version": "task_dag/v1",
                "tasks": [
                    {
                        "id": "draft",
                        "kind": "model",
                        "inputs": {
                            "output_schema": schema,
                            "ensure_keys": ["prethinking", "reply"],
                        },
                    }
                ],
                "semantic_outputs": {"final": "draft"},
            },
            model=request,
        )
        .input("draft a transparent support reply")
        .create_execution()
    )

    streamed = []
    async for item in execution.get_async_generator(type="instant"):
        if item.event_type == "delta":
            streamed.append((item.path, item.delta, item.is_complete))

    data = await execution.async_get_data()

    assert streamed[:4] == [
        ("task_dag.tasks.draft.fields.prethinking", "Check", False),
        ("task_dag.tasks.draft.fields.prethinking", " billing", False),
        ("task_dag.tasks.draft.fields.reply", "We are", False),
        ("task_dag.tasks.draft.fields.reply", " investigating.", False),
    ]
    assert request.output_format == "auto"
    assert data["semantic_outputs"]["final"]["result"]["reply"] == "We are investigating."


def test_deprecated_action_manager_aliases_warn():
    with pytest.warns(DeprecationWarning):
        assert Agently.action.tool_manager is not None
    with pytest.warns(DeprecationWarning):
        assert Agently.action.action_manager is not None


def test_tool_manager_plugin_registration_warns():
    from agently.builtins.plugins.ToolManager.AgentlyToolManager import AgentlyToolManager
    from agently.core import PluginManager
    from agently.utils import Settings

    settings = Settings(name="DeprecatedToolManagerSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="DeprecatedToolManagerPluginManager")

    with pytest.warns(DeprecationWarning):
        plugin_manager.register("ToolManager", AgentlyToolManager)


def test_action_plugin_protocols_exported_for_third_party_plugins():
    from agently.types.plugins import (
        ActionExecutionHandler,
        ActionExecutor,
        ActionFlow,
        ActionPlanningHandler,
        ActionRuntime,
        StandardActionExecutionHandler,
        StandardActionPlanningHandler,
    )

    assert ActionExecutor is not None
    assert ActionRuntime is not None
    assert ActionFlow is not None
    assert ActionPlanningHandler is not None
    assert ActionExecutionHandler is not None
    assert StandardActionPlanningHandler is not None
    assert StandardActionExecutionHandler is not None


def test_agently_load_settings_file(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"

    config_path.write_text(
        yaml.safe_dump(
            {
                "test_main_package": {
                    "base_url": "${ENV.TEST_MAIN_PACKAGE_BASE_URL}",
                }
            }
        ),
        encoding="utf-8",
    )
    env_path.write_text("TEST_MAIN_PACKAGE_BASE_URL=https://example.com\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_MAIN_PACKAGE_BASE_URL", raising=False)

    Agently.load_settings("yaml_file", str(config_path), auto_load_env=True)

    assert Agently.settings["test_main_package.base_url"] == "https://example.com"


def test_agently_load_settings_file_applies_model_requester_alias(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"
    previous_short = Agently.settings.get("OpenAICompatible", None)
    previous_openai = Agently.settings.get("plugins.ModelRequester.OpenAICompatible", None)

    config_path.write_text(
        yaml.safe_dump(
            {
                "OpenAICompatible": {
                    "base_url": "${ENV.OPENAI_BASE_URL}",
                    "api_key": "${ENV.OPENAI_API_KEY}",
                    "model": "${ENV.OPENAI_MODEL}",
                }
            }
        ),
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_BASE_URL=https://example.com/v1",
                "OPENAI_API_KEY=sk-test",
                "OPENAI_MODEL=deepseek-chat",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    try:
        Agently.load_settings("yaml_file", str(config_path), auto_load_env=True)

        assert Agently.settings["OpenAICompatible.model"] == "deepseek-chat"
        assert Agently.settings["plugins.ModelRequester.OpenAICompatible.base_url"] == "https://example.com/v1"
        assert Agently.settings["plugins.ModelRequester.OpenAICompatible.api_key"] == "sk-test"
        assert Agently.settings["plugins.ModelRequester.OpenAICompatible.model"] == "deepseek-chat"
    finally:
        Agently.settings.set("OpenAICompatible", previous_short)
        Agently.settings.set("plugins.ModelRequester.OpenAICompatible", previous_openai)


def test_agently_load_settings_refresh_httpx_log_level(tmp_path):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "httpx_log_level": "INFO",
                }
            }
        ),
        encoding="utf-8",
    )

    Agently.load_settings("yaml_file", str(config_path))

    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("httpcore").level == logging.INFO


def test_agently_set_debug_mapping_profiles():
    snapshot = _snapshot_runtime_log_settings()
    try:
        Agently.set_settings("debug", True)
        assert Agently.settings["debug"] == "simple"
        assert Agently.settings["runtime.show_model_logs"] == "simple"
        assert Agently.settings["runtime.show_action_logs"] == "simple"
        assert Agently.settings["runtime.show_tool_logs"] == "simple"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "simple"
        assert Agently.settings["runtime.show_runtime_logs"] == "simple"
        assert logging.getLogger("httpx").level == logging.WARNING

        Agently.set_settings("debug", "detail")
        assert Agently.settings["debug"] == "detail"
        assert Agently.settings["runtime.show_model_logs"] == "detail"
        assert Agently.settings["runtime.show_action_logs"] == "detail"
        assert Agently.settings["runtime.show_runtime_logs"] == "detail"
        assert logging.getLogger("httpx").level == logging.INFO

        Agently.set_settings("debug", False)
        assert Agently.settings["debug"] == "off"
        assert Agently.settings["runtime.show_model_logs"] == "off"
        assert Agently.settings["runtime.show_action_logs"] == "off"
        assert Agently.settings["runtime.show_tool_logs"] == "off"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "off"
        assert Agently.settings["runtime.show_runtime_logs"] == "off"
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        _restore_runtime_log_settings(snapshot)


def test_agently_load_settings_applies_debug_mapping(tmp_path):
    snapshot = _snapshot_runtime_log_settings()
    try:
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.safe_dump({"debug": "detail"}), encoding="utf-8")

        Agently.load_settings("yaml_file", str(config_path))

        assert Agently.settings["debug"] == "detail"
        assert Agently.settings["runtime.show_model_logs"] == "detail"
        assert Agently.settings["runtime.show_action_logs"] == "detail"
        assert Agently.settings["runtime.show_tool_logs"] == "detail"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "detail"
        assert Agently.settings["runtime.show_runtime_logs"] == "detail"
        assert logging.getLogger("httpx").level == logging.INFO
    finally:
        _restore_runtime_log_settings(snapshot)


def test_request_quick_prompt_supports_key_value_and_kwargs():
    request = Agently.create_request()

    request.info("context", "Public-facing API handler", framework="FastAPI")

    assert request.prompt.to_prompt_object().info == {
        "context": "Public-facing API handler",
        "framework": "FastAPI",
    }


def test_request_quick_prompt_preserves_explicit_mappings():
    request = Agently.create_request()

    request.instruct("Hello ${name}", mappings={"name": "Alice"})

    assert request.prompt.to_prompt_object().instruct == "Hello Alice"


def test_devtools_compatibility_manifest_declares_runtime_protocol():
    manifest = get_devtools_compatibility_manifest()

    assert manifest["companion_package"] == "agently-devtools"
    assert manifest["runtime_protocol"].startswith("agently-devtools.observation-runtime.v")
    assert manifest["recommended_version_specifier"]
    assert manifest["framework_version"] == get_current_release_manifest()["framework_version"]


def test_skills_compatibility_manifest_declares_authoring_protocols():
    manifest = get_skills_compatibility_manifest()

    assert manifest["repository"] == "Agently-Skills"
    assert manifest["authoring_protocol"].startswith("agently-skills.authoring.v")
    assert manifest["devtools_guidance_protocol"].startswith(
        "agently-skills.devtools-guidance.v"
    )


def test_agent_quick_prompt_supports_key_value_and_kwargs():
    agent = Agently.create_agent()

    agent.info("context", "Public-facing API handler", framework="FastAPI", always=True)

    assert agent.agent_prompt.to_prompt_object().info == {
        "context": "Public-facing API handler",
        "framework": "FastAPI",
    }
