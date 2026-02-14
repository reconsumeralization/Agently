import pytest

from agently import Agently


def test_session_extension_activate_and_override_chat_history():
    agent = Agently.create_agent()
    agent.activate_session(session_id="session-extension-test")
    assert agent.activated_session is not None

    agent.add_chat_history({"role": "user", "content": "hello"})

    assert len(agent.activated_session.full_context) == 1
    assert agent.activated_session.full_context[0].content == "hello"
    assert len(agent.activated_session.context_window) == 1

    prompt_chat_history = agent.agent_prompt.get("chat_history")
    assert isinstance(prompt_chat_history, list)
    assert len(prompt_chat_history) == 1


def test_session_extension_clean_context_window():
    agent = Agently.create_agent()
    agent.activate_session(session_id="session-extension-test-clean")
    assert agent.activated_session is not None

    agent.add_chat_history({"role": "user", "content": "hello"})
    agent.clean_context_window()

    assert len(agent.activated_session.context_window) == 0
    assert agent.agent_prompt.get("chat_history") == []


@pytest.mark.asyncio
async def test_session_extension_request_prefix_syncs_context_window():
    agent = Agently.create_agent()
    agent.activate_session(session_id="session-extension-test-prefix")
    assert agent.activated_session is not None

    agent.add_chat_history({"role": "user", "content": "from-session"})
    prompt = agent.request_prompt
    prompt.set("chat_history", [{"role": "assistant", "content": "stale"}])

    await agent._session_request_prefix(prompt, agent.settings)

    synced_history = prompt.get("chat_history")
    assert isinstance(synced_history, list)
    assert len(synced_history) == 1
    assert synced_history[0].content == "from-session"
