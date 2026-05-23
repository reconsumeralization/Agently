"""Code security audit — action-backed Skill with real file ops, bash, and network.

Run:
    python examples/agent_auto_orchestration/07_code_security_audit.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich

This example demonstrates a non-text-guidance Skill — its value comes from action
stages performing real work (file scanning, shell-based linting, CVE lookup) rather
than from providing guidance text to an LLM. The model only contributes a final
natural-language summary of the structured findings.

The Skill's action stages:
  1. scan_codebase     — discover all source files, classify by language
  2. grep_secrets      — shell-based scan for hardcoded keys/tokens/passwords
  3. grep_sqli         — shell-based scan for SQL injection patterns
  4. lookup_cves       — network call to check dependency CVEs (simulated)
  5. compile_report    — assemble structured findings

Capabilities demonstrated:
  - Non-text-guidance (workflow) Skill execution
  - Workspace file operations via enable_workspace
  - Bash sandbox command execution via enable_shell
  - Custom Python action functions
  - Network API simulation in action stages
  - Rich multi-panel live display with per-stage progress
  - Field-level delta streaming from action outputs
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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
    # ... execute query
    token_payload = {"sub": username, "iat": 1234567890}
    return {"token": "fake-jwt", "user": username}

def reset_password(user_id: int, new_password: str) -> bool:
    query = f"UPDATE users SET password = '{new_password}' WHERE id = {user_id}"
    return True

def verify_admin(token: str) -> bool:
    # Trusts client-supplied role without verification
    import base64, json as _json
    try:
        payload = _json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        return payload.get("role") == "admin"
    except Exception:
        return False
''',

    "src/api/payment_controller.py": '''
"""Payment processing controller."""
import hashlib
import requests

STRIPE_API_KEY = "example-stripe-api-key-redacted"
_VERIFY_SSL = False  # Disabled for "internal" testing

def process_payment(amount_cents: int, card_token: str) -> dict:
    # Raw string formatting into shell command
    import subprocess
    cmd = f"stripe charge {amount_cents} --token {card_token}"
    result = subprocess.getoutput(cmd)
    return {"status": "ok", "raw": result}

def refund(transaction_id: str) -> dict:
    # Unsanitised external API call
    import urllib.request
    url = f"https://payments.internal/refund?txn={transaction_id}"
    resp = urllib.request.urlopen(url, timeout=5)
    return {"status": "ok", "body": resp.read().decode()}

# Exposed debug endpoint
def debug_payment_logs() -> list[dict]:
    import glob
    logs = []
    for f in glob.glob("/var/log/payments/*.log"):
        with open(f) as fh:
            logs.append({"file": f, "content": fh.read()})
    return logs
''',

    "src/workers/email_sender.py": '''
"""Transactional email worker."""
import smtplib
from email.mime.text import MIMEText

SMTP_PASSWORD = "em@il-p@ss-2024!"

def send_email(to_addr: str, subject: str, body_html: str) -> bool:
    msg = MIMEText(body_html, "html")
    msg["Subject"] = subject
    msg["From"] = "noreply@example.com"
    msg["To"] = to_addr
    with smtplib.SMTP("smtp.example.com", 587) as server:
        server.starttls()
        server.login("noreply@example.com", SMTP_PASSWORD)
        server.send_message(msg)
    return True

def send_bulk(recipients: list[str], template_html: str) -> dict:
    # No rate limiting, no unsubscribe header
    results = {}
    for addr in recipients:
        results[addr] = send_email(addr, "Special Offer!", template_html)
    return results
''',

    "src/models/user_repository.py": '''
"""User data access layer."""
import sqlite3
from typing import Any

DB_PATH = "/data/users.db"

def get_user_by_id(user_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def search_users(keyword: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    # LIKE with unsanitised user input
    cur = conn.execute(f"SELECT * FROM users WHERE name LIKE '%{keyword}%'")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def upsert_user(user_data: dict[str, Any]) -> int:
    conn = sqlite3.connect(DB_PATH)
    keys = ", ".join(user_data.keys())
    vals = ", ".join(f"'{v}'" for v in user_data.values())
    conn.execute(f"INSERT OR REPLACE INTO users ({keys}) VALUES ({vals})")
    conn.commit()
    conn.close()
    return 1
''',

    "frontend/js/dashboard.js": '''
/* Admin dashboard — client-side logic */
const API_BASE = "https://api.example.com";

// Stores auth token in localStorage (XSS-accessible)
function login(username, password) {
    fetch(`${API_BASE}/auth`, {
        method: "POST",
        body: JSON.stringify({ username, password })
    })
    .then(r => r.json())
    .then(data => {
        localStorage.setItem("auth_token", data.token);
        localStorage.setItem("user_role", data.role);
    });
}

// Renders user-supplied HTML without sanitisation
function renderComment(commentText) {
    document.getElementById("comments").innerHTML += commentText;
}

// Uses eval for dynamic filter expressions
function applyFilter(expression) {
    const items = window.__cachedItems;
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
    "node-fetch": "2.6.1",
    "crypto-js": "4.0.0"
  }
}
''',
}

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — workflow (action-backed), not guidance
# ═══════════════════════════════════════════════════════════════════════════════

AUDIT_SKILL_YAML = """
skill_id: code-security-audit
version: 1.0.0
display_name: Code Security Audit
purpose: >
  Scan a target codebase for security vulnerabilities: hardcoded secrets,
  SQL injection patterns, insecure dependency versions, XSS vectors, and
  unsafe command execution. Produces a structured audit report with
  severity ratings and remediation guidance.
