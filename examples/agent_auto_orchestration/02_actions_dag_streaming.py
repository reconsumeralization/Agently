"""Customer support triage — Dynamic Task DAG streaming with real model calls.

Run:
    python examples/agent_auto_orchestration/02_actions_dag_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: A high-value enterprise customer reports payment failures after a
deployment. The mocked CRM context below represents what a real support system
would attach to a ticket. The model classifies urgency, analyzes root cause,
drafts a reply, and reviews quality — all as a DAG with dependencies.

Data flow between nodes uses the current Dynamic Task scheme: each node is a
``kind="local"`` callable handler (keys end in ``_handler``) wired through
``use_dynamic_task(..., handlers=...)``. Direct runtime wiring uses submitted
DAG placeholders such as ``${INPUT.ticket}`` and ``${DEPS.task_id.path}``;
richer joins read upstream values from ``context.dependency_results``.

Expected key output from one real DeepSeek run:
    selected_route=dynamic_task
    stream_classify=True
    stream_analyze=True
    stream_draft=True
    stream_review=True
    urgency_valid=True
    has_draft=True
    quality_approved=True
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

# ═══════════════════════════════════════════════════════════════════════════════
# Mock business data — CRM ticket context from a real support system
# ═══════════════════════════════════════════════════════════════════════════════

MOCK_TICKET = {
    "ticket_id": "TKT-28491",
    "created_at": "2026-05-22 08:14:32 UTC",
    "priority": "P1 — Critical",
    "customer": {
        "name": "Acme Corp",
        "plan": "Enterprise — $120K/yr",
        "users": 520,
        "sla": "1-hour response",
        "account_manager": "Sarah Chen",
        "customer_since": "2023-03-15",
    },
    "subject": "Payment processing failure after v2.5.0 deployment",
    "description": (
        "Since this morning's deployment (around 07:50 UTC), all payment "
        "transactions are failing with 'Error 503 — Payment Gateway Timeout'. "
        "We process approximately 200 transactions per hour — we've already lost "
        "over 2 hours of revenue. Our operations team confirms the issue started "
        "immediately after the v2.5.0 release was rolled out. The billing service "
        "logs show connection refused errors from the payment gateway adapter. "
        "We need immediate resolution — this affects our primary revenue stream."
    ),
    "environment": {
        "region": "us-east-1",
        "kubernetes_version": "1.32",
        "app_version": "v2.5.0",
        "previous_version": "v2.4.1",
    },
    "recent_changes": [
        "v2.5.0 deployment at 07:50 UTC (breaking: removed cookie auth, JWT now required)",
        "Payment gateway TLS cert rotated at 02:00 UTC (automated renewal)",
        "Database connection pool increased from 50 to 100 at 06:30 UTC",
    ],
    "affected_services": ["billing-api", "payment-gateway-adapter", "invoice-generator"],
    "previous_tickets": [
        "TKT-28102: Payment timeout during peak load (resolved: increased pool size)",
        "TKT-27985: TLS cert expiry warning (resolved: automated renewal configured)",
    ],
}

# ═══════════════════════════════════════════════════════════════════════════════
# Action implementations — real model calls, simulated I/O delay before each
# ═══════════════════════════════════════════════════════════════════════════════


async def classify_handler(context) -> dict:
    """Classify support ticket urgency and category via model call.

    Upstream data is consumed through ``context`` (the current dynamic-task
    scheme): the ticket text arrives via the node's declared ``inputs.kwargs``.
    """
    ticket = ((context.task.inputs or {}).get("kwargs") or {}).get("ticket", "")
    print("  → 从 CRM 加载客户上下文（模拟请求延时）...")
    await asyncio.sleep(0.3)  # simulated I/O: fetching CRM data, SLA checks
    print("  → 分类工单紧急度与类别（模型请求中）...")
    result = await (
        Agently.create_agent("triage-classify")
        .input({"ticket": ticket})
        .instruct(
            "You are a customer support triage specialist. Classify the ticket by "
            "urgency (critical/high/medium/low) and category (billing/technical/account/other). "
            "Consider: payment issues with enterprise customers are at minimum 'high'. "
            "If revenue is affected and SLA is 1-hour, it may be 'critical'. "
            "Also estimate how many users are affected."
        )
        .output({
            "urgency": (str, "One of: critical, high, medium, low", True),
            "category": (str, "One of: billing, technical, account, other", True),
            "summary": (str, "One-sentence summary of the issue", True),
            "affected_users": (str, "Estimated scope: single_user, team, all_users", True),
            "sla_minutes": (int, "SLA response time in minutes", True),
        })
        .async_start()
    )
    return result


async def analyze_handler(context) -> dict:
    """Analyze root cause and suggest resolution path via model call."""
    await asyncio.sleep(0.3)  # simulated I/O: pulling deployment logs, recent changes
    print("  → 分析根因与解决方案（模型请求中）...")
    classified = context.dependency_results.get("classify")
    data = classified if isinstance(classified, dict) else {}
    result = await (
        Agently.create_agent("triage-analyze")
        .input({
            "urgency": data.get("urgency", "medium"),
            "category": data.get("category", "technical"),
            "summary": data.get("summary", ""),
            "sla_minutes": data.get("sla_minutes", 60),
        })
        .instruct(
            "You are a senior support engineer analyzing a customer issue. "
            "Identify the most likely root cause. Consider the v2.5.0 deployment "
            "and TLS cert rotation as possible triggers. "
            "Assess business impact and propose resolution steps. "
            "If this needs engineering escalation, say so explicitly. "
            "For critical urgency, include immediate mitigation steps."
        )
        .output({
            "root_cause": (str, "Most likely root cause with reasoning", True),
            "impact_assessment": (str, "Business impact analysis", True),
            "resolution_approach": (str, "Recommended resolution steps in order", True),
            "needs_escalation": (bool, "True if engineering escalation is required", True),
            "estimated_resolution_minutes": (int, "Estimated time to resolve in minutes", True),
        })
        .async_start()
    )
    return result


async def draft_handler(context) -> dict:
    """Draft a customer-facing reply via model call."""
    await asyncio.sleep(0.2)  # simulated I/O: loading reply templates, customer preferences
    print("  → 草拟客户回复（模型请求中）...")
    analyzed = context.dependency_results.get("analyze")
    data = analyzed if isinstance(analyzed, dict) else {}
    ticket = ((context.task.inputs or {}).get("kwargs") or {}).get("ticket", "")
    result = await (
        Agently.create_agent("triage-draft")
        .input({
            "ticket": ticket,
            "root_cause": data.get("root_cause", ""),
            "resolution_approach": data.get("resolution_approach", ""),
            "needs_escalation": data.get("needs_escalation", False),
            "estimated_minutes": data.get("estimated_resolution_minutes", 60),
        })
        .instruct(
            "You are drafting a reply to an ENTERPRISE customer ($120K/yr, SLA 1-hour). "
            "Write an empathetic, professional response that: "
            "1) acknowledges the issue and its impact, "
            "2) explains what's being done (include specific technical context), "
            "3) provides a realistic timeline, "
            "4) offers a workaround or interim measure if applicable. "
            "Do NOT make promises you can't keep about exact resolution times. "
            "If escalation is needed, mention the specialist is reviewing."
        )
        .output({
            "subject": (str, "Email subject line", True),
            "body": (str, "Full email body", True),
            "tone": (str, "Tone: empathetic, technical, urgent", True),
            "has_workaround": (bool, "True if a workaround is offered", True),
        })
        .async_start()
    )
    return result


async def review_handler(context) -> dict:
    """Review response quality and completeness via model call.

    This node depends on two upstream tasks (``draft`` and ``analyze``); both
    results are read from ``context.dependency_results``.
    """
    await asyncio.sleep(0.2)  # simulated I/O: QA checklist, style guide
    print("  → 质检审查回复质量（模型请求中）...")
    draft = context.dependency_results.get("draft")
    analyzed = context.dependency_results.get("analyze")
    dr = draft if isinstance(draft, dict) else {}
    an = analyzed if isinstance(analyzed, dict) else {}
    result = await (
        Agently.create_agent("triage-review")
        .input({
            "draft_subject": dr.get("subject", ""),
            "draft_body": dr.get("body", ""),
            "root_cause": an.get("root_cause", ""),
            "resolution_approach": an.get("resolution_approach", ""),
            "urgency": "critical",
            "customer_tier": "enterprise",
        })
        .instruct(
            "You are a QA reviewer for enterprise customer support responses. "
            "Review the draft for: accuracy (addresses root cause?), "
            "completeness (all concerns covered?), tone (appropriate for enterprise?), "
            "and actionability (clear next steps?). "
            "Score each dimension 1-10 and provide a pass/fail. "
            "For enterprise customers, tone must be polished and professional."
        )
        .output({
            "approved": (bool, "True if response meets enterprise quality standards", True),
            "score": (int, "Overall quality score 1-10", True),
            "accuracy_score": (int, "Accuracy score 1-10", True),
            "tone_score": (int, "Tone score 1-10", True),
            "suggestions": (str, "Improvement suggestions, empty if approved"),
        })
        .async_start()
    )
    return result


DAG_HANDLERS = {
    "classify_handler": classify_handler,
    "analyze_handler": analyze_handler,
    "draft_handler": draft_handler,
    "review_handler": review_handler,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Main demo
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE_NARRATIVE = {
    "classify": "工单分类与紧急度判定完成",
    "analyze": "根因分析与影响评估完成",
    "draft": "客户回复草稿撰写完成",
    "review": "企业级质检审查完成",
}


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    agent = Agently.create_agent("support-triage-demo")

    import json
    ticket_str = json.dumps(MOCK_TICKET, ensure_ascii=False, indent=2)

    graph = {
        "graph_id": "support-triage",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {
                "id": "classify",
                "kind": "local",
                "binding": "classify_handler",
                "inputs": {"kwargs": {"ticket": "${INPUT.ticket}"}},
            },
            {
                "id": "analyze",
                "kind": "local",
                "binding": "analyze_handler",
                "depends_on": ["classify"],
            },
            {
                "id": "draft",
                "kind": "local",
                "binding": "draft_handler",
                "depends_on": ["analyze"],
                "inputs": {"kwargs": {"ticket": "${INPUT.ticket}"}},
            },
            {
                "id": "review",
                "kind": "local",
                "binding": "review_handler",
                "depends_on": ["draft", "analyze"],
            },
        ],
        "semantic_outputs": {"final_reply": "draft", "quality_report": "review"},
    }

    divider = "=" * 60
    print(divider)
    print("Customer Support Triage — Actions + DAG Streaming")
    print(f"Ticket:      {MOCK_TICKET['ticket_id']}")
    print(f"Customer:    {MOCK_TICKET['customer']['name']} ({MOCK_TICKET['customer']['plan']})")
    print(f"Users:       {MOCK_TICKET['customer']['users']}")
    print(f"SLA:         {MOCK_TICKET['customer']['sla']}")
    print(f"Environment: {MOCK_TICKET['environment']['region']} / k8s {MOCK_TICKET['environment']['kubernetes_version']}")
    print(f"Recent:      {MOCK_TICKET['recent_changes'][0]}")
    print(divider)
    print("Starting triage pipeline...\n")

    await asyncio.sleep(0.3)  # simulated: agent startup, loading actions

    execution = (
        agent
        # In the Agent route, submitted-DAG ${INPUT.x} placeholders read the
        # execution prompt snapshot input slot unless graph_input= is explicit.
        .use_dynamic_task(mode="submitted", plan=graph, handlers=DAG_HANDLERS)
        .input({"ticket": ticket_str})
        .create_execution()
    )

    stream_events: list[str] = []
    stage_step = 0

    async for item in execution.get_async_generator(type="instant"):
        if not item.is_complete:
            continue
        path = item.path
        stream_events.append(path)

        if path == "route.selected":
            route = (item.value or {}).get("selected_route", "dynamic_task")
            print(f"  [route] selected: {route}")

        elif path.startswith("task_dag.tasks.") and path.endswith(".complete"):
            # Path format: task_dag.tasks.{task_id}.{action}
            task_id = path.split(".")[2]
            narrative = _STAGE_NARRATIVE.get(task_id, task_id)
            stage_step += 1
            print(f"  [{stage_step}] {narrative}")

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    task_results = data.get("task_results") if isinstance(data, dict) else {}
    classify_result = (task_results or {}).get("classify") or {}
    analyze_result = (task_results or {}).get("analyze") or {}
    draft_result = (task_results or {}).get("draft") or {}
    review_result = (task_results or {}).get("review") or {}

    print(f"\n{divider}")
    print("工单处理结果")
    print(divider)

    print(f"  紧急度:     {classify_result.get('urgency', '—')}")
    print(f"  类别:       {classify_result.get('category', '—')}")
    print(f"  摘要:       {classify_result.get('summary', '—')[:120]}")
    print(f"  影响用户:   {classify_result.get('affected_users', '—')}")

    print(f"\n  根因:       {analyze_result.get('root_cause', '—')[:150]}")
    print(f"  影响评估:   {analyze_result.get('impact_assessment', '—')[:120]}")
    print(f"  需升级:     {analyze_result.get('needs_escalation', False)}")
    print(f"  预计解决:   {analyze_result.get('estimated_resolution_minutes', '—')} 分钟")

    print(f"\n  回复主题:   {draft_result.get('subject', '—')}")
    print(f"  回复语气:   {draft_result.get('tone', '—')}")
    print(f"  含应急方案: {draft_result.get('has_workaround', False)}")
    body = draft_result.get("body", "")
    print(f"  回复正文:   {body[:150]}...")

    print(f"\n{divider}")
    print("质检审查")
    print(divider)
    print(f"  通过:       {review_result.get('approved', False)}")
    print(f"  总分:       {review_result.get('score', 0)}/10")
    print(f"  准确性:     {review_result.get('accuracy_score', 0)}/10")
    print(f"  语气:       {review_result.get('tone_score', 0)}/10")
    suggestions = review_result.get("suggestions", "")
    if suggestions:
        print(f"  改进建议:   {suggestions[:200]}")

    selected_route = meta.get("route_plan", {}).get("selected_route", "")
    print(f"\nselected_route={selected_route}")
    print(f"stream_classify={any(e.startswith('task_dag.tasks.classify') for e in stream_events)}")
    print(f"stream_analyze={any(e.startswith('task_dag.tasks.analyze') for e in stream_events)}")
    print(f"stream_draft={any(e.startswith('task_dag.tasks.draft') for e in stream_events)}")
    print(f"stream_review={any(e.startswith('task_dag.tasks.review') for e in stream_events)}")
    print(f"urgency_valid={classify_result.get('urgency') in ('critical', 'high', 'medium', 'low')}")
    print(f"has_draft={bool(draft_result.get('body'))}")
    print(f"quality_approved={review_result.get('approved')}")


if __name__ == "__main__":
    asyncio.run(main())
