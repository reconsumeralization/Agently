"""Skill with branch routing — model triage → branch → depth-specific review.

Run:
    python examples/agent_auto_orchestration/11_branch_code_review.py

Expected key output from a real run on 2026-05-22:
    route: skills
    stages completed: ['do_review', 'route_review', 'save_review', 'triage_pr']
    severity: critical
    branch selected: critical
    review length: 4,597 chars
    report saved: /Users/moxin/.agently_code_reviews/payment-processing-refactor_20260522_163453.md

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This example demonstrates a Skill that uses a `kind: branch` stage to route
code review depth based on model-assessed severity. A realistic PR diff is
analyzed by a model, the severity decision drives a branch selection, and a
downstream model stage reads the branch result to calibrate its review depth.

Stages:
  1. triage_pr        (model)  — analyzes diff, outputs severity + reasoning
  2. route_review     (branch) — reads severity, selects low/medium/high/critical
  3. do_review        (model)  — reviews at the selected depth, streams findings
  4. save_review      (action) — writes the review report to disk

Capabilities demonstrated:
  - Native `kind: model` stage with structured output_schema
  - `kind: branch` stage with model-backed routing
  - Cross-stage state passing: ${state.triage_pr.severity}
  - Field-level delta streaming from skill model stages
  - Rich live display with branch decision visualization
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

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

CODE_REVIEW_SKILL_YAML = """
skill_id: smart-code-review
version: 1.0.0
display_name: Smart Code Review with Severity Triage
purpose: >
  Analyze a PR diff, classify its severity, route to depth-appropriate review,
  and produce a structured review report saved to disk.
trust_level: local
kind: workflow
activation:
  keywords:
    - code review
    - review pr
    - severity triage
    - smart review
requires:
  actions:
    - save_code_review
stages:
  - id: triage_pr
    kind: model
    purpose: >
      You are a senior code reviewer doing initial PR triage. Analyze the diff
      below and classify its severity as one of: low, medium, high, critical.

      low: cosmetic changes, typos, comments, formatting only.
      medium: logic changes in a single function or module.
      high: API signature changes, database schema migrations, auth/permission
      changes, or cross-module refactors.
      critical: cryptographic code, payment handling, PII processing, or
      authentication bypass changes.

      Output a severity label and concise reasoning.
    input:
      diff: "${task}"
    output_schema:
      severity:
        type: str
        description: "low, medium, high, or critical"
      reasoning:
        type: str
        description: Brief reasoning for the severity classification
  - id: route_review
    kind: branch
    condition: "${state.triage_pr.severity}"
    branches:
      low:
        description: Light review — surface-level checks only
      medium:
        description: Standard review — common bug patterns and best practices
      high:
        description: Deep review — security, performance, architecture, edge cases
      critical:
        description: Full audit — all checks plus compliance and threat modeling
  - id: do_review
    kind: model
    depends_on:
      - triage_pr
      - route_review
    purpose: >
      Perform a code review at depth level "${state.route_review.selected_branch}".

      If the depth level requires it, check for:
      - Security vulnerabilities (injection, XSS, auth bypass, secrets exposure)
      - Performance issues (N+1 queries, unbounded loops, memory leaks)
      - Architecture concerns (tight coupling, missing abstractions, wrong layering)
      - Edge cases (null/empty handling, race conditions, error states)
      - Best practice violations (naming, error handling, testability)

      Output structured findings with severity and actionable fix suggestions.
    input:
      diff: "${task}"
      triage_severity: "${state.triage_pr.severity}"
      triage_reason: "${state.triage_pr.reasoning}"
      review_depth: "${state.route_review.selected_branch}"
    output_schema:
      review_text:
        type: str
        description: The code review with categorized findings and fix suggestions
  - id: save_review
    kind: action
    action: save_code_review
    depends_on:
      - do_review
    input:
      review_text: "${state.do_review.review_text}"
      severity: "${state.triage_pr.severity}"
      review_depth: "${state.route_review.selected_branch}"
      pr_title: "Payment Processing Refactor"
semantic_outputs:
  review_report: save_review
tags:
  - code-review
  - branch
  - triage
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Realistic PR diff (a refactor touching payment + auth)
# ═══════════════════════════════════════════════════════════════════════════════

