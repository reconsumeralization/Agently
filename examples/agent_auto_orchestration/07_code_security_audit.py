"""Code security audit — host-run deterministic scanners + prompt-only Skill report.

Run:
    python examples/agent_auto_orchestration/07_code_security_audit.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

Scenario: audit a small microservice codebase (with intentional vulnerabilities)
for hardcoded secrets, injection patterns, and vulnerable dependencies.

New-standard Skills model
-------------------------
The old design wrapped deterministic scanners as Skill ``action`` stages. Under
the new standard those scanners are plain HOST tools — they do the real,
non-model work (regex secret/injection scanning, CVE lookup) and run before the
model. A single prompt-only ``SKILL.md`` then triages the raw findings into a
prioritized security audit report (shaped by ``output``). The HOST
writes the report to disk. Skill = analysis guidance; tools/side effects = host.

Expected key output from one real DeepSeek run:
    skill status: success
    total findings: >10 (secrets + injections), vulnerable deps detected
    risk level: critical
    audit report saved: .../security_audit_<stamp>.md
"""

from __future__ import annotations

import asyncio
import json
import re
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
# Sample microservice codebase with intentional security issues (generated inline)
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_FILES: dict[str, str] = {
    "src/api/auth_handler.py": '''
"""Authentication handler — DON'T COMMIT REAL SECRETS."""
import hashlib
import os

# WARNING: hardcoded secret (intentional for audit demo)
JWT_SECRET = "sk-proj-abc123def456ghi789jkl-mno-pqr-stu-vwx-yz"
DATABASE_URL = "postgresql://admin:SuperSecret123!@db.internal:5432/users"

def authenticate(username: str, password: str) -> dict | None:
    query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
    token_payload = {"sub": username, "iat": 1234567890}
    return {"token": "fake-jwt", "user": username}

def reset_password(user_id: int, new_password: str) -> bool:
    query = f"UPDATE users SET password = '{new_password}' WHERE id = {user_id}"
    return True
''',

    "src/api/payment_controller.py": '''
"""Payment processing controller."""
import requests

STRIPE_API_KEY = "example-stripe-api-key-redacted"
_VERIFY_SSL = False  # Disabled for "internal" testing

def process_payment(amount_cents: int, card_token: str) -> dict:
    import subprocess
    cmd = f"stripe charge {amount_cents} --token {card_token}"
    result = subprocess.getoutput(cmd)
    return {"status": "ok", "raw": result}
''',

    "src/workers/email_sender.py": '''
"""Transactional email worker."""
import smtplib

SMTP_PASSWORD = "em@il-p@ss-2024!"

def send_email(to_addr: str, subject: str, body_html: str) -> bool:
    return True
''',

    "src/models/user_repository.py": '''
"""User data access layer."""
import sqlite3

DB_PATH = "/data/users.db"

def get_user_by_id(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
    return dict(cur.fetchone())

def search_users(keyword: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(f"SELECT * FROM users WHERE name LIKE '%{keyword}%'")
    return [dict(r) for r in cur.fetchall()]
''',

    "frontend/js/dashboard.js": '''
/* Admin dashboard — client-side logic */
function renderComment(commentText) {
    document.getElementById("comments").innerHTML += commentText;
}
function applyFilter(expression) {
    return eval(`items.filter(${expression})`);
}
''',

    "package.json": '''
{
  "name": "example-microservice",
  "version": "1.0.0",
  "dependencies": {
    "express": "4.17.3",
    "jsonwebtoken": "8.5.1",
    "lodash": "4.17.20",
    "axios": "0.21.1",
    "node-fetch": "2.6.1"
  }
}
''',
}

# ═══════════════════════════════════════════════════════════════════════════════
# HOST tools — deterministic scanners (no model). These do the real work.
# ═══════════════════════════════════════════════════════════════════════════════

_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r'(?i)(api[_-]?key|apikey|secret|password|passwd|token|jwt[_-]?secret)\s*[:=]\s*["\'][^"\']{8,}["\']', "Hardcoded credential"),
    (r'sk[-_](live|test|proj)[-_][a-zA-Z0-9]{20,}', "Stripe / API key pattern"),
    (r'(?i)postgres(?:ql)?://[^/]+:[^/@]+@', "DB connection string with embedded password"),
    (r'(?i)SMTP[_-]?PASSWORD\s*[:=]\s*["\'][^"\']+["\']', "SMTP password in source"),
]
_INJECTION_PATTERNS: list[tuple[str, str, str]] = [
    (r'(?i)(?:execute|cursor\.execute|exec|query)\s*\(\s*f["\']', "Dynamic SQL with f-string", "high"),
    (r'(?i)(?:SELECT|INSERT|UPDATE|DELETE)\s+.*LIKE\s+.*%\{', "LIKE with unsanitised input", "high"),
    (r'subprocess\.(?:call|run|Popen|getoutput)\s*\([^)]*f["\']', "Shell command with f-string", "critical"),
    (r'\.innerHTML\s*[+\-]=', "innerHTML assignment (XSS)", "high"),
    (r'(?i)\beval\s*\(', "eval() call (code execution)", "critical"),
]
_SIMULATED_CVE_DB: dict[str, list[dict[str, str]]] = {
    "jsonwebtoken": [{"cve": "CVE-2022-23529", "severity": "critical", "desc": "RCE via malicious JWT secret (<=8.5.1)"}],
    "lodash": [{"cve": "CVE-2021-23337", "severity": "critical", "desc": "Command injection via template (<=4.17.20)"}],
    "axios": [{"cve": "CVE-2022-12165", "severity": "high", "desc": "SSRF via baseURL manipulation (<=0.21.1)"}],
    "express": [{"cve": "CVE-2022-24999", "severity": "high", "desc": "Prototype pollution via qs (<=4.17.3)"}],
    "node-fetch": [{"cve": "CVE-2022-0235", "severity": "high", "desc": "Sensitive header exposure on redirect (<=2.6.1)"}],
}


