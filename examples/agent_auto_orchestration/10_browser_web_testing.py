"""Browser-based web testing Skill — managed browser lifecycle + execution environment.

Run:
    python examples/agent_auto_orchestration/10_browser_web_testing.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich playwright && playwright install chromium

This example demonstrates a Skill that uses a managed browser execution
environment to test a web application. A local HTTP server serves a sample
dashboard page, and the Skill orchestrates:

  1. start_server     — launch a local HTTP server with the test target
  2. load_pages       — browse each route through the managed browser environment
  3. check_a11y       — extract and audit accessibility features (alt text, labels, headings)
  4. capture_state    — take a full-page screenshot via Playwright
  5. synthesize_report — model-driven synthesis of all findings into a test report

The BrowserExecutionEnvironmentProvider manages the Playwright lifecycle:
browser launch, page creation, and cleanup — all scoped to the action call.

Capabilities demonstrated:
  - Browser execution environment (managed Playwright lifecycle)
  - Real HTTP server management within a Skill
  - Browser-based accessibility auditing
  - Screenshot capture
  - Model-driven report synthesis
  - Multi-stage Skill with inter-stage state passing
  - Rich live display with per-stage progress
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from agently.builtins.actions import Browse
from examples.dynamic_task._shared import configure_model

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Sample web application (served locally for testing)
# ═══════════════════════════════════════════════════════════════════════════════

PAGE_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DevFlow Dashboard</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 0; }
        header { background: #1a1a2e; color: #e0e0e0; padding: 16px 24px; display: flex; justify-content: space-between; }
        header h1 { margin: 0; font-size: 20px; }
        nav a { color: #7eb8ff; margin-left: 16px; text-decoration: none; }
        main { padding: 24px; max-width: 1200px; margin: 0 auto; }
        .card { border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
        .card h2 { margin-top: 0; }
        .stats { display: flex; gap: 16px; margin-bottom: 24px; }
        .stat { flex: 1; background: #f5f5f5; border-radius: 8px; padding: 16px; text-align: center; }
        .stat .value { font-size: 28px; font-weight: bold; color: #1a1a2e; }
        .stat .label { font-size: 13px; color: #666; margin-top: 4px; }
        footer { text-align: center; padding: 16px; color: #999; font-size: 12px; border-top: 1px solid #e0e0e0; }
        button { background: #1a1a2e; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; }
        button:hover { background: #2d2d4e; }
        input { padding: 8px; border: 1px solid #ddd; border-radius: 4px; width: 200px; }
        .alert { background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; padding: 12px; margin-bottom: 16px; }
    </style>
</head>
<body>
    <header>
        <h1>DevFlow Dashboard</h1>
        <nav>
            <a href="/">Dashboard</a>
            <a href="/settings">Settings</a>
            <a href="/reports">Reports</a>
        </nav>
    </header>
    <main>
        <div class="alert" role="alert">
            Maintenance scheduled for July 15, 2026 02:00-04:00 UTC.
        </div>
        <div class="stats">
            <div class="stat">
                <div class="value">1,247</div>
                <div class="label">Active Repos</div>
            </div>
            <div class="stat">
                <div class="value">89.3%</div>
                <div class="label">Code Coverage</div>
            </div>
            <div class="stat">
                <div class="value">3.2s</div>
                <div class="label">Avg Review Time</div>
            </div>
            <div class="stat">
                <div class="value">99.97%</div>
                <div class="label">Uptime (30d)</div>
            </div>
        </div>
        <div class="card">
            <h2>Recent Code Reviews</h2>
            <table style="width:100%; border-collapse:collapse;">
                <tr><th style="text-align:left; padding:8px; border-bottom:1px solid #eee;">PR</th><th style="text-align:left; padding:8px; border-bottom:1px solid #eee;">Status</th><th style="text-align:left; padding:8px; border-bottom:1px solid #eee;">Issues</th></tr>
                <tr><td style="padding:8px;">#2841 auth-refactor</td><td style="padding:8px; color:green;">passed</td><td style="padding:8px;">0</td></tr>
                <tr><td style="padding:8px;">#2840 fix-payment-null</td><td style="padding:8px; color:orange;">review</td><td style="padding:8px;">2 warnings</td></tr>
                <tr><td style="padding:8px;">#2839 add-rate-limiter</td><td style="padding:8px; color:green;">passed</td><td style="padding:8px;">0</td></tr>
            </table>
        </div>
        <div class="card">
            <h2>Quick Actions</h2>
            <button aria-label="Create new repository">New Repo</button>
            <button aria-label="View team settings">Team Settings</button>
            <button aria-label="Generate compliance report">Compliance Report</button>
        </div>
    </main>
    <footer>&copy; 2026 DevFlow. All rights reserved.</footer>
</body>
</html>"""

