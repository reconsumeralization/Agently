"""Strict installed-wheel consumer; this module does not execute commands."""
from collections.abc import Mapping
from agently import Agent, Agently
from agently.types.data import ShellResult, ShellRisk, ShellRiskHandler
from agently.types.plugins import ShellResource


def classify(facts: Mapping[str, object]) -> ShellRisk:
    return {"effects": [], "uncertainties": [str(facts["command"])], "reason": "Host review required"}


handler: ShellRiskHandler = classify
agent: Agent = Agently.create_agent().enable_shell(
    environment="offline", approval="write", shell="powershell", risk_handler=handler,
)


async def use_resource(resource: ShellResource) -> str:
    result: ShellResult = await resource.async_run("Get-Date", workdir=".")
    return result["stdout"]
