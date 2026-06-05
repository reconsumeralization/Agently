import sqlite3
from typing import Any
import importlib

import pytest

from agently import Agently
from agently.builtins.actions import Browse, Cmd, Search
from agently.builtins.tools import Browse as LegacyBrowse
from agently.builtins.tools import Cmd as LegacyCmd
from agently.builtins.tools import Search as LegacySearch


def test_builtins_actions_is_preferred_import_path_and_tools_is_facade():
    assert issubclass(LegacySearch, Search)
    assert issubclass(LegacyBrowse, Browse)
    assert issubclass(LegacyCmd, Cmd)
    assert not hasattr(Search(timeout=1), "tool_info_list")
    assert hasattr(LegacySearch(timeout=1), "tool_info_list")
    assert not hasattr(Browse(), "tool_info_list")
    assert hasattr(LegacyBrowse(), "tool_info_list")
    assert not hasattr(Cmd(), "tool_info_list")
    assert hasattr(LegacyCmd(), "tool_info_list")


def test_v2_default_plugins_are_registered():
    action_executors = set(Agently.plugin_manager.get_plugin_list("ActionExecutor"))
    environment_providers = set(Agently.plugin_manager.get_plugin_list("ExecutionEnvironmentProvider"))

    assert {
        "SearchActionExecutor",
        "BrowseActionExecutor",
        "NodeJSActionExecutor",
        "DockerActionExecutor",
        "SQLiteActionExecutor",
    }.issubset(action_executors)
    assert {
        "NodeExecutionEnvironmentProvider",
        "DockerExecutionEnvironmentProvider",
        "BrowserExecutionEnvironmentProvider",
        "SQLiteExecutionEnvironmentProvider",
    }.issubset(environment_providers)


def test_agent_use_actions_accepts_search_package():
    agent = Agently.create_agent()
    search = Search(timeout=1)

    async def fake_search(query: str, timelimit=None, max_results: int | None = 10):
        return [{"title": query, "href": "https://example.com", "body": f"limit={max_results}"}]

    search.search = fake_search
    agent.use_actions(search)

    action_ids = {item.get("action_id") for item in agent.action.get_action_list(tags=[f"agent-{ agent.name }"])}
    assert {"search", "search_news", "search_wikipedia", "search_arxiv"}.issubset(action_ids)

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})
    assert result.get("status") == "success"
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "limit=2"}]


def test_agent_use_actions_accepts_browse_package():
    agent = Agently.create_agent()
    browse = Browse(enable_playwright=False, enable_bs4=False)

    async def fake_browse(url: str):
        return f"content from {url}"

    browse.browse = fake_browse
    agent.use_actions(browse)

    spec = agent.action.action_registry.get_spec("browse")
    assert spec is not None
    assert spec.get("executor_type") == "browse"
    assert spec.get("meta", {}).get("component") == "builtins.actions.Browse"

    result = agent.action.execute_action("browse", {"url": "https://example.com"})
    assert result.get("status") == "success"
    assert result.get("data") == "content from https://example.com"