trust_level: local
kind: workflow
activation:
  keywords:
    - security audit
    - code scan
    - vulnerability check
    - secret scan
    - security review
requires:
  actions:
    - scan_codebase
    - grep_secrets
    - grep_sqli
    - lookup_cves
    - compile_audit_report
stages:
  - id: scan_codebase
    kind: action
    action: scan_codebase
    input:
      target_dir: "${task}"
  - id: grep_secrets
    kind: action
    action: grep_secrets
    input:
      target_dir: "${task}"
  - id: grep_sqli
    kind: action
    action: grep_sqli
    input:
      target_dir: "${task}"
  - id: lookup_cves
    kind: action
    action: lookup_cves
    depends_on:
      - scan_codebase
    input:
      dependencies: "${state.scan_codebase.dependencies}"
  - id: compile_audit_report
    kind: action
    action: compile_audit_report
    depends_on:
      - scan_codebase
      - grep_secrets
      - grep_sqli
      - lookup_cves
    input:
      scan_codebase: "${state.scan_codebase}"
      grep_secrets: "${state.grep_secrets}"
      grep_sqli: "${state.grep_sqli}"
      lookup_cves: "${state.lookup_cves}"
semantic_outputs:
  report: compile_audit_report
tags:
  - security
  - audit
  - code-review
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Action implementations — real Python functions registered as actions
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_scan_codebase(target_dir: str, **kwargs) -> dict[str, Any]:
    """Walk the target directory, classify files by language, extract dependency info."""
    root = Path(target_dir)
    if not root.exists():
        return {"error": f"Directory not found: {target_dir}"}

    files_by_lang: dict[str, list[str]] = {}
    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(root))
        suffix = fpath.suffix
        if suffix in {".py"}:
            files_by_lang.setdefault("python", []).append(rel)
        elif suffix in {".js", ".ts", ".jsx", ".tsx"}:
            files_by_lang.setdefault("javascript", []).append(rel)
        elif suffix in {".json"}:
            files_by_lang.setdefault("json", []).append(rel)
        elif suffix in {".java", ".kt"}:
            files_by_lang.setdefault("jvm", []).append(rel)
        elif suffix in {".go"}:
            files_by_lang.setdefault("go", []).append(rel)
        else:
            files_by_lang.setdefault("other", []).append(rel)

    total_files = sum(len(v) for v in files_by_lang.values())

    # Extract dependencies from package.json if present
    dependencies: dict[str, str] = {}
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            dependencies.update(pkg.get("dependencies", {}))
        except Exception:
            pass

    return {
        "total_files": total_files,
        "files_by_language": files_by_lang,
        "dependencies": dependencies,
    }


