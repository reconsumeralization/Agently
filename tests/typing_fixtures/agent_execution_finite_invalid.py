from agently import Agently


agent = Agently.create_agent("finite-choice-negative")
agent.create_task(goal="ship", execution="parallel")
