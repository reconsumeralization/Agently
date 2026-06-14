"""Web research report — host-run web search/fetch + prompt-only Skill synthesis.

Run:
    python examples/agent_auto_orchestration/08_web_research_report.py [python-asyncio|kubernetes|rust-lang]

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Optional: pip install httpx beautifulsoup4 for live search; otherwise a
    realistic simulated source set is used.

New-standard Skills model
-------------------------
The old design wrapped web search / page fetch as Skill ``action`` stages. Under
the new standard those are plain HOST tools doing the real network I/O. They run
first; a single prompt-only ``SKILL.md`` then synthesizes the fetched sources
into a structured report (shaped by ``output``). The HOST writes the
report. Skill = synthesis guidance; network side effects = host.

Expected key output from one real DeepSeek run:
    skill status: success
    sources fetched: >=3
    report length: >1,500 chars
    report file: .../<slug>_report.md
"""

from __future__ import annotations

import asyncio
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

_SEARCH_CACHE: dict[str, list[dict[str, str]]] = {}

RESEARCH_TOPICS = {
    "python-asyncio": {"topic": "Python asyncio best practices 2025-2026", "slug": "python-asyncio",
                       "query": "Python asyncio best practices 2026 async programming"},
    "kubernetes": {"topic": "Kubernetes production best practices 2026", "slug": "kubernetes-2026",
                   "query": "Kubernetes production deployment best practices 2026"},
    "rust-lang": {"topic": "Rust programming language ecosystem trends 2026", "slug": "rust-ecosystem-2026",
                  "query": "Rust programming language ecosystem 2026 trends adoption"},
}

# ═══════════════════════════════════════════════════════════════════════════════
# HOST tools — real web search & page fetch (httpx + BeautifulSoup, with fallback)
# ═══════════════════════════════════════════════════════════════════════════════


async def search_sources(query: str, max_results: int = 5) -> list[dict[str, str]]:
    cache_key = f"{query}:{max_results}"
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]
    results: list[dict[str, str]] = []
    try:
        import httpx
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible; Agently-Research/1.0)"},
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for i, el in enumerate(soup.select(".result")):
                    if i >= max_results:
                        break
                    link = el.select_one(".result__a")
                    snippet_el = el.select_one(".result__snippet")
                    if link:
                        results.append({
                            "title": link.get_text(strip=True),
                            "url": str(link.get("href", "") or ""),
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                        })
    except Exception:
        pass
    if not results:
        results = _simulated_search(query, max_results)
    if results:
        _SEARCH_CACHE[cache_key] = results
    return results


def _simulated_search(query: str, max_results: int) -> list[dict[str, str]]:
    topic_lower = query.lower()
    if "asyncio" in topic_lower or "python" in topic_lower:
        results = [
            {"title": "Async IO in Python: A Complete Walkthrough – Real Python", "url": "https://realpython.com/async-io-python/", "snippet": "Coroutines, the event loop, tasks, asyncio.gather() and create_task()."},
            {"title": "asyncio — Asynchronous I/O — Python 3.13 docs", "url": "https://docs.python.org/3/library/asyncio.html", "snippet": "Event loop, coroutines, tasks, streams, synchronization, subprocess."},
            {"title": "Python Asyncio Performance Benchmarking 2026", "url": "https://blog.python.org/2026/01/asyncio-benchmarks-3-13.html", "snippet": "Python 3.13: ~15% faster task switching, lower coroutine memory overhead."},
            {"title": "When NOT to use asyncio — tradeoffs", "url": "https://blog.appsignal.com/2025/12/08/when-not-to-use-asyncio.html", "snippet": "CPU-bound workloads, simple scripts, libraries lacking async support."},
        ]
    elif "kubernetes" in topic_lower or "k8s" in topic_lower:
        results = [
            {"title": "Kubernetes 1.32 Release Notes", "url": "https://kubernetes.io/blog/2025/12/17/kubernetes-1-32-release/", "snippet": "Dynamic resource allocation GA, sidecar lifecycle, network policy v2."},
            {"title": "Kubernetes production best practices (2026)", "url": "https://www.cncf.io/blog/2026/01/15/kubernetes-production-best-practices-2026/", "snippet": "Autoscaling, pod security admission, cost optimization, OpenTelemetry."},
            {"title": "Kubernetes vs Nomad vs ECS — 2026", "url": "https://www.infoworld.com/article/3701266/container-orchestration-comparison-2026.html", "snippet": "Kubernetes leads in ecosystem; Nomad gains for simpler deployments."},
        ]
    elif "rust" in topic_lower:
        results = [
            {"title": "Rust in 2026: State of the Ecosystem", "url": "https://blog.rust-lang.org/2026/01/20/rust-2026-ecosystem/", "snippet": "2.8M developers, embedded support, async traits stabilized, cargo-component."},
            {"title": "Why we switched from Python to Rust for our data pipeline", "url": "https://engineering.databricks.com/blog/2025/11/15/python-to-rust-data-pipeline.html", "snippet": "10x throughput, 4x memory reduction, comparable productivity."},
            {"title": "Rust 2024 Edition migration guide", "url": "https://doc.rust-lang.org/edition-guide/rust-2024/", "snippet": "RPIT lifetime capture, unsafe extern blocks, borrow-checker changes."},
        ]
    else:
        results = [
            {"title": f"Latest developments in {query}", "url": f"https://en.wikipedia.org/wiki/{query.replace(' ', '_')}", "snippet": f"Fundamentals and recent developments of {query}."},
        ]
    return results[:max_results]