_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r'(?i)(api[_-]?key|apikey|secret|password|passwd|token|jwt[_-]?secret)\s*[:=]\s*["\'][^"\']{8,}["\']', "Hardcoded credential"),
    (r'sk[-_](live|test|proj)[-_][a-zA-Z0-9]{20,}', "Stripe / API key pattern"),
    (r'(?i)postgres(?:ql)?://[^/]+:[^/@]+@', "Database connection string with embedded password"),
    (r'(?i)SMTP[_-]?PASSWORD\s*[:=]\s*["\'][^"\']+["\']', "SMTP password in source"),
    (r'(?i)private[_-]?key|rsa[_-]?private|ec[_-]?private', "Private key reference"),
]


async def _action_grep_secrets(target_dir: str, **kwargs) -> dict[str, Any]:
    """Scan files for hardcoded secrets using regex patterns."""
    root = Path(target_dir)
    findings: list[dict[str, Any]] = []

    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file() or fpath.suffix not in {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".go", ".yaml", ".yml", ".toml", ".env", ".ini", ".cfg"}:
            continue
        if any(part.startswith(".") for part in fpath.parts):
            continue
        try:
            content = fpath.read_text()
        except Exception:
            continue

        rel = str(fpath.relative_to(root))
        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern, desc in _SECRET_PATTERNS:
                m = re.search(pattern, line)
                if m:
                    # Mask the actual secret value
                    masked = line[:m.start()] + m.group(0)[:20] + "***[MASKED]"
                    findings.append({
                        "file": rel,
                        "line": lineno,
                        "type": desc,
                        "snippet": masked.strip()[:120],
                        "severity": "critical",
                    })

    return {"findings": findings, "total": len(findings)}


_SQLI_PATTERNS: list[tuple[str, str]] = [
    (r'(?i)(?:execute|cursor\.execute|exec|query)\s*\(\s*f["\']', "Dynamic SQL with f-string"),
    (r'(?i)(?:execute|cursor\.execute|exec|query)\s*\(\s*["\'].*%\s*\(', "Dynamic SQL with %-formatting"),
    (r'(?i)(?:SELECT|INSERT|UPDATE|DELETE)\s+.*\+.*\+', "String concatenation in SQL"),
    (r'(?i)\.execute\s*\(\s*["\'][^"\']*[\'"]\s*%\s*\w+', "SQL with modulo interpolation"),
]

_XSS_PATTERNS: list[tuple[str, str]] = [
    (r'\.innerHTML\s*[+\-]=', "innerHTML assignment (XSS vector)"),
    (r'document\.write\s*\(', "document.write call (XSS vector)"),
    (r'(?i)eval\s*\(', "eval() call (arbitrary code execution)"),
    (r'(?i)dangerouslySetInnerHTML', "React dangerouslySetInnerHTML"),
]

_CMD_INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r'subprocess\.(?:call|run|Popen|getoutput)\s*\([^)]*\+', "Shell command with string concatenation"),
    (r'os\.system\s*\([^)]*\+', "os.system with concatenation"),
    (r'(?i)(?:exec|spawn|popen|system)\s*\(\s*["\'][^"\']*\$\{', "Template literal in shell command"),
]


async def _action_grep_sqli(target_dir: str, **kwargs) -> dict[str, Any]:
    """Scan for SQL injection, XSS, and command injection patterns."""
    root = Path(target_dir)
    findings: list[dict[str, Any]] = []

    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file() or fpath.suffix not in {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".go", ".rb", ".php"}:
            continue
        if any(part.startswith(".") for part in fpath.parts):
            continue
        try:
            content = fpath.read_text()
        except Exception:
            continue

        rel = str(fpath.relative_to(root))
        for lineno, line in enumerate(content.splitlines(), 1):
            for pattern, desc in _SQLI_PATTERNS:
                if re.search(pattern, line):
                    findings.append({
                        "file": rel, "line": lineno, "type": desc,
                        "category": "sql_injection", "severity": "high",
                        "snippet": line.strip()[:120],
                    })
            for pattern, desc in _XSS_PATTERNS:
                if re.search(pattern, line):
                    findings.append({
                        "file": rel, "line": lineno, "type": desc,
                        "category": "xss", "severity": "high",
                        "snippet": line.strip()[:120],
                    })
            for pattern, desc in _CMD_INJECTION_PATTERNS:
                if re.search(pattern, line):
                    findings.append({
                        "file": rel, "line": lineno, "type": desc,
                        "category": "command_injection", "severity": "critical",
                        "snippet": line.strip()[:120],
                    })

    by_category: dict[str, int] = {}
    for f in findings:
        by_category[f["category"]] = by_category.get(f["category"], 0) + 1

    return {"findings": findings, "total": len(findings), "by_category": by_category}


