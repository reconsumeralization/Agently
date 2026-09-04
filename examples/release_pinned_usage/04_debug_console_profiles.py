"""Pinned human-readable debug console profile behavior.

Run:
    python examples/release_pinned_usage/04_debug_console_profiles.py

Expected key output:
    simple_prompt_readable=True
    simple_request_is_summary=True
    simple_stream_blocks=1
    simple_stream_not_repeated=True
    simple_stream_finishes_before_done=True
    detail_request_json=True
    detail_stream_blocks=1
    detail_final_materialized=True
    detail_has_no_runtime_progress_mirrors=True
    detail_agent_execution_lifecycle=True
    event_center_keeps_runtime_progress=True
    execution_resource_simple_self_check=True
    execution_resource_simple_pull_is_readable=True
    execution_resource_detail_probe=True
    action_planning_projection_is_compact=True
"""

from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently, RuntimeEvent
from agently.builtins.hookers.RuntimeConsoleSinkHooker import RuntimeConsoleSinkHooker
from examples.release_pinned_usage._local_requesters import (
    PinnedUsageDebugStreamRequester,
    create_debug_stream_agent,
)


def _run(profile: bool | str, name: str) -> tuple[str, list[RuntimeEvent]]:
    captured_events: list[RuntimeEvent] = []
    hook_name = f"release_pinned.debug_console.{name}"

    def capture(event: RuntimeEvent) -> None:
        captured_events.append(event)

    PinnedUsageDebugStreamRequester.reset()
    agent = create_debug_stream_agent(name).set_settings("debug", profile)
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            agent.input("PINNED_DEBUG_PROMPT").info("PINNED_DEBUG_CONTEXT").start()
    finally:
        Agently.event_center.unregister_hook(hook_name)
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
    return rendered, captured_events


def main() -> None:
    simple, _simple_events = _run(True, "release-pinned-debug-simple")
    detail, detail_events = _run("detail", "release-pinned-debug-detail")
    final_text = "PINNED_DEBUG_APINNED_DEBUG_B"
    event_center_keeps_runtime_progress = any(
        getattr(event, "event_type", None) in {"agent_execution.stream", "agent_execution.stream.delta"}
        and isinstance(getattr(event, "payload", None), dict)
        and event.payload.get("stream_kind") == "runtime_progress"
        for event in detail_events
    )
    environment_event = RuntimeEvent(
        event_type="execution_resource.failed",
        source="ExecutionResourceManager",
        level="ERROR",
        message="Execution environment check failed.",
        payload={
            "kind": "code_execution",
            "provider_id": "docker",
            "phase": "provider_probe",
            "error_code": "execution_resource.provider_unavailable",
            "reason": "Docker daemon is unavailable",
            "suggestion": "Start Docker; trusted code may explicitly opt into trusted_local.",
            "provider_probes": [{"provider_id": "docker", "available": False}],
        },
    )
    image_progress_event = RuntimeEvent(
        event_type="execution_resource.progress",
        source="ExecutionResourceManager",
        message="75782e20ea1f: Download complete",
        payload={
            "provider_id": "docker",
            "phase": "image_pull_progress",
            "image": "node:22-slim",
            "line": "75782e20ea1f: Download complete",
        },
    )
    image_inspection_event = RuntimeEvent(
        event_type="execution_resource.progress",
        source="ExecutionResourceManager",
        payload={
            "provider_id": "docker",
            "phase": "image_inspection",
            "image": "node:22-slim",
        },
    )
    simple_environment_output = io.StringIO()
    detail_environment_output = io.StringIO()
    with redirect_stdout(simple_environment_output):
        RuntimeConsoleSinkHooker._handle_execution_resource_event(environment_event, "simple")
        RuntimeConsoleSinkHooker._handle_execution_resource_event(image_progress_event, "simple")
        RuntimeConsoleSinkHooker._handle_execution_resource_event(image_inspection_event, "simple")
    with redirect_stdout(detail_environment_output):
        RuntimeConsoleSinkHooker._handle_execution_resource_event(environment_event, "detail")
    simple_environment = re.sub(r"\x1b\[[0-9;]*m", "", simple_environment_output.getvalue())
    detail_environment = re.sub(r"\x1b\[[0-9;]*m", "", detail_environment_output.getvalue())
    planning_projection = Agently.action._to_model_planning_action_list(
        [
            {
                "name": "run_nodejs_code",
                "desc": "Run JavaScript.",
                "kwargs": {"js_code": "JavaScript source"},
                "execution_resources": [{"kind": "code_execution", "config": {"image": "node:22-slim"}}],
                "executor_type": "NodejsSandbox",
            }
        ]
    )[0]
    simple_environment_self_check = (
        "[Environment] [Docker]" in simple_environment
        and "Docker daemon is unavailable" in simple_environment
        and "Next step:" in simple_environment
        and "provider=" not in simple_environment
        and "phase=" not in simple_environment
    )
    simple_pull_is_readable = (
        "[Environment] [Docker image node:22-slim] Downloaded layer 75782e20ea1f."
        in simple_environment
        and "Stage: Preparing" not in simple_environment
        and "Stage: Checking Image" not in simple_environment
    )

    print(f"simple_prompt_readable={'PINNED_DEBUG_PROMPT' in simple and 'Stage: Prompt' in simple}")
    print(f"simple_request_is_summary={'provider=' in simple and 'request_options' not in simple}")
    print(f"simple_stream_blocks={simple.count('Stage: Streaming')}")
    print(f"simple_stream_not_repeated={simple.count(final_text) == 1}")
    print(
        "simple_stream_finishes_before_done="
        f"{simple.index(final_text) < simple.index('Stage: Done') and 'Stage: Streaming' not in simple[simple.index('Stage: Done') :]}"
    )
    print(f"detail_request_json={'request_options' in detail and 'request_url' in detail}")
    print(f"detail_stream_blocks={detail.count('Stage: Streaming')}")
    print(f"detail_final_materialized={detail.count(final_text) == 2}")
    print(f"detail_has_no_runtime_progress_mirrors={'runtime.progress.' not in detail}")
    print(
        "detail_agent_execution_lifecycle="
        f"{'[AgentExecution]' in detail and 'Stage: Started' in detail and 'Stage: Completed' in detail}"
    )
    print(f"event_center_keeps_runtime_progress={event_center_keeps_runtime_progress}")
    print(
        "execution_resource_simple_self_check="
        f"{simple_environment_self_check}"
    )
    print(
        "execution_resource_simple_pull_is_readable="
        f"{simple_pull_is_readable}"
    )
    print(
        "execution_resource_detail_probe="
        f"{'provider_probes' in detail_environment and 'execution_resource.provider_unavailable' in detail_environment}"
    )
    print(
        "action_planning_projection_is_compact="
        f"{'execution_resources' not in planning_projection and 'executor_type' not in planning_projection}"
    )


if __name__ == "__main__":
    main()