@pytest.mark.asyncio
async def test_action_runtime_default_planning_uses_configured_model_key(monkeypatch):
    import agently.core as agently_core

    agent = Agently.create_agent()
    agent.settings.set("action.planning_model_key", "task-main")
    seen_model_keys: list[str | None] = []

    class FakeResult:
        async def async_get_data(self):
            return {"next_action": "response", "execution_commands": []}

    class FakeResponse:
        result = FakeResult()

        async def get_async_generator(self, *_, **__):
            if False:
                yield None

    class FakeModelRequest:
        def __init__(self, *_, model_key=None, **__):
            seen_model_keys.append(model_key)

        def input(self, *_args, **_kwargs):
            return self

        def info(self, *_args, **_kwargs):
            return self

        def instruct(self, *_args, **_kwargs):
            return self

        def output(self, *_args, **_kwargs):
            return self

        def get_response(self, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(agently_core, "ModelRequest", FakeModelRequest)
    prompt = Agently.create_prompt()
    prompt.set("input", "collect evidence")

    decision = await agent.action.action_runtime._default_structured_planning_handler(
        {
            "prompt": prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "round_index": 0,
            "max_rounds": 1,
            "done_plans": [],
            "last_round_records": [],
        },
        {"action_list": [{"name": "search"}]},
    )

    assert seen_model_keys == ["task-main"]
    assert decision["next_action"] == "response"


@pytest.mark.asyncio
async def test_action_runtime_native_tool_planning_uses_configured_model_key(monkeypatch):
    import agently.core as agently_core

    agent = Agently.create_agent()
    agent.settings.set("action.planning_model_key", "task-main")
    seen_model_keys: list[str | None] = []

    class FakePrompt:
        def set(self, *_args, **_kwargs):
            return None

    class FakeResponse:
        def get_async_generator(self, *_, **__):
            async def generate():
                yield "done", None

            return generate()

    class FakeModelRequest:
        prompt = FakePrompt()

        def __init__(self, *_, model_key=None, **__):
            seen_model_keys.append(model_key)

        def input(self, *_args, **_kwargs):
            return self

        def info(self, *_args, **_kwargs):
            return self

        def instruct(self, *_args, **_kwargs):
            return self

        def get_response(self, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(agently_core, "ModelRequest", FakeModelRequest)
    prompt = Agently.create_prompt()
    prompt.set("input", "collect evidence")

    decision = await agent.action.action_runtime._default_native_tool_call_planning_handler(
        {
            "prompt": prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "round_index": 0,
            "max_rounds": 1,
            "done_plans": [],
            "last_round_records": [],
        },
        {"action_list": [{"name": "search"}]},
    )

    assert seen_model_keys == ["task-main"]
    assert decision["next_action"] == "response"


def test_agent_enable_sqlite_registers_managed_sqlite_action(tmp_path):
    db_path = tmp_path / "items.db"
    connection = sqlite3.connect(db_path)
    connection.execute("create table items (id integer primary key, name text)")
    connection.execute("insert into items (name) values (?)", ("Agently",))
    connection.commit()
    connection.close()

    agent = Agently.create_agent()
    agent.enable_sqlite(database=str(db_path), action_id="test_query_sqlite")

    spec = agent.action.action_registry.get_spec("test_query_sqlite")
    assert spec is not None
    environments = spec.get("execution_environments", [])
    assert environments and environments[0].get("kind") == "sqlite"

    result = agent.action.execute_action(
        "test_query_sqlite",
        {"query": "select name from items where id = ?", "params": [1]},
    )
    assert result.get("status") == "success"
    data = result.get("data")
    assert isinstance(data, dict)
    assert data.get("rows") == [{"name": "Agently"}]


def test_instruction_heavy_action_records_digest_and_artifact_recall(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(root=tmp_path, commands=["pwd"], action_id="test_recall_bash")

    result = agent.action.execute_action(
        "test_recall_bash",
        {"cmd": "pwd", "workdir": str(tmp_path), "api_token": "secret-value"},
        purpose="Inspect cwd",
    )

    assert result.get("status") == "success"
    assert str(tmp_path) in str(result.get("data", {}).get("stdout", ""))

    digest = result.get("model_digest")
    assert isinstance(digest, dict)
    assert digest.get("action_id") == "test_recall_bash"
    assert digest.get("instruction", {}).get("kind") == "cmd"
    assert "artifact_refs" in digest

    artifact_refs = result.get("artifact_refs")
    assert isinstance(artifact_refs, list)
    assert len(artifact_refs) >= 2
    assert "api_token" in result.get("redaction_report", [])
    input_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_input")
    output_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_output")
    input_artifact_id = input_ref.get("artifact_id")
    input_action_call_id = input_ref.get("action_call_id")
    output_artifact_id = output_ref.get("artifact_id")
    output_action_call_id = output_ref.get("action_call_id")
    assert isinstance(input_artifact_id, str)
    assert isinstance(input_action_call_id, str)
    assert isinstance(output_artifact_id, str)
    assert isinstance(output_action_call_id, str)

    recalled_input = agent.action.read_action_artifact(
        artifact_id=input_artifact_id,
        action_call_id=input_action_call_id,
    )
    assert recalled_input.get("value", {}).get("api_token") == "[REDACTED]"

    recalled = agent.action.read_action_artifact(
        artifact_id=output_artifact_id,
        action_call_id=output_action_call_id,
    )
    assert recalled.get("ok") is True
    assert str(tmp_path) in str(recalled.get("value", {}).get("stdout", ""))

    prompt_results = agent.action.to_action_results([result])
    visible = prompt_results["Inspect cwd"]
    assert isinstance(visible, dict)
    assert visible.get("action_call_id") == result.get("action_call_id")
    assert visible.get("artifact_refs") == artifact_refs


@pytest.mark.asyncio
async def test_action_loop_uses_digest_and_exposes_recall_after_artifacts(tmp_path):
    agent = Agently.create_agent()
    tag = f"recall-loop-{agent.name}"
    action_id = "test_loop_recall_bash"
    agent.action.register_bash_sandbox_action(
        action_id=action_id,
        tags=[tag],
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(tmp_path)],
        expose_to_model=True,
    )
    spec = agent.action.action_registry.get_spec(action_id)
    assert spec is not None
    spec_desc = str(spec.get("desc", ""))
    assert "Allowed command prefixes: pwd." in spec_desc
    assert f"Allowed working directory roots: {tmp_path}" in spec_desc
    assert "Timeout: 20 seconds." in spec_desc
    prompt = Agently.create_prompt()
    prompt.set("input", "inspect cwd")
    seen_rounds: list[dict[str, Any]] = []

    async def planning_handler(context, request):
        seen_rounds.append(
            {
                "round_index": context.get("round_index"),
                "last_round_records": context.get("last_round_records", []),
                "action_ids": [item.get("action_id") for item in request.get("action_list", [])],
            }
        )
        if context.get("round_index") == 0:
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "Inspect cwd",
                        "action_id": action_id,
                        "action_input": {"cmd": "pwd", "workdir": str(tmp_path)},
                        "todo_suggestion": "respond",
                    }
                ],
            }
        return {
            "next_action": "response",
            "action_calls": [],
        }

    records = await agent.action.async_plan_and_execute(
        prompt=prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(tags=[tag]),
        agent_name=agent.name,
        planning_handler=planning_handler,
        max_rounds=3,
    )

    assert len(records) == 1
    assert str(tmp_path) in str(records[0].get("data", {}).get("stdout", ""))
    assert len(seen_rounds) >= 2
    second_round = seen_rounds[1]
    assert "read_action_artifact" in second_round["action_ids"]
    visible_record = second_round["last_round_records"][0]
    assert visible_record.get("data") == records[0].get("model_digest")
    assert visible_record.get("result") == records[0].get("model_digest")


