from pprint import pprint

from agently import Agently
from agently.types.data import ActionResult

from _shared_model import configure_model, print_model_provider


def plan_next_step(history: list[dict]) -> dict:
    result = (
        Agently.create_agent()
        .input({
            "task": "Calculate (12 + 7) * 3.",
            "history": history,
            "available_actions": [
                {"action_id": "add", "kwargs": {"a": "int", "b": "int"}},
                {"action_id": "multiply", "kwargs": {"a": "int", "b": "int"}},
            ],
        })
        .instruct([
            "You are the planner in a plan -> action -> observe loop.",
            "Use the available actions to compute arithmetic instead of doing arithmetic silently.",
            "Inspect the task expression, the available action contracts, and the history.",
            "If another action is needed, choose exactly one available action and provide concrete integer kwargs.",
            "Return final only after the action history contains enough evidence to support the complete expression value.",
            "Do not skip action execution.",
        ])
        .output({
            "type": ("'action' | 'final'", "next step type"),
            "action_id": ("'add' | 'multiply' | null", "action to execute when type is action"),
            "purpose": ("str | null", "why this action is needed"),
            "action_input": ("dict | null", "kwargs for the action"),
            "answer": ("str | null", "final answer when type is final"),
        })
        .get_result()
    )
    return result.get_data(ensure_keys=["type"])


def main():
    provider = configure_model(temperature=0.0)
    print_model_provider(provider)

    agent = Agently.create_agent()

    @agent.action_func
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @agent.action_func
    def multiply(a: int, b: int) -> int:
        """Multiply two integers."""
        return a * b

    agent.use_actions([add, multiply])

    history: list[dict] = []
    records: list[ActionResult] = []
    answer = ""

    for round_no in range(1, 5):
        decision = plan_next_step(history)
        print(f"[MODEL_DECISION_ROUND_{ round_no }]")
        pprint(decision)

        if decision["type"] == "final":
            answer = decision.get("answer") or ""
            break

        action_id = decision["action_id"]
        action_input = decision["action_input"] or {}
        record = agent.action.execute_action(
            action_id,
            action_input,
            purpose=decision.get("purpose") or f"Use { action_id }",
        )
        records.append(record)
        history.append({
            "action_id": record.get("action_id"),
            "kwargs": record.get("kwargs"),
            "result": record.get("result"),
            "status": record.get("status"),
        })

    print("[ACTION_HISTORY]")
    pprint(history)
    print("[FINAL_ANSWER]")
    print(answer)

    results = [record.get("result") for record in records]
    assert 19 in results
    assert 57 in results
    assert "57" in answer


if __name__ == "__main__":
    main()

# Expected key output from a real DeepSeek run on 2026-06-08:
# [MODEL_PROVIDER] prints deepseek.
# [MODEL_DECISION_ROUND_*] prints real model planner decisions.
# One action result is 19 and one action result is 57.
# [FINAL_ANSWER] prints 57.

# How it works:
# A plan-action-observe loop where the model acts as planner: each round it
# inspects the task, available action contracts, and history, then returns
# {type, action_id, action_input} or {type:"final", answer}. The prompt does not
# prescribe which action id or arguments must appear in a given round; local
# assertions only verify the observed action results and final answer.
# Local add() and multiply() are registered as @agent.action_func.
# execute_action() runs each planned action directly (no model planning of the call).
# The loop asserts that intermediate results 19 and 57 appear and the final answer is 57.
#
# Verified flow from the 2026-06-08 DeepSeek run:
# round 1: model plans add(12,7) -> 19; execute_action("add",{a:12,b:7}) -> 19
#   |
#   v
# round 2: model plans multiply(19,3) -> 57; execute_action("multiply",{a:19,b:3}) -> 57
#   |
#   v
# round 3: model returns type="final", answer="(12 + 7) * 3 = 57"
# assertions: 19 in results, 57 in results, "57" in answer
