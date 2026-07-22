import pytest
from textwrap import indent
from typing import Any, cast

from itertools import repeat

from agently import Agently
from agently.core.session import Session
from agently.core.storage import RecordStore


@pytest.mark.asyncio
async def test_one_message_session():
    session = Session()
    assert isinstance(session.id, str)
    assert session._auto_resize is True
    assert session.session_settings.get("max_length", None) is None
    session.session_settings.set("max_length", 100)
    await session.async_add_chat_history({"role": "user", "content": "hi" * 100})
    assert len(session.context_window[-1].content) == 100


@pytest.mark.asyncio
async def test_multi_messages_session():
    session = Session()
    assert isinstance(session.id, str)
    assert session._auto_resize is True
    assert session.session_settings.get("max_length", None) is None
    session.session_settings.set("max_length", 100)
    await session.async_add_chat_history([{"role": "user", "content": "hi"} for _ in repeat(None, 100)])
    total_length = 0
    for message in session.context_window:
        total_length += len(str(message.model_dump()))
    max_length = session.session_settings.get("max_length")
    assert isinstance(max_length, int)
    assert total_length <= max_length
    assert len(session.context_window) > 0


def test_session_json_export_and_load():
    session = Session(id="session-1", auto_resize=False)
    session.session_settings.set("max_length", 123)
    session.add_chat_history(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
    )

    json_data = session.get_json_session()
    loaded = Session(auto_resize=True)
    loaded.load_json_session(json_data)

    assert loaded.id == "session-1"
    assert loaded.session_settings.get("max_length") == 123
    assert len(loaded.full_context) == 2
    assert len(loaded.context_window) == 2


def test_session_yaml_export_and_load_by_path():
    session = Session(id="session-2", auto_resize=False)
    session.add_chat_history({"role": "user", "content": "content-from-yaml"})

    yaml_data = session.get_yaml_session()
    wrapped_yaml_data = f"payload:\n  session:\n{indent(yaml_data, '    ')}"

    loaded = Session(auto_resize=True)
    loaded.load_yaml_session(wrapped_yaml_data, session_key_path="payload.session")

    assert loaded.id == "session-2"
    assert len(loaded.context_window) == 1
    assert loaded.context_window[0].content == "content-from-yaml"


def test_session_set_then_add_chat_history_does_not_duplicate_turns():
    session = Session(auto_resize=False)
    session.set_chat_history(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
    )

    session.add_chat_history({"role": "user", "content": "follow-up"})

    history = session.full_context
    assert len(history) == 3
    assert [message.content for message in history] == ["hello", "world", "follow-up"]
    assert [message.content for message in session.context_window] == ["hello", "world", "follow-up"]


@pytest.mark.asyncio
async def test_session_legacy_execution_aliases():
    session = Session(auto_resize=False)
    await session.async_add_chat_history({"role": "user", "content": "hello"})

    async def execution_handler(full_context, context_window, memo, session_settings):
        _ = (full_context, context_window, session_settings)
        return None, [], memo

    with pytest.warns(DeprecationWarning):
        session.register_execution_handlers("legacy_drop", execution_handler)

    assert "legacy_drop" in session._resize_handlers
    assert "legacy_drop" in session._execution_handlers

    with pytest.warns(DeprecationWarning):
        await session.async_execute_strategy("legacy_drop")
    assert len(session.context_window) == 0


def test_agent_session_memory_binds_agent_record_store(tmp_path):
    agent = Agently.create_agent("memory-bind")
    agent.use_record_store(tmp_path / "memory-bind-a", mode="read_write")
    agent.activate_session(session_id="support-demo")

    assert agent.activated_session is not None
    agent.activated_session.use_memory(mode="AgentlyMemory")

    assert agent.activated_session.memory is not None
    assert agent.activated_session.memory.memory_store is agent.record_store

    agent.use_record_store(tmp_path / "memory-bind-b", mode="read_write")
    assert agent.activated_session.memory.memory_store is agent.record_store


@pytest.mark.asyncio
async def test_standalone_session_memory_requires_record_store():
    agent = Agently.create_agent("memory-no-workspace-prompt")
    session = Session(plugin_manager=Agently.plugin_manager, settings=Agently.settings)
    session.use_memory(mode="AgentlyMemory")

    with pytest.raises(RuntimeError, match="requires a RecordStore"):
        await session.async_prepare_memory(agent.create_temp_request().prompt, session.settings)


@pytest.mark.asyncio
async def test_session_memory_stores_record_store_records_with_fixed_fields(tmp_path):
    record_store = RecordStore(tmp_path / "session-memory-records", mode="read_write")
    session = Session(
        id="memory-session",
        plugin_manager=Agently.plugin_manager,
        settings=Agently.settings,
        memory_store=record_store,
    )
    session.use_memory(mode="AgentlyMemory")

    async def fake_extract_memories(*, session, user_content, assistant_content):
        _ = (session, user_content, assistant_content)
        return [
            {
                "scope": "SESSION_MEMORY",
                "summary": "prefers concise updates",
                "body": {"preference": "concise updates"},
                "tags": ["preference", "project"],
                "importance": 0.8,
            }
        ]

    session.memory._extract_memories = fake_extract_memories
    diagnostics = await session.async_after_memory_turn(
        user_content="Please keep project updates concise.",
        assistant_content="I will keep that in mind.",
        result=cast(Any, None),
        settings=session.settings,
    )

    assert diagnostics["stored"] == 1
    refs = await record_store.grep(
        None,
        filters={
            "collection": "memory",
            "kind": "session_memory",
            "scope.memory_scope": "SESSION_MEMORY",
            "scope.session_id": "memory-session",
        },
    )
    assert len(refs) == 1
    data = await record_store.get_data(refs[0])
    assert data["memory_scope"] == "SESSION_MEMORY"
    assert data["body"] == {"preference": "concise updates"}
    assert data["tags"] == ["preference", "project"]
    assert data["provenance"]["plugin"] == "AgentlyMemory"
    assert data["provenance"]["session_id"] == "memory-session"
    assert "vector_index" in data
    assert refs[0]["meta"]["tags"] == ["preference", "project"]


