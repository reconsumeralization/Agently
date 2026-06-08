import asyncio
import time
from pprint import pprint

from agently import TriggerFlow

from _shared_model import configure_model, print_model_provider


TASK_DURATIONS = {"t1": 0.8, "t2": 0.5, "t3": 0.7, "t4": 0.2, "t5": 0.2}


def decompose_task(task: str) -> list[dict]:
    from agently import Agently

    result = (
        Agently.create_agent()
        .input(task)
        .instruct([
            "Decompose the task into 4 to 6 concrete implementation tasks.",
            "Choose concise stable ids yourself; do not use a predeclared template.",
            "Include at least two tasks that can start immediately with empty depends_on lists.",
            "Include at least one task that depends on earlier work.",
            "Keep every depends_on id valid, keep the dependency graph acyclic, and avoid self-dependencies.",
        ])
        .output({
            "tasks": [{
                "id": ("str", "task id"),
                "title": ("str", "short task title"),
                "description": ("str", "what to do"),
                "depends_on": (["str"], "task ids that must finish first"),
            }],
            "summary": ("str", "decomposition summary"),
        })
        .get_result()
    )
    return result.get_data(ensure_keys=["tasks"])["tasks"]


def _task_id(task: dict) -> str:
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise AssertionError(f"Every task must have a non-empty string id: {task}")
    return task_id


def _dependencies(task: dict) -> list[str]:
    depends_on = task.get("depends_on") or []
    if not isinstance(depends_on, list):
        raise AssertionError(f"Task {_task_id(task)} depends_on must be a list")
    if any(not isinstance(dep, str) or not dep.strip() for dep in depends_on):
        raise AssertionError(f"Task {_task_id(task)} has invalid dependency ids: {depends_on}")
    return depends_on


def validate_dependency_plan(tasks: list[dict]) -> None:
    if not 4 <= len(tasks) <= 6:
        raise AssertionError(f"Expected 4 to 6 tasks, got {len(tasks)}")

    ids = [_task_id(task) for task in tasks]
    if len(set(ids)) != len(ids):
        raise AssertionError(f"Task ids must be unique: {ids}")

    known_ids = set(ids)
    for task in tasks:
        task_id = _task_id(task)
        depends_on = _dependencies(task)
        if task_id in depends_on:
            raise AssertionError(f"Task {task_id} cannot depend on itself")
        missing = [dep for dep in depends_on if dep not in known_ids]
        if missing:
            raise AssertionError(f"Task {task_id} depends on unknown ids: {missing}")

    pending = set(known_ids)
    completed: set[str] = set()
    by_id = {_task_id(task): task for task in tasks}
    while pending:
        ready = [
            task_id
            for task_id in pending
            if set(_dependencies(by_id[task_id])).issubset(completed)
        ]
        if not ready:
            raise AssertionError("Task dependency graph must be acyclic")
        completed.update(ready)
        pending.difference_update(ready)


