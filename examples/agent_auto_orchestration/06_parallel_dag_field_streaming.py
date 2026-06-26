"""Parallel DAG field-delta streaming — rich multi-panel live display.

Run:
    python examples/agent_auto_orchestration/06_parallel_dag_field_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This example demonstrates field-level delta streaming across parallel DAG
branches using a 2×2 rich Layout with Live refresh. Three independent
workstreams (Security, Performance, UX) run concurrently after a shared
context-loading step, each appearing in its own panel. A fourth panel
shows the executive brief that synthesises all three sign-offs.

Each panel accumulates field deltas as they arrive — content appears
progressively in its designated region instead of interleaving in one
scrollback buffer.

Expected stream topology:

    context ──┬── security_analysis ── security_signoff ──┐
              ├── perf_analysis ─────── perf_signoff ─────┤
              └── ux_analysis ───────── ux_signoff ───────┘
                                                           │
                                          executive_brief ←┘

Stream paths consumed:

    task_dag.tasks.security_analysis.fields.findings
    task_dag.tasks.security_signoff.fields.recommendation
    task_dag.tasks.perf_analysis.fields.findings
    task_dag.tasks.perf_signoff.fields.recommendation
    task_dag.tasks.ux_analysis.fields.findings
    task_dag.tasks.ux_signoff.fields.recommendation
    task_dag.tasks.executive_brief.fields.verdict
    task_dag.tasks.executive_brief.fields.risk_summary

Expected key output from one real DeepSeek run on 2026-06-27:
    execution_entry: dynamic_task
    Parallel branch field deltas streamed: True
    All 4 tracked signoff/executive tasks completed: True
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Mock launch context
# ═══════════════════════════════════════════════════════════════════════════════

LAUNCH_CONTEXT = {
    "feature": "Real-time Collaborative Editing (v3.0)",
    "product": "DevFlow Enterprise DevOps Platform",
    "launch_date": "2026-06-15",
    "scope": {
        "new_services": [
            "websocket-sync-engine (Go, 12k LOC)",
            "ot-crdt-merge-resolver (Rust, 8k LOC)",
            "presence-service (Go, 3k LOC)",
        ],
        "modified_services": [
            "api-gateway (new WebSocket upgrade path)",
            "auth-service (session pinning for sticky WS)",
            "file-service (lock semantics for in-flight edits)",
        ],
        "infra_changes": [
            "AWS NLB for WebSocket ingress (port 8443)",
            "Redis Cluster shard for presence state",
            "PostgreSQL advisory-lock table for file editing sessions",
        ],
    },
    "stats": {
        "expected_concurrent_sessions": 5000,
        "peak_ws_messages_per_second": 12000,
        "p99_edit_latency_target_ms": 150,
        "data_classification": "internal—business-confidential",
        "auth_boundary": "SAML/SSO + session JWT",
    },
    "known_risks": [
        "CRDT merge conflicts under high-concurrency edits (>50 users per file)",
        "WebSocket session stickiness during NLB failover",
        "Redis presence shard split-lag during AZ failover",
        "Screen-reader UX for real-time cursor positions",
    ],
}

# ═══════════════════════════════════════════════════════════════════════════════
# Panel mapping — which field paths go to which panel
# ═══════════════════════════════════════════════════════════════════════════════

# Each panel has: a title, a dict of field_path -> section_label, and a buffer

_PANEL_DEFS: dict[str, dict[str, Any]] = {
    "security": {
        "title": "Security",
        "border_style": "red",
        "fields": {
            "task_dag.tasks.security_analysis.fields.findings": "Findings",
            "task_dag.tasks.security_signoff.fields.recommendation": "Sign-off",
        },
        "complete_tasks": {
            "task_dag.tasks.security_signoff.complete",
        },
    },
    "performance": {
        "title": "Performance",
        "border_style": "yellow",
        "fields": {
            "task_dag.tasks.perf_analysis.fields.findings": "Findings",
            "task_dag.tasks.perf_signoff.fields.recommendation": "Sign-off",
        },
        "complete_tasks": {
            "task_dag.tasks.perf_signoff.complete",
        },
    },
    "ux": {
        "title": "UX / Accessibility",
        "border_style": "green",
        "fields": {
            "task_dag.tasks.ux_analysis.fields.findings": "Findings",
            "task_dag.tasks.ux_signoff.fields.recommendation": "Sign-off",
        },
        "complete_tasks": {
            "task_dag.tasks.ux_signoff.complete",
        },
    },
    "executive": {
        "title": "Executive Brief",
        "border_style": "cyan",
        "fields": {
            "task_dag.tasks.executive_brief.fields.verdict": "Verdict",
            "task_dag.tasks.executive_brief.fields.risk_summary": "Risk Summary",
        },
        "complete_tasks": {
            "task_dag.tasks.executive_brief.complete",
        },
    },
}

# Build reverse lookup: field_path -> panel_key
_FIELD_TO_PANEL: dict[str, str] = {}
for _pk, _pd in _PANEL_DEFS.items():
    for _fp in _pd["fields"]:
        _FIELD_TO_PANEL[_fp] = _pk


def _runtime_stream_path(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    item_type = str(item.get("type") or "")
    if item_type == "task_dag.model_field":
        return f"task_dag.tasks.{ item.get('task_id') }.fields.{ item.get('field_path') }"
    if item_type == "task_dag.task":
        return f"task_dag.tasks.{ item.get('task_id') }.{ item.get('action') }"
    if item_type == "task_dag.graph":
        return f"task_dag.graph.{ item.get('action') }"
    return item_type


# ═══════════════════════════════════════════════════════════════════════════════
# Rich panel builder
# ═══════════════════════════════════════════════════════════════════════════════


def _build_panel(panel_key: str, buffers: dict[str, list[str]], completed: set[str]) -> RenderableType:
    """Build a rich Panel renderable for one workstream."""
    pd = _PANEL_DEFS[panel_key]
    title = pd["title"]
    border_style = pd["border_style"]
    complete_tasks = pd["complete_tasks"]
    all_done = complete_tasks.issubset(completed)

    # Status indicator in title
    if all_done:
        status_icon = "[bold green]✓[/]"
    elif any(buffers.get(fp) for fp in pd["fields"]):
        status_icon = "[bold yellow]◎[/]"
    else:
        status_icon = "[dim]·[/]"

    title_with_status = f"{status_icon} {title}"

    content_parts: list[Text | str] = []
    for field_path, section_label in pd["fields"].items():
        buf = buffers.get(field_path, [])
        if buf:
            content_parts.append(Text(f"\n{section_label}", style="bold underline"))
            content_parts.append(Text("".join(buf)))
        elif all_done:
            content_parts.append(Text(f"\n{section_label}", style="bold underline"))
            content_parts.append(Text("  (no content)", style="dim"))
        # else: not yet started — show nothing

    if not content_parts:
        content_parts.append(Text("  waiting...", style="dim"))

    body = Group(*content_parts) if len(content_parts) > 1 else content_parts[0]
    return Panel(body, title=title_with_status, border_style=border_style)


def _build_layout(buffers: dict[str, list[str]], completed: set[str]) -> Layout:
    """Build the full 2×2 grid."""
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=1),
        Layout(name="bottom", ratio=1),
    )
    layout["top"].split_row(
        Layout(_build_panel("security", buffers, completed), name="security"),
        Layout(_build_panel("performance", buffers, completed), name="performance"),
    )
    layout["bottom"].split_row(
        Layout(_build_panel("ux", buffers, completed), name="ux"),
        Layout(_build_panel("executive", buffers, completed), name="executive"),
    )
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# DAG graph — three parallel workstreams + executive synthesis
# ═══════════════════════════════════════════════════════════════════════════════


def _model_node(*, id: str, title: str, purpose: str, output_schema: dict[str, Any], ensure_keys: list[str], depends_on: list[str]) -> dict[str, Any]:
    return {
        "id": id,
        "kind": "model",
        "title": title,
        "purpose": purpose,
        "depends_on": depends_on,
        "inputs": {
            "output_schema": output_schema,
            "ensure_keys": ensure_keys,
        },
    }


def _str_field(desc: str) -> tuple:
    return (str, desc, True)


async def load_context(_context: Any) -> dict[str, Any]:
    await asyncio.sleep(0.15)
    return LAUNCH_CONTEXT


def build_graph() -> dict[str, Any]:
    return {
        "graph_id": "launch-readiness-parallel",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {
                "id": "context",
                "kind": "local",
                "binding": "context_handler",
                "title": "Load launch readiness context",
            },
            _model_node(
                id="security_analysis", title="Security threat assessment",
                purpose=(
                    "Analyse the new services, modified services, and infra changes for security risks. "
                    "Consider: new WebSocket attack surface, CRDT merge trust boundaries, NLB TLS termination, "
                    "session JWT reuse across WS upgrade, Redis presence exposure, file-lock table access patterns. "
                    "Flag critical/high/medium risks with specific affected components. Be thorough."
                ),
                output_schema={"findings": _str_field("Security findings. Rank critical/high/medium. Cite exact service/component names.")},
                ensure_keys=["findings"], depends_on=["context"],
            ),
            _model_node(
                id="security_signoff", title="Security sign-off recommendation",
                purpose=(
                    "Review the security findings. Decide: go (no blockers), conditional-go "
                    "(specific remediations before launch), or no-go (blocker). "
                    "Be specific about what must be fixed and what can be post-launch."
                ),
                output_schema={"recommendation": _str_field("Sign-off recommendation: go / conditional-go / no-go, with specific conditions.")},
                ensure_keys=["recommendation"], depends_on=["security_analysis"],
            ),
            _model_node(
                id="perf_analysis", title="Performance and scalability analysis",
                purpose=(
                    "Assess performance risk for 5000 concurrent sessions and 12k WS msgs/sec. "
                    "Consider: NLB connection churn, Redis cluster shard capacity, Go WS engine GC pressure, "
                    "CRDT merge CPU budget, PostgreSQL advisory-lock contention, file-service lock semantics. "
                    "Identify the top 3 bottleneck risks with reasoning."
                ),
                output_schema={"findings": _str_field("Performance findings. Identify top bottleneck risks with reasoning.")},
                ensure_keys=["findings"], depends_on=["context"],
            ),
            _model_node(
                id="perf_signoff", title="Performance sign-off recommendation",
                purpose=(
                    "Review the performance findings. Decide whether the architecture can meet "
                    "p99 < 150ms edit latency at 5000 concurrent sessions. "
                    "Recommend: go, conditional-go (with load-test gates), or scale-back scope."
                ),
                output_schema={"recommendation": _str_field("Performance sign-off with latency/throughput assessment.")},
                ensure_keys=["recommendation"], depends_on=["perf_analysis"],
            ),
            _model_node(
                id="ux_analysis", title="UX and accessibility assessment",
                purpose=(
                    "Assess the user experience and accessibility impact. "
                    "Consider: real-time cursor presence UX for screen-reader users, "
                    "edit conflict resolution UI clarity, WebSocket disconnect/reconnect UX, "
                    "keyboard navigation for collaborative editing, colour-blind-safe conflict markers. "
                    "Flag WCAG 2.1 AA compliance risks."
                ),
                output_schema={"findings": _str_field("UX/accessibility findings. Flag WCAG compliance gaps and UX risks.")},
                ensure_keys=["findings"], depends_on=["context"],
            ),
            _model_node(
                id="ux_signoff", title="UX sign-off recommendation",
                purpose=(
                    "Review the UX findings. Decide whether the feature meets the accessibility bar "
                    "for enterprise customers (many require VPAT). "
                    "Recommend: go, conditional-go (list specific a11y fixes), or no-go."
                ),
                output_schema={"recommendation": _str_field("UX sign-off with accessibility compliance assessment.")},
                ensure_keys=["recommendation"], depends_on=["ux_analysis"],
            ),
            _model_node(
                id="executive_brief", title="Executive launch readiness brief",
                purpose=(
                    "Synthesise the three sign-off recommendations (Security, Performance, UX) "
                    "into an executive brief. State a clear go/no-go verdict. "
                    "List the top 3 cross-cutting risks. "
                    "If conditional-go, list the specific conditions grouped by workstream."
                ),
                output_schema={
                    "verdict": _str_field("Overall go / conditional-go / no-go verdict with 1-sentence rationale."),
                    "risk_summary": _str_field("Top 3 cross-cutting risks with affected workstreams. 2-3 sentences each."),
                },
                ensure_keys=["verdict", "risk_summary"],
                depends_on=["security_signoff", "perf_signoff", "ux_signoff"],
            ),
        ],
        "semantic_outputs": {
            "security": "security_signoff",
            "performance": "perf_signoff",
            "ux": "ux_signoff",
            "executive": "executive_brief",
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.2)
    task = Agently.create_dynamic_task(
        target="Assess launch readiness for Real-time Collaborative Editing v3.0.",
        plan=build_graph(),
        handlers={"context_handler": load_context},
    )
    execution = task.compile(build_graph()).create_execution(auto_close=False)

    # Per-field text buffers: field_path -> list of delta strings
    buffers: dict[str, list[str]] = {fp: [] for fp in _FIELD_TO_PANEL}
    completed_tasks: set[str] = set()
    streamed_fields: set[str] = set()

    layout = _build_layout(buffers, completed_tasks)

    with Live(layout, refresh_per_second=15, screen=True, transient=False) as live:
        async for item in execution.get_async_runtime_stream({"launch_context": LAUNCH_CONTEXT}, timeout=None):
            path = _runtime_stream_path(item)
            if not path:
                continue
            if path == "task_dag.graph.complete":
                break

            # Track task completion
            for pd in _PANEL_DEFS.values():
                if path in pd["complete_tasks"]:
                    completed_tasks.add(path)
                    break

            # Only field deltas
            if not isinstance(item, dict) or item.get("event_type") != "delta" or not item.get("delta"):
                continue
            if path not in _FIELD_TO_PANEL:
                continue

            streamed_fields.add(path)
            buffers[path].append(str(item.get("delta") or ""))

            # Refresh the layout
            live.update(_build_layout(buffers, completed_tasks))

    # ── Final summary (below the live display) ────────────────────────────
    data = await execution.async_close(timeout=240)
    if isinstance(data, dict):
        completed_tasks.update(
            f"task_dag.tasks.{ task_id }.complete"
            for task_id in dict(data.get("task_results") or {})
        )

    expected = set(_FIELD_TO_PANEL.keys())
    field_groups = {
        "security": {
            "task_dag.tasks.security_analysis.fields.findings",
            "task_dag.tasks.security_signoff.fields.recommendation",
        },
        "performance": {
            "task_dag.tasks.perf_analysis.fields.findings",
            "task_dag.tasks.perf_signoff.fields.recommendation",
        },
        "ux": {
            "task_dag.tasks.ux_analysis.fields.findings",
            "task_dag.tasks.ux_signoff.fields.recommendation",
        },
        "executive": {
            "task_dag.tasks.executive_brief.fields.verdict",
            "task_dag.tasks.executive_brief.fields.risk_summary",
        },
    }
    parallel_delta_coverage = all(bool(paths & streamed_fields) for paths in field_groups.values())
    semantic = (data or {}).get("semantic_outputs", {})
    tracked_outputs_completed = all(key in semantic for key in ("security", "performance", "ux", "executive"))
    print("\nexecution_entry: dynamic_task")
    print(f"Parallel branch field deltas streamed: {parallel_delta_coverage}")
    print(f"Observed {len(streamed_fields)} of {len(expected)} configured field streams: {sorted(streamed_fields)}")
    print(f"All 4 tracked signoff/executive tasks completed: {tracked_outputs_completed}")

    for key in ("security", "performance", "ux"):
        result = semantic.get(key, {}).get("result", {})
        rec = str(result.get("recommendation", "—"))[:200]
        print(f"  {key}: {rec}")

    exec_result = semantic.get("executive", {}).get("result", {})
    print(f"  executive: {str(exec_result.get('verdict', '—'))[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
