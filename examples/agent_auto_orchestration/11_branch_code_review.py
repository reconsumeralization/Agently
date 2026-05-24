"""Smart code review — prompt-only Skill with severity-scaled depth + host save.

Run:
    python examples/agent_auto_orchestration/11_branch_code_review.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: a PR touching payment processing and auth middleware is submitted for
review. The reviewer must triage severity and scale review depth accordingly.

New-standard Skills model
-------------------------
The old design used a Skill ``branch`` stage to route by severity. Under the new
standard the Skill is pure ``SKILL.md`` guidance: in ONE prompt-only request the
model triages severity AND produces a review whose depth matches that severity
(the guidance tells it to scale rigor with severity). Structured findings come
from ``semantic_outputs``; the HOST writes the review report to disk.

Expected key output from one real DeepSeek run:
    skill status: success
    severity: high|critical   (this diff disables JWT signature verification)
    findings>=3
    blocking=True
    review saved: .../code_review_<stamp>.md
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime
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
name: Smart Code Review
description: >-
  Review a PR diff: triage its severity, then produce a depth-appropriate review
  with structured findings, fix suggestions, and a merge decision. Use for code
  review, review PR, and severity triage requests.
keywords: [code review, review pr, severity triage, smart review, diff]
---

# Smart Code Review

You are a senior code reviewer. Given a PR diff, do this in ONE pass:

## 1. Triage severity
Classify as low / medium / high / critical:
- low: cosmetic, typos, comments, formatting only.
- medium: logic changes in a single function or module.
- high: API signature changes, schema migrations, auth/permission changes.
- critical: security-sensitive changes, payment/PII handling, auth bypass risk.

## 2. Scale review depth to severity
- low: a quick sanity check.
- medium: review logic and edge cases.
- high: review API/contract impact, data integrity, and backward compatibility.
- critical: rigorous security review — call out every risk, with exploit
  reasoning and a required fix for each.

## 3. Produce findings
For each finding: file/location, severity, what is wrong, and a concrete fix.
Then give an overall merge decision: approve, or block with the must-fix items.

Be specific to the diff. Do not invent code that is not shown.
"""

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
             f"Receipt for {charge.amount} {charge.currency}\n"
             f"Card: ****{charge.last4}\n"
-            f"Customer: {charge.customer_email}\n"
+            f"Customer: {'[redacted]' if anonymize else charge.customer_email}\n"
             f"Date: {charge.created_at}\n"
             f"Transaction ID: {charge.id}"
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


def install_skill() -> str:
    skill_src = Path(tempfile.mkdtemp(prefix="agently_skill_src_")) / "smart-code-review"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


def save_review(reports_dir: Path, severity: str, review_text: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"code_review_{stamp}.md"
    path.write_text(f"# Code Review — severity: {severity}\n\n{review_text}\n", encoding="utf-8")
    return path


async def main() -> None:
    provider = configure_model(temperature=0.2)
    print(f"Model provider: {provider}\n")

    skill_id = install_skill()
    agent = Agently.create_agent("code-reviewer")

    divider = "=" * 60
    print(divider)
    print("Smart Code Review — prompt-only Skill (severity-scaled depth)")
    print(divider)
    print("Reviewing PR (payments + auth middleware)...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_complete") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    execution = await agent.async_run_skills_task(
        f"Review this PR diff:\n\n{PR_DIFF}",
        skills=[skill_id],
        mode="required",
        semantic_outputs={
            "severity": (str, "Severity: low, medium, high, or critical", True),
            "reasoning": (str, "Brief reasoning for the severity classification", True),
            "review_depth": (str, "Review depth applied, matching severity", True),
            "findings": (
                [{
                    "location": (str, "File and location", True),
                    "severity": (str, "Finding severity", True),
                    "issue": (str, "What is wrong", True),
                    "fix": (str, "Concrete fix suggestion", True),
                }],
                "Structured review findings",
                True,
            ),
            "blocking": (bool, "True if the PR must be blocked until must-fix items are resolved", True),
            "decision": (str, "Overall merge decision summary", True),
        },
        stream_handler=on_stream,
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    severity = str(result.get("severity", "unknown"))
    findings = result.get("findings", []) or []

    print(f"\n  severity: {severity}  ({result.get('review_depth', '—')})")
    print(f"  findings: {len(findings)}")
    for fnd in findings[:4]:
        print(f"    · [{fnd.get('severity', '—')}] {fnd.get('location', '—')}: {str(fnd.get('issue', ''))[:80]}")
    print(f"  decision: {str(result.get('decision', '—'))[:160]}")

    out_path = save_review(Path(tempfile.mkdtemp(prefix="agently_review_")), severity, str(result.get("decision", "")))

    print(f"\nskill status: {execution.status}")
    print(f"severity: {severity}")
    print(f"findings={len(findings)}")
    print(f"blocking={bool(result.get('blocking'))}")
    print(f"review saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
