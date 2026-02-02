from agently import Agently
from agently.core.Session import Session

# Standalone session usage (no Agent required)
standalone_session = Session().use_lite(chars=2000)
standalone_session.append_message({"role": "user", "content": "Hello"})
standalone_session.append_message({"role": "assistant", "content": "Hi there"})
standalone_session.resize()  # apply resize policy if needed

# Agent integration via SessionExtension
agent = Agently.create_agent()
agent.enable_session_lite(chars=2000)
agent.add_chat_history({"role": "user", "content": "How are you?"})

# When you call agent.start()/get_data(), the session hooks will inject chat_history
# and record user/assistant messages automatically.
