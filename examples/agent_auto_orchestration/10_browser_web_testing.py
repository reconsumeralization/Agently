"""Browser web testing — host serves app + scans + screenshots, Skill writes QA report.

Run:
    python examples/agent_auto_orchestration/10_browser_web_testing.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Optional: pip install playwright && playwright install chromium for a real
    screenshot; otherwise the screenshot step is skipped gracefully.

New-standard Skills model
-------------------------
The old design wrapped serving / a11y scan / screenshot as Skill ``action``
stages. Under the new standard those are plain HOST tools: the host serves a
local test app, runs a deterministic accessibility scan, and (if Playwright is
installed) captures a screenshot. A single prompt-only ``SKILL.md`` then turns
the page markup + scan findings into a QA test report (shaped by
``semantic_outputs``). Skill = QA reasoning; serving/scanning/screenshots = host.

Expected key output from one real DeepSeek run:
    skill status: success
    pages tested: 2
    total issues: >=1
    screenshot: <path or '(skipped — playwright not installed)'>
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import re
import socketserver
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

# ═══════════════════════════════════════════════════════════════════════════════
# A small local web app to test (intentional accessibility gaps)
# ═══════════════════════════════════════════════════════════════════════════════

PAGE_INDEX = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>CI Dashboard</title></head>
<body>
  <div class="banner"><img src="/logo.png"></div>
  <p>Recent pipeline runs</p>
  <table>
    <tr><td>#2841 auth-refactor</td><td>passed</td></tr>
    <tr><td>#2839 add-rate-limiter</td><td>passed</td></tr>
  </table>
  <a href="/settings.html">Settings</a>
</body></html>
"""

PAGE_SETTINGS = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Settings</title></head>
<body>
  <h1>Settings</h1>
  <form>
    <input type="text" name="email" placeholder="Email">
    <input type="password" name="token" placeholder="API token">
    <button type="submit">Save</button>
  </form>
</body></html>
"""

PAGES = {"index.html": PAGE_INDEX, "settings.html": PAGE_SETTINGS}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence per-request logging
        pass


@contextlib.contextmanager
def serve_test_app(root: Path):
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(root), **kw)
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            thread.join(timeout=2)


# ═══════════════════════════════════════════════════════════════════════════════
# HOST tools — deterministic accessibility scan + optional screenshot
# ═══════════════════════════════════════════════════════════════════════════════

def accessibility_scan(pages: dict[str, str]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for name, html in pages.items():
        imgs = re.findall(r"<img\b[^>]*>", html, re.I)
        for img in imgs:
            if not re.search(r'\balt\s*=', img, re.I):
                findings.append({"page": name, "severity": "high", "category": "accessibility",
                                 "issue": "Image without alt text", "wcag": "WCAG 1.1.1",
                                 "fix": "Add a descriptive alt attribute."})
        if "<form" in html.lower() and "<label" not in html.lower():
            findings.append({"page": name, "severity": "high", "category": "accessibility",
                             "issue": "Form inputs without <label>", "wcag": "WCAG 3.3.2",
                             "fix": "Associate a <label> with each input."})
        if "<h1" not in html.lower():
            findings.append({"page": name, "severity": "medium", "category": "accessibility",
                             "issue": "No h1 heading", "wcag": "WCAG 1.3.1",
                             "fix": "Add a single h1 describing the page."})
    severity_counts: dict[str, int] = {}
    for f in findings:
        severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1
    return {
        "findings": findings,
        "total_issues": len(findings),
        "severity_breakdown": severity_counts,
        "passed": not any(f["severity"] == "high" for f in findings),
        "pages_tested": len(pages),
    }


async def capture_screenshot(url: str) -> str:
    """Full-page screenshot via Playwright if available; '' if not installed."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url, wait_until="networkidle", timeout=15000)
            out = Path(tempfile.mkdtemp(prefix="agently_screenshots_")) / f"page_{datetime.now():%Y%m%d_%H%M%S}.png"
            await page.screenshot(path=str(out), full_page=True)
            await browser.close()
            return str(out)
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_MD = """\
---
name: Web App QA Reporter
description: >-
  Turn a local web app's markup and an accessibility scan into a QA test report:
  functional checks, accessibility issues with WCAG references, and prioritized
  fixes. Use for web testing, QA, accessibility audit, and frontend review.
keywords: [web testing, QA, accessibility, WCAG, frontend, audit]
---

# Web App QA Reporter

You are a frontend QA engineer. You are given the served pages' HTML and the
output of a deterministic accessibility scan. Produce a QA report.

## Do
1. Summarize what the app appears to do (from the markup).
2. List functional checks a tester should run (navigation, form submit, etc.).
3. Report accessibility issues — incorporate the scan findings, each with its
   WCAG reference and a concrete fix; add any additional issues the markup
   reveals.
4. Give an overall PASS / ISSUES-FOUND verdict and the top fixes in priority order.

Base findings on the provided markup and scan. Do not invent pages or elements.
"""


