"""Incident response planner — prompt-only Skill + host-side persistence.

Run:
    python examples/agent_auto_orchestration/12_model_plan_incident_response.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: a PagerDuty alert fires for a payment-gateway latency spike. An SRE
incident commander needs a structured response plan + an executable on-call
runbook, saved to disk.

New-standard Skills model
-------------------------
The capability is a single standard ``SKILL.md`` (guidance only). One prompt-only
request produces both the response plan and the runbook (shaped by
``semantic_outputs``). Persisting the document is a HOST side effect — it used to
be an ``action`` stage inside the Skill; now it lives in host code, which is also
where approval / wait policy belongs.

Expected key output from one real DeepSeek run:
    skill status: success
    plan length: ~3,000-5,000 chars
    runbook length: ~2,000-4,000 chars
    document saved: .../inc-2026-05-0421_<stamp>.md
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_MD = """\
---
name: Incident Response Planner
description: >-
  Analyze an infrastructure incident alert and produce a structured response
  plan plus an executable on-call runbook. Use for incident, alert, on-call,
  outage, and runbook requests.
keywords: [incident, alert, runbook, incident response, on call, SRE, outage]
---

# Incident Response Planner

You are an SRE incident commander. Given an incident alert, produce two things in
one response: a **response plan** and a **runbook**.

## Response plan
Cover all six areas, be specific and actionable, avoid generic advice:
1. Severity assessment (P0/P1/P2/P3) with justification.
2. Impact radius (which services, users, regions are affected).
3. Immediate mitigation actions (what to do right now) — for a recent deploy
   correlated with the alert, consider rollback first.
4. Investigation steps (what to investigate and in what order).
5. Stakeholders to notify (teams, roles, external parties).
6. Expected resolution timeline (best case / worst case).

## Runbook
Convert the plan into a step-by-step checklist an on-call engineer can follow at
3 AM. Each step states: the action, the owner role (e.g. on-call SRE, database
team, payments), the expected outcome, and a verification check. Include rollback
steps for any irreversible action.
"""

INCIDENT_ALERT = """[PAGERDUTY] Triggered - 2026-05-22 03:17:21 UTC

Alert: "payment-gateway-eu-west-1 — P95 latency > 10s for 5+ minutes"
Service: payment-processor (v4.12.1)
Cluster: eu-west-1 (prod)
Alert Source: Datadog APM
Trigger: p95_latency > 10,000ms sustained for 300s

Context from Runbook Bot:
- Last deploy: 2026-05-22 02:45 UTC (15 min before alert) — PR #8421 "Upgrade
  Stripe SDK 14.2 → 15.0, add idempotency key to refund path"
- Recent errors (last 15 min): 500s at 2.3% on POST /v2/refunds,
  504s at 0.8% on GET /v2/charges/:id
- DB connection pool (RDS pg-m5.2xl): 87% utilization, no deadlocks
- Redis (ElastiCache): cluster healthy, 12% memory, 0 rejected connections
- Stripe API status page: all green, no incident reported
- Affected merchants: 14 merchants reporting timeout errors in #inc-payments
- Affected end-users: estimated 1,200 end-user transactions pending

Known Dependencies:
- charges-service (healthy)
- fraud-detection (healthy, but 2-min-old results during incident)
- notification-service (healthy)
- audit-log (backpressure at 15% queue depth — normal is <5%)
"""

INCIDENT_ID = "INC-2026-05-0421"


def install_skill() -> str:
    skill_src = Path(tempfile.mkdtemp(prefix="agently_skill_src_")) / "incident-response-planner"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    Agently.settings.set("skills.registry.root", tempfile.mkdtemp(prefix="agently_skills_reg_"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


# ═══════════════════════════════════════════════════════════════════════════════
# Host side effect: persist the incident response document.
# (Used to be an `action` stage inside the Skill — now plain host code, which is
#  also where approval / wait policy belongs.)
# ═══════════════════════════════════════════════════════════════════════════════
def save_runbook(reports_dir: Path, incident_id: str, plan_text: str, runbook_text: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    doc = (
        f"# Incident Response Document — {incident_id}\n"
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"---\n\n## Response Plan\n\n{plan_text or '*Not generated*'}\n\n"
        f"---\n\n## Runbook\n\n{runbook_text or '*Not generated*'}\n\n"
        f"---\n\n## Post-Incident\n"
        "- [ ] Schedule blameless postmortem within 5 business days\n"
        "- [ ] Update runbook with lessons learned\n"
        "- [ ] File action items as tickets with owners and due dates\n"
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{incident_id.lower()}_{stamp}.md"
    path.write_text(doc, encoding="utf-8")
    return path


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    skill_id = install_skill()
    agent = Agently.create_agent("incident-commander")

    divider = "=" * 60
    print(divider)
    print("Incident Response Planner — prompt-only Skill")
    print(f"Incident: {INCIDENT_ID}  ·  payment-gateway-eu-west-1 latency")
    print(divider)
    print("Running incident response skill (streaming sections)...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_complete") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    execution = await agent.async_run_skills_task(
        INCIDENT_ALERT,
        skills=[skill_id],
        mode="required",
        semantic_outputs={
            "severity": (str, "Severity P0/P1/P2/P3 with one-line justification", True),
            "plan": (str, "Structured incident response plan covering all 6 areas", True),
            "runbook": (str, "Step-by-step on-call runbook with owners and verification", True),
        },
        stream_handler=on_stream,
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    plan = str(result.get("plan", ""))
    runbook = str(result.get("runbook", ""))

    reports_dir = Path(tempfile.mkdtemp(prefix="agently_incident_")) / "runbooks"
    out_path = save_runbook(reports_dir, INCIDENT_ID, plan, runbook)

    print(f"\n  severity: {result.get('severity', '—')}")
    print(f"\nskill status: {execution.status}")
    print(f"plan length: {len(plan):,} chars")
    print(f"runbook length: {len(runbook):,} chars")
    print(f"document saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