def split_by_dependency(tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    independent = [task for task in tasks if not task.get("depends_on")]
    dependent = [task for task in tasks if task.get("depends_on")]
    return independent, dependent


def build_parallel_executor():
    flow = TriggerFlow(name="cookbook-todo-concurrent-model")

    async def execute_one(data):
        task = data.input
        await asyncio.sleep(TASK_DURATIONS.get(task["id"], 0.6))
        return {
            "id": task["id"],
            "title": task["title"],
            "result": f"{task['title']} done",
        }

    async def collect(data):
        await data.async_set_state("results", data.input)

    flow.for_each(concurrency=3).to(execute_one).end_for_each().to(collect)
    return flow


async def run_parallel(tasks: list[dict]) -> tuple[float, list[dict]]:
    flow = build_parallel_executor()
    started = time.perf_counter()
    execution = flow.create_execution(auto_close_timeout=0.0)
    await execution.async_start(tasks)
    state = await execution.async_close()
    return time.perf_counter() - started, state["results"]


async def run_dependency_plan(tasks: list[dict]) -> tuple[float, list[dict], list[list[str]]]:
    pending = {_task_id(task): task for task in tasks}
    completed: set[str] = set()
    results: list[dict] = []
    waves: list[list[str]] = []
    started = time.perf_counter()

    while pending:
        ready = [
            task
            for task in pending.values()
            if set(_dependencies(task)).issubset(completed)
        ]
        if not ready:
            raise AssertionError("No executable task wave; dependency graph is invalid")

        _, wave_results = await run_parallel(ready)
        wave_ids = [_task_id(item) for item in wave_results]
        waves.append(wave_ids)
        results.extend(wave_results)
        completed.update(wave_ids)
        for task_id in wave_ids:
            pending.pop(task_id, None)

    return time.perf_counter() - started, results, waves


async def run_sequential(tasks: list[dict]) -> tuple[float, list[dict]]:
    started = time.perf_counter()
    results = []
    for task in tasks:
        await asyncio.sleep(TASK_DURATIONS.get(task["id"], 0.6))
        results.append({"id": task["id"], "result": f"{task['title']} done"})
    return time.perf_counter() - started, results


async def main_async():
    provider = configure_model(temperature=0.0)
    print_model_provider(provider)

    tasks = decompose_task(
        "Build a bilingual customer-service bot that answers FAQs, checks order status, and hands off to a human agent."
    )
    validate_dependency_plan(tasks)
    independent, dependent = split_by_dependency(tasks)

    sequential_elapsed, _ = await run_sequential(tasks)
    parallel_elapsed, parallel_results, execution_waves = await run_dependency_plan(tasks)

    summary = {
        "model_tasks": tasks,
        "task_ids": [task["id"] for task in tasks],
        "independent_ids": [task["id"] for task in independent],
        "dependent_ids": [task["id"] for task in dependent],
        "parallel_result_ids": [item["id"] for item in parallel_results],
        "execution_waves": execution_waves,
        "sequential_elapsed": round(sequential_elapsed, 3),
        "parallel_elapsed": round(parallel_elapsed, 3),
        "parallel_is_faster": parallel_elapsed < sequential_elapsed,
    }

    print("[TODO_CONCURRENCY_SUMMARY]")
    pprint(summary)

    assert 4 <= len(summary["task_ids"]) <= 6
    assert len(summary["independent_ids"]) >= 2
    assert len(summary["dependent_ids"]) >= 1
    assert set(summary["parallel_result_ids"]) == set(summary["task_ids"])
    assert summary["parallel_is_faster"] is True


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

# Expected key output from a real DeepSeek run on 2026-06-08:
# [MODEL_PROVIDER] prints deepseek.
# [TODO_CONCURRENCY_SUMMARY] contains model_tasks generated by the model.
# task_ids were ['db-setup', 'channel-setup', 'nlu-model', 'core-services',
# 'handoff', 'integration-test'].
# independent_ids were ['db-setup', 'channel-setup']; dependent_ids were
# ['nlu-model', 'core-services', 'handoff', 'integration-test'].
# execution_waves were [['db-setup', 'channel-setup'], ['nlu-model',
# 'core-services'], ['handoff'], ['integration-test']].
# parallel_is_faster is True.

# How it works:
# The model generates a task list with dependency metadata (id, title,
# depends_on). It owns task titles, ids, and dependency choices inside the
# constraints needed to demonstrate concurrency.
# Local validation checks that ids are unique, dependencies reference known
# tasks, and the graph is acyclic. The host executor then runs every ready wave
# with TriggerFlow for_each(concurrency=3) until all tasks finish.
# The example asserts that every model-generated task ran once and that
# dependency-aware parallel execution is faster than fully sequential execution.
#
# Verified flow shape from the 2026-06-08 DeepSeek run:
# model generated 6 task records with two empty depends_on lists and four
# dependent tasks.
#   |
#   v
# validate_dependency_plan(tasks) confirms ids, references, and acyclic deps
#   |
#   v
# ready tasks run in TriggerFlow parallel waves until all task ids complete
# parallel_is_faster == True