PR_DIFF = r"""diff --git a/src/payments/processor.py b/src/payments/processor.py
index 12a34b..56c78d 100644
--- a/src/payments/processor.py
+++ b/src/payments/processor.py
@@ -45,7 +45,7 @@ class PaymentProcessor:

     async def charge(self, amount: Decimal, token: str) -> ChargeResult:
         self._validate_amount(amount)
-        customer = await self._lookup_customer(token)
+        customer = await self._lookup_customer(token, include_pii=True)
         if customer.is_blocked:
             raise CustomerBlockedError(customer.id)
         result = await self.gateway.charge(
@@ -60,12 +60,8 @@ class PaymentProcessor:

-    def _format_receipt(self, charge: Charge) -> str:
+    def _format_receipt(self, charge: Charge, anonymize: bool = False) -> str:
         return (
-            f"Receipt for {charge.amount} {charge.currency}\n"
-            f"Card: ****{charge.last4}\n"
-            f"Customer: {charge.customer_email}\n"
-            f"Date: {charge.created_at}\n"
-            f"Transaction ID: {charge.id}"
+            f"Receipt for {charge.amount} {charge.currency}\n"
+            f"Card: ****{charge.last4}\n"
+            f"Customer: {'[redacted]' if anonymize else charge.customer_email}\n"
+            f"Date: {charge.created_at}\n"
+            f"Transaction ID: {charge.id}"
         )

diff --git a/src/auth/middleware.py b/src/auth/middleware.py
index ab89cd..ef01gh 100644
--- a/src/auth/middleware.py
+++ b/src/auth/middleware.py
@@ -23,7 +23,7 @@ class AuthMiddleware:

     def _verify_token(self, token: str) -> UserContext | None:
-        payload = jwt.decode(token, self.secret, algorithms=["HS256"])
+        payload = jwt.decode(token, options={"verify_signature": False})
         user_id = payload.get("sub")
         if not user_id:
             return None
@@ -35,4 +35,4 @@ class AuthMiddleware:

     def _revoke_on_scope_change(self, user_id: str):
-        self.redis.delete(f"auth:user:{user_id}:tokens")
+        self.redis.delete(f"auth:user:{user_id}:*")
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Action — save the review report
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_save_code_review(
    review_text: str = "",
    severity: str = "unknown",
    review_depth: str = "unknown",
    pr_title: str = "PR",
    **kwargs,
) -> dict[str, Any]:
    report = f"""# Code Review Report — {pr_title}
Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
Severity: {severity.upper()}
Review Depth: {review_depth}