@pytest.mark.asyncio
async def test_session_memory_recall_uses_task_context_without_prompt_slots(tmp_path):
    record_store = RecordStore(tmp_path / "session-memory-task-context", mode="read_write")
    ref = await record_store.put(
        {"memory": "Delivery updates must contain exactly two bullets and one risk line."},
        collection="memory",
        kind="session_memory",
        summary="Saved delivery update promise",
        scope={"memory_scope": "SESSION_MEMORY", "session_id": "memory-context"},
        meta={"tags": ["delivery", "promise"]},
    )
    session = Session(
        id="memory-context",
        plugin_manager=Agently.plugin_manager,
        settings=Agently.settings,
        memory_store=record_store,
    )
    session.use_memory(mode="AgentlyMemory")

    class SelectOfferedMemory:
        def __init__(self):
            self.cards = []

        def input(self, _value):
            return self

        def info(self, value):
            self.cards = list(value["offered_context_blocks"])
            return self

        def instruct(self, _value):
            return self

        def output(self, _value, *, format):
            assert format == "json"
            return self

        async def async_get_data(self):
            return {"selected_keys": [item["block_key"] for item in self.cards]}

    session.memory._create_model_request = lambda _phase: SelectOfferedMemory()
    request = Agently.create_agent("memory-task-context-request").create_temp_request()
    request.input("What delivery promise did we save?")

    diagnostics = await session.async_prepare_memory(request.prompt, session.settings)
    prompt_data = request.prompt.get()
    assert isinstance(prompt_data, dict)
    info = prompt_data.get("info", {})

    assert "GLOBAL_MEMORY" not in prompt_data
    assert "SESSION_MEMORY" not in prompt_data
    assert set(info["session_memory_context"]) == {"blocks"}
    assert set(info["session_memory_context"]["blocks"][0]) == {
        "content",
        "role",
        "source_ref",
        "completeness",
    }
    assert info["session_memory_context"]["blocks"][0]["source_ref"] == ref["id"]
    assert session.memory.diagnostics[-1]["path"] == "task_context"
    assert diagnostics["path"] == "task_context"


@pytest.mark.asyncio
async def test_session_memory_recall_fails_closed_when_semantic_selection_fails(tmp_path):
    record_store = RecordStore(tmp_path / "session-memory-selection-failure", mode="read_write")
    await record_store.put(
        {"memory": "Do not expose this without semantic selection."},
        collection="memory",
        kind="session_memory",
        summary="Protected optional memory",
        scope={"memory_scope": "SESSION_MEMORY", "session_id": "selection-failure"},
    )
    session = Session(
        id="selection-failure",
        plugin_manager=Agently.plugin_manager,
        settings=Agently.settings,
        memory_store=record_store,
    )
    session.use_memory(mode="AgentlyMemory")
    session.memory._create_model_request = lambda _phase: (_ for _ in ()).throw(
        RuntimeError("selector unavailable")
    )
    request = Agently.create_agent("memory-selection-failure").create_temp_request()
    request.input("Recall relevant memory")

    diagnostics = await session.async_prepare_memory(request.prompt, session.settings)

    assert request.prompt.get("info.session_memory_context") == {"blocks": []}
    assert any(
        item["code"] == "context.selection_failed"
        for item in diagnostics["diagnostics"]
    )


@pytest.mark.asyncio
async def test_session_memory_context_source_is_fixed_to_global_and_active_session(tmp_path):
    record_store = RecordStore(tmp_path / "session-memory-source-scope", mode="read_write")
    global_ref = await record_store.put(
        {"memory": "Global delivery policy."},
        collection="memory",
        kind="global_memory",
        summary="Global policy",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )
    active_ref = await record_store.put(
        {"memory": "Active session promise."},
        collection="memory",
        kind="session_memory",
        summary="Active promise",
        scope={"memory_scope": "SESSION_MEMORY", "session_id": "active"},
    )
    other_ref = await record_store.put(
        {"memory": "Other session secret."},
        collection="memory",
        kind="session_memory",
        summary="Other secret",
        scope={"memory_scope": "SESSION_MEMORY", "session_id": "other"},
    )
    session = Session(
        id="active",
        plugin_manager=Agently.plugin_manager,
        settings=Agently.settings,
        memory_store=record_store,
    )
    session.use_memory(mode="AgentlyMemory")

    source = session.memory.create_context_source(session=session, settings=session.settings)
    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=100,
    )

    assert source.source_kind == "session_memory"
    assert {item.source_ref for item in page.descriptors} == {
        global_ref["id"],
        active_ref["id"],
    }
    with pytest.raises(PermissionError, match="not authorized"):
        await source.async_read_exact(other_ref["id"], max_chars=1000)