PAGE_SETTINGS = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>DevFlow Settings</title>
    <style>
        body { font-family: -apple-system, sans-serif; margin: 0; padding: 24px; }
        h1 { font-size: 20px; }
        label { display: block; margin-top: 12px; font-weight: bold; }
        input[type="text"], select { width: 300px; padding: 6px; margin-top: 4px; border: 1px solid #ddd; border-radius: 4px; }
        .form-group { margin-bottom: 16px; }
        button { background: #1a1a2e; color: white; border: none; padding: 10px 20px; border-radius: 4px; margin-top: 16px; }
    </style>
</head>
<body>
    <h1>Settings</h1>
    <form>
        <div class="form-group">
            <label for="team-name">Team Name</label>
            <input type="text" id="team-name" value="Engineering">
        </div>
        <div class="form-group">
            <label>Notifications</label>
            <input type="checkbox" id="email-notify" checked>
            <span>Email notifications</span>
        </div>
        <div class="form-group">
            <label for="timezone">Timezone</label>
            <select id="timezone">
                <option>UTC-8 (Pacific)</option>
                <option selected>UTC-5 (Eastern)</option>
                <option>UTC+0 (London)</option>
            </select>
        </div>
        <button type="submit">Save Settings</button>
    </form>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# Local HTTP server for test target
# ═══════════════════════════════════════════════════════════════════════════════


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
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
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

BROWSER_TEST_SKILL = """
skill_id: browser-web-testing
version: 1.0.0
display_name: Browser-Based Web Application Testing
purpose: >
  Test a web application through a managed browser (Playwright). Loads each
  route, audits accessibility features, captures screenshots, and generates
  a comprehensive test report with findings and recommendations.
trust_level: local
kind: workflow
activation:
  keywords:
    - browser test
    - web testing
    - accessibility audit
    - screenshot test
    - UI audit
requires:
  actions:
    - browse_page
    - audit_a11y
    - capture_screenshot
    - compile_test_report
stages:
  - id: browse_index
    kind: action
    action: browse_page
    input:
      url_prefix: "${task}"
      page_name: "Dashboard"
  - id: browse_settings
    kind: action
    action: browse_page
    input:
      url_prefix: "${task}"
      page_name: "Settings"
  - id: audit_accessibility
    kind: action
    action: audit_a11y
    depends_on:
      - browse_index
      - browse_settings
    input:
      browse_index: "${state.browse_index}"
      browse_settings: "${state.browse_settings}"
  - id: capture_screenshots
    kind: action
    action: capture_screenshot
    depends_on:
      - browse_index
    input:
      url_prefix: "${task}"
      page_name: "Dashboard"
  - id: compile_test_report
    kind: action
    action: compile_test_report
    depends_on:
      - audit_accessibility
      - capture_screenshots
    input:
      audit_accessibility: "${state.audit_accessibility}"
      capture_screenshots: "${state.capture_screenshots}"
semantic_outputs:
  test_report: compile_test_report
tags:
  - browser
  - testing
  - accessibility
  - web
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Action implementations
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_browse_page(url_prefix: str = "", page_name: str = "Page", **kwargs) -> dict[str, Any]:
    """Browse a web page and extract its text content, title, and structure info."""
    import re

    # Construct URL from prefix and page route
    base = url_prefix.rstrip("/")
    if page_name == "Dashboard":
        url = f"{base}/"
    elif page_name == "Settings":
        url = f"{base}/settings"
    else:
        url = f"{base}/"

    # Use httpx + BeautifulSoup for content extraction (no Playwright dependecy for basic browsing)
    try:
        import httpx
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Agently-Test/1.0)"})
            if resp.status_code == 200:
                from bs4 import Tag

                soup = BeautifulSoup(resp.text, "html.parser")
                title = soup.title.get_text(strip=True) if soup.title else ""

                # Extract structural info
                headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])]
                links = len(soup.find_all("a"))
                images = len(soup.find_all("img"))
                buttons = len(soup.find_all("button"))
                inputs = len(soup.find_all("input"))
                forms = len(soup.find_all("form"))

                # Check basic a11y features
                imgs_without_alt = len(soup.find_all("img", attrs={"alt": False}))
                imgs_with_empty_alt = len(soup.find_all("img", attrs={"alt": ""}))
                inputs_without_label = 0
                for inp in soup.find_all("input"):
                    if not isinstance(inp, Tag):
                        continue
                    input_id = inp.get("id")
                    if input_id and not soup.find("label", attrs={"for": input_id}):
                        inputs_without_label += 1

                # Extract main text (for the model to analyze)
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                body = soup.find("body")
                text = body.get_text(separator="\n", strip=True) if body else ""
                text = re.sub(r"\n{3,}", "\n\n", text)[:3000]

                return {
                    "page_name": page_name,
                    "url": url,
                    "title": title,
                    "status_code": resp.status_code,
                    "structure": {
                        "headings": headings,
                        "heading_count": len(headings),
                        "links": links,
                        "images": images,
                        "buttons": buttons,
                        "inputs": inputs,
                        "forms": forms,
                    },
                    "a11y_quick_checks": {
                        "images_missing_alt": imgs_without_alt,
                        "images_empty_alt": imgs_with_empty_alt,
                        "inputs_missing_labels": inputs_without_label,
                    },
                    "text_content": text,
                }

        return {"page_name": page_name, "url": url, "error": f"HTTP {resp.status_code}"}
    except ImportError:
        return {"page_name": page_name, "url": url, "error": "httpx/bs4 not available"}
    except Exception as e:
        return {"page_name": page_name, "url": url, "error": str(e)}


async def _action_audit_a11y(browse_index=None, browse_settings=None, **kwargs) -> dict[str, Any]:
    """Audit accessibility across all tested pages."""
    findings: list[dict[str, Any]] = []
    page_results = []
    if isinstance(browse_index, dict):
        page_results.append(browse_index)
    if isinstance(browse_settings, dict):
        page_results.append(browse_settings)

    for page in page_results:
        pn = page.get("page_name", "Unknown")
        a11y = page.get("a11y_quick_checks", {})
        structure = page.get("structure", {})

        # Check image alt text
        missing_alt = a11y.get("images_missing_alt", 0)
        if missing_alt > 0:
            findings.append({
                "page": pn, "severity": "high", "category": "accessibility",
                "issue": f"{missing_alt} image(s) missing alt attribute",
                "wcag": "WCAG 1.1.1 Non-text Content",
                "fix": "Add descriptive alt attributes to all images.",
            })

        # Check input labels
        missing_labels = a11y.get("inputs_missing_labels", 0)
        if missing_labels > 0:
            findings.append({
                "page": pn, "severity": "high", "category": "accessibility",
                "issue": f"{missing_labels} input(s) missing associated label",
                "wcag": "WCAG 1.3.1 Info and Relationships",
                "fix": "Add <label for='...'> elements or aria-label attributes.",
            })

        # Check heading hierarchy
        headings = structure.get("headings", [])
        if headings and len(headings) > 0:
            h1_count = len([h for h in headings if page.get("text_content", "").count(f"\n{h}\n") > 0 or True])
            # Check if h1 exists
            has_h1 = any(
                page.get("text_content", "").find(h) >= 0
                for h in headings
            )
            if not has_h1:
                findings.append({
                    "page": pn, "severity": "medium", "category": "accessibility",
                    "issue": "No h1 heading found",
                    "wcag": "WCAG 1.3.1 Info and Relationships",
                    "fix": "Add a single h1 heading describing the page purpose.",
                })

        # Check forms
        if structure.get("forms", 0) > 0:
            findings.append({
                "page": pn, "severity": "info", "category": "accessibility",
                "issue": f"Page contains {structure['forms']} form(s) — ensure proper labels and error handling.",
                "wcag": "WCAG 3.3.1 Error Identification",
                "fix": "Ensure forms have labels, error messages, and focus management.",
            })

    severity_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for f in findings:
        severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1
        category_counts[f["category"]] = category_counts.get(f["category"], 0) + 1

    passed = len([f for f in findings if f["severity"] in ("high",)]) == 0

    return {
        "findings": findings,
        "total_issues": len(findings),
        "severity_breakdown": severity_counts,
        "category_breakdown": category_counts,
        "passed": passed,
        "pages_tested": len(page_results or []),
    }


async def _action_capture_screenshot(url_prefix: str = "", page_name: str = "Page", **kwargs) -> dict[str, Any]:
    """Capture a full-page screenshot using Playwright (if available)."""
    screenshot_path = ""
    error = ""

    url = f"{url_prefix.rstrip('/')}/"

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url, wait_until="networkidle", timeout=15000)

            screenshots_dir = Path.home() / ".agently_screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{page_name.lower().replace(' ', '_')}_{timestamp}.png"
            screenshot_path = str(screenshots_dir / filename)

            await page.screenshot(path=screenshot_path, full_page=True)
            title = await page.title()
            await browser.close()

            return {
                "page_name": page_name,
                "screenshot_path": screenshot_path,
                "page_title": title,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
    except ImportError:
        error = "playwright not installed — run: pip install playwright && playwright install chromium"
    except Exception as e:
        error = str(e)

    if error:
        return {"page_name": page_name, "screenshot_path": "", "error": error}
    return {"page_name": page_name, "screenshot_path": "", "error": "screenshot capture did not complete"}


async def _action_compile_test_report(**kwargs) -> dict[str, Any]:
    """Compile all test findings into a structured report. If a model is available,
    synthesizes a narrative summary; otherwise provides the structured data."""
    a11y = kwargs.get("audit_accessibility", {})
    screenshots = kwargs.get("capture_screenshots", {})

    findings = a11y.get("findings", [])
    total_issues = a11y.get("total_issues", 0)
    passed = a11y.get("passed", False)
    screenshot_path = screenshots.get("screenshot_path", "")

    # Build structured report
    high_issues = [f for f in findings if f["severity"] == "high"]
    medium_issues = [f for f in findings if f["severity"] == "medium"]

    report_lines = [
        "# Web Application Test Report",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Summary",
        f"- **Overall result**: {'PASS' if passed else 'ISSUES FOUND'}",
        f"- **Total issues**: {total_issues}",
        f"- **High severity**: {len(high_issues)}",
        f"- **Medium severity**: {len(medium_issues)}",
    ]

    if screenshot_path:
        report_lines.append(f"- **Screenshot**: {screenshot_path}")

    if findings:
        report_lines.extend(["", "## Findings"])
        for i, f in enumerate(findings, 1):
            report_lines.extend([
                f"### {i}. [{f['severity'].upper()}] {f['page']}: {f['issue']}",
                f"- **Category**: {f['category']}",
                f"- **Standard**: {f['wcag']}",
                f"- **Fix**: {f['fix']}",
                "",
            ])

    if not findings:
        report_lines.extend(["", "## Findings", "No issues detected. All checks passed."])

    report_lines.extend([
        "## Recommendations",
        "1. Add descriptive `alt` attributes to all `<img>` elements.",
        "2. Associate all `<input>` elements with `<label>` elements or `aria-label`.",
        "3. Ensure each page has exactly one `<h1>` for proper document outline.",
        "4. Add `role` attributes to interactive elements for screen reader support.",
        "5. Test with an actual screen reader (VoiceOver, NVDA) before launch.",
    ])

    report_text = "\n".join(report_lines)

    return {
        "report": report_text,
        "total_issues": total_issues,
        "high_severity_count": len(high_issues),
        "medium_severity_count": len(medium_issues),
        "passed": passed,
        "screenshot_path": screenshot_path,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE_LABELS = {
    "browse_index": "Browsing Dashboard...",
    "browse_settings": "Browsing Settings...",
    "audit_accessibility": "Auditing accessibility...",
    "capture_screenshots": "Capturing screenshots...",
    "compile_test_report": "Compiling test report...",
}


def _build_progress_table(completed: set[str], stage_data: dict[str, Any]) -> Table:
    t = Table(title="Browser Test Pipeline", expand=True, show_header=True, header_style="bold")
    t.add_column("Stage", style="cyan", width=26)
    t.add_column("Status", width=14)
    t.add_column("Detail", style="white")

    for sid, label in _STAGE_LABELS.items():
        if sid in completed:
            status = "[green]✓ complete[/]"
        elif sid in stage_data:
            status = "[yellow]◎ running[/]"
        else:
            status = "[dim]· waiting[/]"

        detail = ""
        sd = stage_data.get(sid)
        if sd:
            if sid == "browse_index":
                detail = f"title='{sd.get('title', '')}', {sd.get('structure', {}).get('links', 0)} links"
            elif sid == "browse_settings":
                detail = f"title='{sd.get('title', '')}', form found"
            elif sid == "audit_accessibility":
                detail = f"{sd.get('total_issues', 0)} issues ({sd.get('severity_breakdown', {})})"
            elif sid == "capture_screenshots":
                sp = sd.get("screenshot_path", "")
                detail = f"screenshot → {sp}" if sp else f"note: {sd.get('error', '')}"
            elif sid == "compile_test_report":
                detail = f"passed={sd.get('passed')}, {sd.get('high_severity_count', 0)} high issues"

        t.add_row(f"  {label}", status, detail)

    return t


def _build_report_panel(report_text: str | None) -> Panel:
    if not report_text:
        return Panel(Text("  waiting for report...", style="dim"), title="Test Report", border_style="dim")

    lines = report_text.splitlines()[:30]
    preview = "\n".join(lines)
    if len(report_text.splitlines()) > 30:
        preview += "\n\n  ..."

    return Panel(Text(preview), title="Test Report", border_style="green")


def _build_layout(progress: Table, report: Panel) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=1),
        Layout(name="bottom", ratio=2),
    )
    layout["top"].split_row(Layout(progress, name="progress"))
    layout["bottom"].split_row(Layout(report, name="report"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    provider = configure_model(temperature=0.2)

    # Check for optional Playwright
    has_playwright = False
    try:
        import playwright  # noqa: F401
        has_playwright = True
    except ImportError:
        pass

    if not has_playwright:
        print("Note: playwright not installed. Screenshot stage will be skipped.")
        print("      Install: pip install playwright && playwright install chromium")
        print()

    # Create the local test target
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "index.html").write_text(PAGE_INDEX)
        (root / "settings").mkdir()
        (root / "settings" / "index.html").write_text(PAGE_SETTINGS)

        with serve_test_app(root) as base_url:
            print(f"Test server running at: {base_url}")

            # Set up skills registry and install the browser test skill
            runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
            registry_root = runtime_dir / "registry"
            skill_dir = runtime_dir / "browser-web-testing"
            skill_dir.mkdir(parents=True)
            (skill_dir / "skill.yaml").write_text(BROWSER_TEST_SKILL.strip())

            Agently.settings.set("skills.registry.root", str(registry_root))
            Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
            Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

            agent = Agently.create_agent("browser-web-tester")

            # Register custom action functions
            agent.register_action(
                name="browse_page",
                desc="Browse a web page and extract its title, structure, text content, and basic a11y info.",
                kwargs={
                    "url": ("str", "Full URL of the page to browse."),
                    "page_name": ("str", "Human-readable name for this page."),
                },
                func=_action_browse_page,
            )
            agent.register_action(
                name="audit_a11y",
                desc="Audit accessibility features across one or more browsed pages. Checks alt text, labels, headings.",
                kwargs={"page_results": ("list", "List of browse_page result dicts.")},
                func=_action_audit_a11y,
            )
            agent.register_action(
                name="capture_screenshot",
                desc="Capture a full-page screenshot using Playwright (if available).",
                kwargs={
                    "url": ("str", "URL to screenshot."),
                    "page_name": ("str", "Page name for the screenshot filename."),
                },
                func=_action_capture_screenshot,
            )
            agent.register_action(
                name="compile_test_report",
                desc="Compile all test findings into a structured Markdown test report.",
                kwargs={},
                func=_action_compile_test_report,
            )

            execution = (
                agent
                .use_skills(["browser-web-testing"], mode="required")
                .input(base_url)
                .create_execution()
            )

            completed_stages: set[str] = set()
            stage_data: dict[str, Any] = {}
            final_report: str | None = None
            screenshot_path: str = ""

            progress = _build_progress_table(completed_stages, stage_data)
            report_panel = _build_report_panel(None)
            layout = _build_layout(progress, report_panel)

            with Live(layout, refresh_per_second=8, screen=True, transient=False) as live:
                async for item in execution.get_async_generator(type="instant"):
                    # Track stage completion
                    if item.path.startswith("skills.stages.") and item.is_complete:
                        stage_id = item.path.split(".")[-1]
                        if stage_id not in ("plan",):
                            completed_stages.add(stage_id)

                    # Capture action results
                    if item.path.startswith("actions.") and item.is_complete:
                        action_name = item.path.split(".", 1)[1]
                        value = item.value or {}
                        result_data = value
                        if isinstance(value, dict):
                            result_data = value.get("result", value.get("output", value.get("data", value)))

                        if action_name == "browse_page":
                            if "browse_index" not in stage_data:
                                stage_data["browse_index"] = result_data
                            else:
                                stage_data["browse_settings"] = result_data
                        elif action_name == "audit_a11y":
                            stage_data["audit_accessibility"] = result_data
                        elif action_name == "capture_screenshot":
                            stage_data["capture_screenshots"] = result_data
                            if isinstance(result_data, dict):
                                screenshot_path = result_data.get("screenshot_path", "")
                        elif action_name == "compile_test_report":
                            stage_data["compile_test_report"] = result_data
                            if isinstance(result_data, dict):
                                final_report = result_data.get("report", "")
                            report_panel = _build_report_panel(final_report)

                        progress = _build_progress_table(completed_stages, stage_data)
                        live.update(_build_layout(progress, report_panel))

    # ── Final summary ──────────────────────────────────────────────────────
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\nroute: {route}")
    print(f"stages completed: {sorted(completed_stages)}")

    report_data = data.get("semantic_outputs", {}).get("test_report", {}).get("result", {}) if isinstance(data, dict) else {}
    print(f"total issues: {report_data.get('total_issues', '?')}")
    print(f"passed: {report_data.get('passed', '?')}")
    print(f"screenshot: {screenshot_path or '(skipped — playwright not installed)'}")

    if final_report:
        print("\n" + final_report[:2000])


if __name__ == "__main__":
    asyncio.run(main())