# Simulated CVE database — in production this would call osv.dev or NVD API
_SIMULATED_CVE_DB: dict[str, list[dict[str, str]]] = {
    "express": [
        {"cve": "CVE-2022-24999", "severity": "high", "desc": "Prototype pollution via qs parser (<=4.17.3)"},
    ],
    "jsonwebtoken": [
        {"cve": "CVE-2022-23529", "severity": "critical", "desc": "Remote code execution via malicious JWT secret (<=8.5.1)"},
        {"cve": "CVE-2022-23539", "severity": "high", "desc": "Improper key verification allows forged tokens (<=8.5.1)"},
    ],
    "lodash": [
        {"cve": "CVE-2021-23337", "severity": "critical", "desc": "Command injection via template (<=4.17.20)"},
        {"cve": "CVE-2020-8203", "severity": "high", "desc": "Prototype pollution via deep defaults (<=4.17.20)"},
    ],
    "axios": [
        {"cve": "CVE-2022-12165", "severity": "high", "desc": "SSRF via baseURL manipulation (<=0.21.1)"},
        {"cve": "CVE-2020-28168", "severity": "medium", "desc": "Server-side request forgery (<=0.21.1)"},
    ],
    "node-fetch": [
        {"cve": "CVE-2022-0235", "severity": "high", "desc": "Exposure of sensitive headers on redirect (<=2.6.1)"},
    ],
}


async def _action_lookup_cves(dependencies: dict[str, str], **kwargs) -> dict[str, Any]:
    """Look up known CVEs for declared dependencies. Uses simulated DB; in production
    this would call the OSV.dev API or GitHub Advisory Database."""
    # Simulate network call latency
    await asyncio.sleep(0.3)

    results: dict[str, list[dict[str, str]]] = {}
    critical_count = 0
    high_count = 0

    for pkg, version in (dependencies or {}).items():
        cves = _SIMULATED_CVE_DB.get(pkg, [])
        if cves:
            results[pkg] = cves
            for c in cves:
                if c["severity"] == "critical":
                    critical_count += 1
                elif c["severity"] == "high":
                    high_count += 1

    return {
        "vulnerable_packages": results,
        "total_packages_scanned": len(dependencies),
        "vulnerable_count": len(results),
        "critical_cves": critical_count,
        "high_cves": high_count,
    }