def test_search_package_does_not_load_backend_during_registration(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def fake_import_package(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Search backend should be loaded lazily.")

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", fake_import_package)
    agent = Agently.create_agent()
    agent.use_actions(Search(timeout=1))

    assert calls == []


def test_search_package_falls_back_when_ddgs_backend_has_no_results(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            backend = kwargs.get("backend")
            if backend == "yahoo":
                raise RuntimeError("No results found.")
            return [{"title": query, "href": "https://example.com", "body": f"backend={backend}"}]

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(backend="yahoo", search_fallback_backends=["brave"]))

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})

    assert result.get("status") == "partial_success"
    assert result.get("success") is True
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "backend=brave"}]


def test_search_package_reports_partial_success_when_fallback_recovers(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            backend = kwargs.get("backend")
            if backend == "yahoo":
                raise RuntimeError("HTTP 429")
            return [{"title": query, "href": "https://example.com", "body": f"backend={backend}"}]

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(backend="yahoo", search_fallback_backends=["google"]))

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})

    assert result.get("status") == "partial_success"
    assert result.get("success") is True
    assert result.get("ok") is True
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "backend=google"}]
    assert result.get("meta", {}).get("backend") == "google"
    assert result.get("meta", {}).get("failed_backends") == ["yahoo"]
    assert result.get("diagnostics", [])[0].get("code") == "search_backend_failed"


def test_search_package_continues_after_empty_backend(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            backend = kwargs.get("backend")
            if backend == "yahoo":
                return []
            return [{"title": query, "href": "https://example.com", "body": f"backend={backend}"}]

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(backend="yahoo", search_fallback_backends=["google"]))

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})

    assert result.get("status") == "partial_success"
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "backend=google"}]
    assert result.get("meta", {}).get("empty_backends") == ["yahoo"]


def test_search_package_treats_no_results_as_empty_success(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def news(self, query: str, **kwargs):
            raise RuntimeError("No results found.")

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(news_backend="yahoo", news_fallback_backends=["duckduckgo"]))

    result = agent.action.execute_action("search_news", {"query": "Agently Moxin", "max_results": 2})

    assert result.get("status") == "success"
    assert result.get("data") == []