def install_skill() -> str:
    skill_src = Path(tempfile.mkdtemp(prefix="agently_skill_src_")) / "web-app-qa-reporter"
    skill_src.mkdir(parents=True, exist_ok=True)
    (skill_src / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


async def main() -> None:
    provider = configure_model(temperature=0.2)
    print(f"Model provider: {provider}\n")

    app_root = Path(tempfile.mkdtemp(prefix="agently_testapp_"))
    for name, html in PAGES.items():
        (app_root / name).write_text(html, encoding="utf-8")

    divider = "=" * 60
    print(divider)
    print("Browser Web Testing — host tools + prompt-only Skill")
    print(divider)

    with serve_test_app(app_root) as base_url:
        print(f"Test server: {base_url}")
        scan = accessibility_scan(PAGES)
        print(f"  a11y scan: {scan['total_issues']} issues across {scan['pages_tested']} pages")
        screenshot = await capture_screenshot(f"{base_url}/index.html")
        print(f"  screenshot: {screenshot or '(skipped — playwright not installed)'}\n")

        skill_id = install_skill()
        agent = Agently.create_agent("web-qa")

        markup = "\n\n".join(f"### {name}\n{html}" for name, html in PAGES.items())
        task = (
            f"Produce a QA report for this local web app served at {base_url}.\n\n"
            f"Pages markup:\n{markup}\n\n"
            f"Accessibility scan findings (JSON):\n{scan['findings']}"
        )

        print("Generating QA report (skill)...")
        execution = await agent.async_run_skills_task(
            task,
            skills=[skill_id],
            mode="required",
            semantic_outputs={
                "app_summary": (str, "What the app appears to do", True),
                "functional_checks": ([str], "Functional checks a tester should run", True),
                "accessibility_issues": (
                    [{
                        "page": (str, "Page", True),
                        "issue": (str, "Issue", True),
                        "wcag": (str, "WCAG reference", True),
                        "fix": (str, "Concrete fix", True),
                    }],
                    "Accessibility issues",
                    True,
                ),
                "verdict": (str, "PASS or ISSUES-FOUND", True),
                "report": (str, "Full markdown QA report", True),
            },
        )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    issues = result.get("accessibility_issues", []) or []
    print(f"\n  app: {str(result.get('app_summary', ''))[:120]}")
    print(f"  verdict: {result.get('verdict', '—')}")
    for i in issues[:4]:
        print(f"    · [{i.get('page', '—')}] {str(i.get('issue', ''))[:60]} ({i.get('wcag', '—')})")

    print(f"\nskill status: {execution.status}")
    print(f"pages tested: {scan['pages_tested']}")
    print(f"total issues: {len(issues)}")
    print(f"screenshot: {screenshot or '(skipped — playwright not installed)'}")


if __name__ == "__main__":
    asyncio.run(main())
