import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agently import Agently
from agently.core import TaskDAG


# DynamicTask submitted-DAG config example.
# The DAG lives in YAML so the graph can be reviewed or versioned separately
# from handler code. Runtime behavior is still owned by DynamicTask/TaskDAG.
# Expected key output from one local run:
# graph_id=policy-review-config
# topological_order=extract_terms,extract_dates,final_review
# semantic_final_task=final_review


CONFIG_PATH = Path(__file__).with_name("config_policy_review.yaml")


async def run_local_task(context):
    if context.dependency_results:
        deps = ",".join(
            f"{ task_id }={ result }"
            for task_id, result in sorted(context.dependency_results.items())
        )
        return f"{ context.task.id }({ deps })"
    return f"{ context.task.id }:{ context.graph_input['doc'] }"


async def main():
    graph = TaskDAG.from_yaml(CONFIG_PATH)
    task = Agently.create_dynamic_task(
        target="review policy",
        plan=graph,
        handlers={"local_handler": run_local_task},
    )
    validation = task.validate()
    snapshot = await task.async_run(graph_input={"doc": "policy"}, timeout=1)

    print(f"graph_id={ graph.graph_id }")
    print(f"topological_order={ ','.join(validation.topological_task_ids) }")
    print(f"semantic_final_task={ snapshot['semantic_outputs']['final']['task_id'] }")


if __name__ == "__main__":
    asyncio.run(main())
