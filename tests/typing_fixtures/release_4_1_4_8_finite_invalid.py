from agently import Agently


def sample_action(value: str) -> str:
    return value


agent = Agently.create_agent("release-finite-choice-negative")
agent.set_action_loop(planning_protocol="unknown_protocol")
agent.register_action(
    name="sample_action",
    desc="Return the supplied value.",
    kwargs={"value": str},
    func=sample_action,
    concurrency_mode="serial",
)
agent.enable_shell(environment="unrestricted")
agent.enable_shell(approval="sometimes")
agent.enable_shell(shell="cmd")