def scan_codebase(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    total_files = 0
    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file():
            continue
        total_files += 1
        if fpath.suffix not in {".py", ".js", ".ts", ".java", ".go", ".yaml", ".yml", ".env"}:
            continue
        rel = str(fpath.relative_to(root))
        for lineno, line in enumerate(fpath.read_text().splitlines(), 1):
            for pattern, desc in _SECRET_PATTERNS:
                if re.search(pattern, line):
                    findings.append({"file": rel, "line": lineno, "type": desc, "category": "secret", "severity": "critical"})
            for pattern, desc, sev in _INJECTION_PATTERNS:
                if re.search(pattern, line):
                    findings.append({"file": rel, "line": lineno, "type": desc, "category": "injection", "severity": sev})

    deps: dict[str, str] = {}
    pkg = root / "package.json"
    if pkg.exists():
        deps = json.loads(pkg.read_text()).get("dependencies", {})
    vulnerable = {p: _SIMULATED_CVE_DB[p] for p in deps if p in _SIMULATED_CVE_DB}

    severity_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1
    return {
        "total_files_scanned": total_files,
        "findings": findings,
        "secrets_found": sum(1 for f in findings if f["category"] == "secret"),
        "injection_risks": sum(1 for f in findings if f["category"] == "injection"),
        "severity_breakdown": severity_counts,
        "vulnerable_dependencies": vulnerable,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "security-audit-reporter"


def install_skill() -> str:
    skill_src = SKILL_SOURCE
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


async def main() -> None:
    provider = configure_model(temperature=0.2)
    print(f"Model provider: {provider}\n")

    # ── HOST: build sample codebase and run the deterministic scanners ──
    tmp_root = Path(tempfile.mkdtemp(prefix="agently_audit_"))
    for rel_path, content in SAMPLE_FILES.items():
        full = tmp_root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    print(f"Sample codebase: {tmp_root}")

    scan = scan_codebase(tmp_root)
    print(f"Scanners found {len(scan['findings'])} findings, "
          f"{len(scan['vulnerable_dependencies'])} vulnerable deps\n")

    skill_id = install_skill()
    agent = Agently.create_agent("security-auditor")

    divider = "=" * 60
    print(divider)
    print("Code Security Audit — host scanners + prompt-only Skill")
    print(divider)
    print("Triaging findings into an audit report...\n")

    streamed: set[str] = set()

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") != "skills.model_stream":
            return
        path = item.get("path")
        if path and item.get("is_completed") and path not in streamed:
            streamed.add(str(path))
            print(f"  [section ready] {path}")

    execution = await agent.async_run_skills_task(
        "Produce a security audit report from these scan findings:\n\n"
        + json.dumps(scan, ensure_ascii=False, indent=2),
        skills=[skill_id],
        mode="required",
        output={
            "risk_level": (str, "Overall risk: low, medium, high, or critical", True),
            "executive_summary": (str, "Short summary for an engineering leader", True),
            "top_remediations": (
                [{"action": (str, "Concrete fix", True), "why": (str, "Why it matters", True)}],
                "Prioritized remediations (most urgent first)",
                True,
            ),
            "themes": (
                [{"theme": (str, "secrets/injection/deps", True), "count": (int, "Finding count", True)}],
                "Findings grouped by theme",
                True,
            ),
        },
        stream_handler=on_stream,
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    remediations = result.get("top_remediations", []) or []

    print(f"\n  total files scanned: {scan['total_files_scanned']}")
    print(f"  findings: {len(scan['findings'])} (secrets={scan['secrets_found']}, injections={scan['injection_risks']})")
    print(f"  vulnerable deps: {len(scan['vulnerable_dependencies'])}")
    print(f"  risk level: {result.get('risk_level', '?')}")
    print(f"  severity breakdown: {scan['severity_breakdown']}")
    print("\n  top remediations:")
    for r in remediations[:4]:
        print(f"    · {str(r.get('action', ''))[:90]}")

    out_dir = Path(tempfile.mkdtemp(prefix="agently_audit_report_"))
    out_path = out_dir / f"security_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out_path.write_text(
        f"# Security Audit — risk: {result.get('risk_level', '?')}\n\n"
        f"{result.get('executive_summary', '')}\n\n## Remediations\n\n"
        + "\n".join(f"- {r.get('action')} — {r.get('why')}" for r in remediations),
        encoding="utf-8",
    )

    print(f"\nskill status: {execution.status}")
    print(f"total findings: {len(scan['findings'])}")
    print(f"risk level: {result.get('risk_level', '?')}")
    print(f"audit report saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