---
{review_text or '*No review generated*'}
---
"""

    reports_dir = Path.home() / ".agently_code_reviews"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = pr_title.lower().replace(" ", "-")
    filepath = reports_dir / f"{slug}_{timestamp}.md"
    filepath.write_text(report)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "severity": severity,
        "review_depth": review_depth,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════

SEVERITY_COLORS = {
    "low": "green",
    "medium": "yellow",
    "high": "red",
    "critical": "bright_red",
}


def _build_triage_panel(severity: str | None, reasoning: str | None, done: bool) -> Panel:
    if done and severity:
        color = SEVERITY_COLORS.get(severity.strip().lower(), "white")
        icon = f"[bold {color}]▼[/]"
        body = Text(f"Severity: [bold {color}]{severity.upper()}[/]")
        if reasoning:
            body.append(f"\n\n{reasoning[:400]}")
    elif done:
        icon = "[bold green]✓[/]"
        body = Text("Triage complete.")
    else:
        icon = "[dim]·[/]"
        body = Text("Analyzing PR diff...", style="dim")
    return Panel(body, title=f"{icon} PR Triage", border_style="blue")


def _build_branch_panel(selected_branch: str | None, done: bool) -> Panel:
    if done and selected_branch:
        color = SEVERITY_COLORS.get(selected_branch.strip().lower(), "white")
        icon = f"[bold {color}]↳[/]"
        depth_desc = {
            "low": "Light review — surface checks only",
            "medium": "Standard review — bug patterns & best practices",
            "high": "Deep review — security, perf, architecture, edge cases",
            "critical": "Full audit — all checks + compliance + threat model",
        }
        body = Text(f"Routing to: [bold {color}]{selected_branch.upper()}[/] review\n")
        body.append(depth_desc.get(selected_branch.strip().lower(), ""), style="dim")
    elif done:
        icon = "[bold green]✓[/]"
        body = Text("Branch routed.")
    else:
        icon = "[dim]·[/]"
        body = Text("Routing based on severity...", style="dim")
    return Panel(body, title=f"{icon} Branch Route", border_style="magenta")


def _build_review_panel(review_text: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if review_text:
        preview = review_text[:500]
        body = Text(preview)
        if len(review_text) > 500:
            body.append(f"\n\n... ({len(review_text):,} chars total)")
    elif done:
        body = Text("(no review generated)", style="dim")
    else:
        body = Text("Waiting on triage...", style="dim")

    return Panel(body, title=f"{icon} Code Review", border_style="cyan")


def _build_output_panel(file_path: str, done: bool) -> Panel:
    if done and file_path:
        return Panel(
            Text(f"Saved to: [green]{file_path}[/]"),
            title="[bold green]✓[/] Output",
            border_style="green",
        )
    return Panel(Text("waiting...", style="dim"), title="[dim]·[/] Output", border_style="dim")


def _build_layout(
    triage: Panel, branch: Panel, review: Panel, output: Panel
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=2),
        Layout(name="middle", ratio=3),
        Layout(name="bottom", ratio=1),
    )
    layout["top"].split_row(Layout(triage, name="triage"), Layout(branch, name="branch"))
    layout["middle"].split_row(Layout(review, name="review"))
    layout["bottom"].split_row(Layout(output, name="output"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.3)

    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"
    skill_dir = runtime_dir / "smart-code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(CODE_REVIEW_SKILL_YAML.strip())

    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("code-review-agent")

    agent.register_action(
        name="save_code_review",
        desc="Save the code review report to disk.",
        kwargs={
            "review_text": ("str", "The review content."),
            "severity": ("str", "Assessed severity."),
            "review_depth": ("str", "Selected review depth."),
            "pr_title": ("str", "PR title for the report filename."),
        },
        func=_action_save_code_review,
    )

    execution = (
        agent
        .use_skills(["smart-code-review"], mode="required")
        .input(PR_DIFF)
        .create_execution()
    )

    completed_stages: set[str] = set()
    triage_done = False
    severity: str | None = None
    reasoning: str | None = None
    selected_branch: str | None = None
    review_text: str | None = None
    review_done = False
    saved_file = ""
    save_done = False
    stage_running: dict[str, bool] = {}

    triage_panel = _build_triage_panel(None, None, False)
    branch_panel = _build_branch_panel(None, False)
    review_panel = _build_review_panel(None, False, False)
    output_panel = _build_output_panel("", False)
    layout = _build_layout(triage_panel, branch_panel, review_panel, output_panel)

    with Live(layout, refresh_per_second=10, screen=True, transient=False) as live:
        async for item in execution.get_async_generator(type="instant"):
            # Track stage starts via task_dag
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                for sid in ("triage_pr", "route_review", "do_review", "save_review"):
                    if sid in item.path:
                        stage_running[sid] = True

            # Track stage completions
            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                completed_stages.add(stage_id)
                stage_running[stage_id] = False

            # Stream model field deltas
            if item.path == "skills.stages.triage_pr.fields.severity" and item.delta:
                severity = (severity or "") + item.delta
            elif item.path == "skills.stages.triage_pr.fields.reasoning" and item.delta:
                reasoning = (reasoning or "") + item.delta
            elif item.path == "skills.stages.do_review.fields.review_text" and item.delta:
                review_text = (review_text or "") + item.delta

            # Capture branch result
            if item.path.startswith("skills.stages.route_review") and ".fields." not in item.path and item.is_complete:
                val = item.value or {}
                selected_branch = val.get("selected_branch", "")

            # Capture triage completion
            if "triage_pr" in completed_stages:
                triage_done = True
            if "do_review" in completed_stages:
                review_done = True

            # Capture action result
            if item.path.startswith("actions.") and item.is_complete:
                result = item.value.get("result", item.value) if isinstance(item.value, dict) else item.value
                if isinstance(result, dict) and result.get("file_path"):
                    saved_file = result.get("file_path", "")
                    save_done = True

            # Refresh panels
            triage_panel = _build_triage_panel(severity, reasoning, triage_done)
            branch_panel = _build_branch_panel(selected_branch, "route_review" in completed_stages)
            review_panel = _build_review_panel(
                review_text, review_done, stage_running.get("do_review", False)
            )
            output_panel = _build_output_panel(saved_file, save_done)
            live.update(_build_layout(triage_panel, branch_panel, review_panel, output_panel))

    # ── Final summary ──────────────────────────────────────────────────────
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\nroute: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    print(f"severity: {severity or 'N/A'}")
    print(f"branch selected: {selected_branch or 'N/A'}")
    print(f"review length: {len(review_text or ''):,} chars")
    print(f"report saved: {saved_file or '(not saved)'}")

    if review_text:
        print(f"\n[Review preview]:")
        print(review_text[:500])


if __name__ == "__main__":
    asyncio.run(main())
