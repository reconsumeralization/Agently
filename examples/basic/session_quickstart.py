from agently import Agently
from agently.core import ModelRequest
from agently.core.Session import Session
from agently.utils import DataFormatter

# Quickstart: standalone lite mode
standalone_session = Session().use_lite(chars=2000)
standalone_session.append_message({"role": "user", "content": "Hello"})
standalone_session.append_message({"role": "assistant", "content": "Hi there"})
standalone_session.resize()  # apply resize policy if needed

# Quickstart: Agent integration via SessionExtension
lite_agent = Agently.create_agent()
lite_agent.enable_session_lite(chars=2000)
lite_agent.add_chat_history({"role": "user", "content": "How are you?"})

# Advanced memo summarization with local Ollama (qwen2.5:7b)
Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "model_type": "chat",
        "options": {"temperature": 0.2},
    },
)


memo_agent = Agently.create_agent()


def memo_update_handler(memo, messages, attachments, settings):
    requester = ModelRequest(
        memo_agent.plugin_manager,
        agent_name=memo_agent.name,
        parent_settings=settings,
    )
    serialized_messages = []
    for message in messages:
        if hasattr(message, "model_dump"):
            serialized_messages.append(message.model_dump())
        else:
            serialized_messages.append(message)
    prompt_input = {
        "current_memo": memo,
        "messages": DataFormatter.sanitize(serialized_messages),
        "attachments": attachments,
    }
    instruct = [
        "You are a memory manager for a long conversation.",
        "Maintain a stable memo with keys: profile, preferences, constraints, tasks, decisions, facts, recent_summary.",
        "Only keep durable information; remove transient chit-chat.",
        "Deduplicate items and keep lists concise.",
        "Return the updated memo dictionary only.",
    ]
    output_schema = {"memo": (dict, "Updated memo dictionary")}
    data = requester.input(prompt_input).instruct(instruct).output(output_schema).get_data()
    if isinstance(data, dict) and isinstance(data.get("memo"), dict):
        return data["memo"]
    if isinstance(data, dict):
        return data
    return memo


advanced_session = Session(
    parent_settings=memo_agent.settings,
    agent=memo_agent,
    memo_update_handler=memo_update_handler,
).configure(
    mode="memo",
    limit={"chars": 6000, "messages": 12},
    every_n_turns=2,
)

memo_agent.attach_session(advanced_session)
assert memo_agent.session is not None


def stream_input(agent, text: str):
    print(f"\n[User]: {text}\n[Assistant]: ", end="", flush=True)
    gen = agent.input(text).get_generator(type="specific")
    for event, content in gen:
        if event == "delta":
            print(content, end="", flush=True)
    print("")


# When you call agent.start()/get_data(), the session hooks will inject chat_history
# and record user/assistant messages automatically.
stream_input(memo_agent, "I am Alex, based in PST. Please keep answers concise.")
stream_input(memo_agent, "Track that I prefer JSON responses and I am building a finance app.")

print("Memo snapshot:")
print(memo_agent.session.memo)