async def _action_compile_audit_report(**kwargs) -> dict[str, Any]:
    """Compile all stage findings into a structured audit report."""
    all_findings: list[dict[str, Any]] = []
    secrets_total = 0
    sqli_total = 0

    # Stage outputs are passed via kwargs (from skill YAML input bindings)
    secrets = (kwargs.get("grep_secrets") or {}).get("findings", [])
    injections = (kwargs.get("grep_sqli") or {}).get("findings", [])
    cve_data = kwargs.get("lookup_cves") or {}
    scan_data = kwargs.get("scan_codebase") or {}

    all_findings.extend(secrets)
    all_findings.extend(injections)
    secrets_total = len(secrets)
    sqli_total = len(injections)

    severity_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in all_findings:
        sev = f.get("severity", "low")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    # Summarize by file
    by_file: dict[str, list[str]] = {}
    for f in all_findings:
        by_file.setdefault(f["file"], []).append(f["type"])

    return {
        "summary": {
            "total_files_scanned": scan_data.get("total_files", 0),
            "total_findings": len(all_findings),
            "secrets_found": secrets_total,
            "injection_risks": sqli_total,
            "vulnerable_dependencies": cve_data.get("vulnerable_count", 0),
            "critical_cves": cve_data.get("critical_cves", 0),
            "high_cves": cve_data.get("high_cves", 0),
        },
        "severity_breakdown": severity_counts,
        "findings_by_file": {k: list(set(v)) for k, v in by_file.items()},
        "cve_details": cve_data.get("vulnerable_packages", {}),
        "findings": all_findings,
        "risk_level": "critical" if severity_counts.get("critical", 0) > 2 else "high",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════


def _build_status_table(scan_data: dict | None, secrets_data: dict | None, sqli_data: dict | None, cve_data: dict | None) -> Table:
    t = Table(title="Stage Progress", expand=True, show_header=True, header_style="bold")
    t.add_column("Stage", style="cyan", width=22)
    t.add_column("Status", style="yellow", width=12)
    t.add_column("Output", style="white")

    stages = [
        ("scan_codebase", scan_data),
        ("grep_secrets", secrets_data),
        ("grep_sqli", sqli_data),
        ("lookup_cves", cve_data),
    ]

    for name, data in stages:
        if data is None:
            t.add_row(name, "◎ running...", "")
        elif data.get("error"):
            t.add_row(name, "✗ error", data["error"])
        else:
            detail = ""
            if name == "scan_codebase":
                detail = f"{data.get('total_files', 0)} files, {len(data.get('dependencies', {}))} deps"
            elif name == "grep_secrets":
                detail = f"{data.get('total', 0)} secrets found"
            elif name == "grep_sqli":
                detail = f"{data.get('total', 0)} patterns ({data.get('by_category', {})})"
            elif name == "lookup_cves":
                detail = f"{data.get('vulnerable_count', 0)} vuln pkgs, {data.get('critical_cves', 0)} critical CVEs"
            t.add_row(name, "✓ complete", detail)

    return t


def _build_findings_panel(findings_data: list[dict[str, Any]] | None) -> Panel:
    if not findings_data:
        return Panel(Text("  waiting for findings...", style="dim"), title="Findings", border_style="yellow")

    content: list[Text] = []
    for f in findings_data[:20]:  # show first 20
        sev_color = {"critical": "red", "high": "yellow", "medium": "dim", "low": "dim"}
        color = sev_color.get(f.get("severity", "low"), "dim")
        icon = "▸"
        content.append(Text(f"  {icon} [{color}]{f['severity'].upper()}[/] {f['file']}:{f.get('line', '?')}", style=""))
        content.append(Text(f"    {f['snippet']}", style="dim"))

    if len(findings_data) > 20:
        content.append(Text(f"\n  ... and {len(findings_data) - 20} more findings", style="dim"))

    return Panel(Group(*content), title=f"Findings ({len(findings_data)})", border_style="yellow")


def _build_layout(status_table: Table, findings_panel: Panel, report: dict | None = None) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=2),
        Layout(name="bottom", ratio=3),
    )
    layout["top"].split_row(
        Layout(status_table, name="status"),
    )
    bottom_right: Panel
    if report:
        s = report.get("summary", {})
        summary_text = "\n".join([
            f"  Files scanned: {s.get('total_files_scanned', 0)}",
            f"  Total findings: {s.get('total_findings', 0)}",
            f"  Secrets exposed: {s.get('secrets_found', 0)}",
            f"  Injection risks: {s.get('injection_risks', 0)}",
            f"  Vuln dependencies: {s.get('vulnerable_dependencies', 0)} ({s.get('critical_cves', 0)} critical CVEs)",
            f"  Risk level: [bold red]{report.get('risk_level', '?').upper()}[/]",
        ])
        bottom_right = Panel(Text(summary_text), title="Audit Report", border_style="red")
    else:
        bottom_right = Panel(Text("  waiting...", style="dim"), title="Audit Report", border_style="dim")
    layout["bottom"].split_row(
        Layout(findings_panel, name="findings"),
        Layout(bottom_right, name="report"),
    )
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.2)

    # Create a temporary "microservice" codebase from the sample files
    tmp_root = Path(tempfile.mkdtemp(prefix="agently_audit_"))
    for rel_path, content in SAMPLE_FILES.items():
        full = tmp_root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content.strip())
    print(f"Sample codebase: {tmp_root}")

    # Set up skills registry and install the audit skill
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"
    skill_dir = runtime_dir / "code-security-audit"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(AUDIT_SKILL_YAML.strip())

    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("code-security-auditor")

    # Register custom action functions
    agent.register_action(
        name="scan_codebase",
        desc="Walk a target directory and classify source files by language. Extract dependency manifests.",
        kwargs={"target_dir": ("str", "Path to the codebase root directory.")},
        func=_action_scan_codebase,
    )
    agent.register_action(
        name="grep_secrets",
        desc="Scan source files for hardcoded secrets: API keys, passwords, tokens, connection strings.",
        kwargs={"target_dir": ("str", "Path to the codebase root directory.")},
        func=_action_grep_secrets,
    )
    agent.register_action(
        name="grep_sqli",
        desc="Scan source files for SQL injection, XSS, and command injection patterns.",
        kwargs={"target_dir": ("str", "Path to the codebase root directory.")},
        func=_action_grep_sqli,
    )
    agent.register_action(
        name="lookup_cves",
        desc="Look up known CVEs for declared package dependencies via OSV.dev/NVD API (simulated).",
        kwargs={"dependencies": ("dict", "Dict of package_name -> version strings.")},
        func=_action_lookup_cves,
    )
    agent.register_action(
        name="compile_audit_report",
        desc="Compile all security stage findings into a structured audit report with severity breakdown.",
        kwargs={},
        func=_action_compile_audit_report,
    )

    # Run the audit — pass target directory path directly as the task text
    execution = (
        agent
        .use_skills(["code-security-audit"], mode="required")
        .input(str(tmp_root))
        .create_execution()
    )

    # Live display
    scan_data: dict | None = None
    secrets_data: dict | None = None
    sqli_data: dict | None = None
    cve_data: dict | None = None
    all_findings: list[dict[str, Any]] = []
    final_report: dict | None = None
    completed_stages: set[str] = set()

    status_table = _build_status_table(scan_data, secrets_data, sqli_data, cve_data)
    findings_panel = _build_findings_panel(None)
    layout = _build_layout(status_table, findings_panel, None)

    with Live(layout, refresh_per_second=8, screen=True, transient=False) as live:
        async for item in execution.get_async_generator(type="instant"):
            # Track stage completion
            if item.path.startswith("skills.stages.") and item.is_complete:
                stage_id = item.path.split(".")[-1]
                if stage_id not in ("plan",):
                    completed_stages.add(stage_id)

            # Capture action results from actions.* paths
            if item.path.startswith("actions.") and item.is_complete:
                action_name = item.path.split(".", 1)[1]
                value = item.value or {}
                # The value structure may vary — try several nesting patterns
                result_data = value
                if isinstance(value, dict):
                    result_data = value.get("result", value.get("output", value.get("data", value)))

                if action_name == "scan_codebase":
                    scan_data = result_data if isinstance(result_data, dict) else {}
                elif action_name == "grep_secrets":
                    secrets_data = result_data if isinstance(result_data, dict) else {}
                    all_findings = list(secrets_data.get("findings", []))
                elif action_name == "grep_sqli":
                    sqli_data = result_data if isinstance(result_data, dict) else {}
                    all_findings.extend(sqli_data.get("findings", []))
                elif action_name == "lookup_cves":
                    cve_data = result_data if isinstance(result_data, dict) else {}
                elif action_name == "compile_audit_report":
                    final_report = result_data if isinstance(result_data, dict) else {}

                status_table = _build_status_table(scan_data, secrets_data, sqli_data, cve_data)
                findings_panel = _build_findings_panel(all_findings if all_findings else None)
                layout = _build_layout(status_table, findings_panel, final_report)
                live.update(layout)

    # ── Final summary ────────────────────────────────────────────────────
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    route = (meta.get("route_plan") or {}).get("selected_route", "")
    print(f"\nroute: {route}")
    print(f"stages completed: {sorted(completed_stages)}")

    if final_report:
        s = final_report.get("summary", {})
        print(f"total files: {s.get('total_files_scanned', 0)}")
        print(f"findings: {s.get('total_findings', 0)} (secrets={s.get('secrets_found', 0)}, "
              f"injections={s.get('injection_risks', 0)}, "
              f"vuln_deps={s.get('vulnerable_dependencies', 0)}/{s.get('critical_cves', 0)} critical CVEs)")
        print(f"risk level: {final_report.get('risk_level', '?')}")
        print(f"severity breakdown: {final_report.get('severity_breakdown', {})}")

    # Print per-file findings summary
    if final_report:
        print("\nFindings by file:")
        for fpath, types in (final_report.get("findings_by_file") or {}).items():
            print(f"  {fpath}: {', '.join(types)}")

    # Cleanup
    import shutil
    shutil.rmtree(tmp_root)


if __name__ == "__main__":
    asyncio.run(main())