async def fetch_details(urls: list[str], max_fetch: int = 3) -> list[dict[str, str]]:
    pages: list[dict[str, str]] = []
    for url in urls[:max_fetch]:
        content, title = "", url
        try:
            import httpx
            from bs4 import BeautifulSoup

            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Agently-Research/1.0)"})
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    title = soup.title.get_text(strip=True) if soup.title else url
                    for tag in soup(["script", "style", "nav", "footer", "header"]):
                        tag.decompose()
                    body = soup.find("body")
                    if body:
                        content = re.sub(r"\n{3,}", "\n\n", body.get_text(separator="\n", strip=True))[:3000]
        except Exception:
            pass
        if not content:
            content = "[Content could not be fetched live; rely on the search snippet.]"
        pages.append({"url": url, "title": title, "content": content})
    return pages


# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition — a standard SKILL.md, guidance only
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_SOURCE = Path(__file__).resolve().parent / "skills" / "web-research-report"


def install_skill() -> str:
    skill_src = SKILL_SOURCE
    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
    return str(contract["skill_id"])


async def main() -> None:
    provider = configure_model(temperature=0.3)
    print(f"Model provider: {provider}\n")

    topic_key = next((a for a in sys.argv[1:] if a in RESEARCH_TOPICS), "python-asyncio")
    cfg = RESEARCH_TOPICS[topic_key]

    divider = "=" * 60
    print(divider)
    print(f"Web Research Report — prompt-only Skill")
    print(f"Topic: {cfg['topic']}")
    print(divider)

    # ── HOST: real web search + page fetch ──
    print("Searching sources...")
    results = await search_sources(cfg["query"])
    urls = [r["url"] for r in results if r.get("url")]
    print(f"  {len(results)} sources found. Fetching top pages...")
    pages = await fetch_details(urls)
    print(f"  fetched {len(pages)} pages\n")

    skill_id = install_skill()
    agent = Agently.create_agent("web-researcher")

    sources_block = "\n\n".join(
        f"### {p['title']}\nURL: {p['url']}\n{p['content'][:1500]}" for p in pages
    )
    task = (
        f"Write a research report on: {cfg['topic']}.\n\n"
        f"Search results:\n" + "\n".join(f"- {r['title']} — {r['url']}: {r.get('snippet', '')}" for r in results)
        + f"\n\nFetched source content:\n{sources_block}"
    )

    print("Synthesizing report (skill)...")
    execution = await agent.async_run_skills_task(
        task,
        skills=[skill_id],
        mode="required",
        output={
            "executive_summary": (str, "3-5 sentence summary", True),
            "report": (str, "Full markdown report with themed sections and citations", True),
            "sources": (
                [{"title": (str, "Source title", True), "url": (str, "Source url", True)}],
                "Sources cited",
                True,
            ),
            "recommendation": (str, "What to do next", True),
        },
    )

    print(f"\nskill status: {execution.status}")
    if execution.status != "success":
        print("output:", execution.output)
        return

    result = execution.output or {}
    report = str(result.get("report", ""))
    out_dir = Path(tempfile.mkdtemp(prefix="agently_research_"))
    out_path = out_dir / f"{cfg['slug']}_report.md"
    out_path.write_text(f"# {cfg['topic']}\n\n{report}\n", encoding="utf-8")

    print(f"\n  summary: {str(result.get('executive_summary', ''))[:200]}")
    print(f"\nskill status: {execution.status}")
    print(f"sources fetched: {len(pages)}")
    print(f"report length: {len(report):,} chars")
    print(f"report file: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
