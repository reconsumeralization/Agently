import pytest

from agently import Agently
from agently.core.Session import Session


def test_session_extension_attach_and_proxy():
    agent = Agently.create_agent()
    session = Session(parent_settings=agent.settings, agent=agent)
    agent.attach_session(session)

    agent.add_chat_history({"role": "user", "content": "hi"})
    assert agent.session is session
    assert len(session.full_chat_history) == 1
    assert session.full_chat_history[0].content == "hi"


def test_enable_session_shortcuts():
    agent = Agently.create_agent()
    agent.enable_session_lite(chars=20, messages=1)
    assert agent.session is not None
    assert agent.session.settings.get("session.mode") == "lite"
    assert agent.session.settings.get("session.resize.max_messages_text_length") == 20
    assert agent.session.settings.get("session.resize.max_keep_messages_count") == 1

    agent.enable_session_memo(chars=30)
    assert agent.session.settings.get("session.mode") == "memo"
    assert agent.session.settings.get("session.memo.enabled") is True
    assert agent.session.settings.get("session.resize.max_messages_text_length") == 30


@pytest.mark.asyncio
async def test_session_extension_request_prefix_injects_history():
    agent = Agently.create_agent()
    agent.attach_session()
    assert agent.session is not None

    agent.session.append_message({"role": "user", "content": "hello"})
    prompt = agent.request_prompt

    await agent._session_request_prefix(prompt, agent.settings)
    assert prompt.get("chat_history") == agent.session.current_chat_history


@pytest.mark.asyncio
async def test_session_extension_finally_records_messages():
    agent = Agently.create_agent()
    agent.attach_session()
    assert agent.session is not None

    prompt = agent.request_prompt
    prompt.set("input", "question")

    class DummyResult:
        def __init__(self, prompt, text):
            self.prompt = prompt
            self._text = text

        async def async_get_text(self):
            return self._text

    await agent._session_finally(DummyResult(prompt, "answer"), agent.settings)

    assert len(agent.session.full_chat_history) == 2
    assert agent.session.full_chat_history[0].role == "user"
    assert agent.session.full_chat_history[0].content == "question"
    assert agent.session.full_chat_history[1].role == "assistant"
    assert agent.session.full_chat_history[1].content == "answer"
