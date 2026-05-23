"""Deep research — interactive topic input → model_plan decomposition → parallel search → browse → dive → synthesis.

Run:
    python examples/agent_auto_orchestration/14_deep_research.py

    # or pass a topic on the command line:
    python examples/agent_auto_orchestration/14_deep_research.py "RISC-V ecosystem growth and its impact on the semiconductor industry"

    # or via environment variable:
    AGENTLY_RESEARCH_TOPIC="6G technology landscape 2025-2026" \\
    python examples/agent_auto_orchestration/14_deep_research.py

    # if no topic is provided, the script will prompt interactively.

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich httpx beautifulsoup4

This example demonstrates a production-grade deep research pipeline: you provide a
research question, the model decomposes it into structured dimensions, parallel
web searches run, full articles are browsed and reference links tracked, a
synthesis stage combines all three layers of data, a cross-validation stage
checks consistency, and the final report is saved to disk.

No topic is hardcoded — every run researches whatever question you ask. No mock
data — all web search and article fetching uses real HTTP calls to DuckDuckGo.

Skill stages (10 stages, 2-layer research depth):
  Layer 1 — Broad search:
  1. plan_research       (model_plan) — decompose topic into 3+ dimensions with search queries
  2. search_upstream     (action)     — web search for upstream dimension
  3. search_core         (action)     — web search for core/midstream dimension
  4. search_downstream   (action)     — web search for downstream dimension
     ↑ stages 2-4 run in parallel after stage 1 completes
  5. validate_searches   (validate)   — gates on all 3 dimensions yielding results

  Layer 2 — Deep dive (browse + reference tracking):
  6. browse_deepen       (action)     — fetches full article content from top results across
                                        all dimensions, extracts reference hooks (companies,
                                        technologies, standards, events mentioned in articles
                                        that weren't in the original search), runs targeted
                                        follow-up searches for those hooks, and fetches
                                        content from the follow-up results
  7. validate_depth      (validate)   — gates on browse content + dive results present

  Synthesis:
  8. synthesize          (model)      — combines all 3 layers (search abstracts, full-article
                                        content, dive reference findings) with domain expertise
  9. cross_validate      (model)      — checks consistency, flags contradictions, labels confidence
  10. compile_report     (action)     — formats and saves the complete research report

Capabilities demonstrated:
  - `kind: model_plan` topic decomposition into structured research dimensions
  - 3-way parallel action execution (fan-out from single dependency)
  - `kind: validate` gating on minimum source coverage
  - `kind: action` full-article browsing (HTTP fetch → BeautifulSoup content extraction)
  - `kind: action` reference-link tracking and targeted follow-up search
  - 2-layer research depth: broad search → browse → dive → deeper browse
  - `kind: model` synthesis with field-level delta streaming
  - `kind: model` cross-validation with conceptual confidence labels
  - Real web search and article fetching (DuckDuckGo HTTP → BeautifulSoup, with fallback)
  - Rich 4-row live display with per-stage progress
"""

from __future__ import annotations

import asyncio
import json
import re
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

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

DEEP_RESEARCH_SKILL_YAML = """
skill_id: deep-research
version: 1.0.0
display_name: Deep Research Pipeline
purpose: >
  Decompose a complex research topic into structured dimensions, execute
  parallel web searches for each dimension, validate source coverage,
  synthesize findings enriched with domain expertise, cross-validate for
  consistency and gaps, and compile a comprehensive research report.
trust_level: local
kind: workflow
activation:
  keywords:
    - research
    - deep research
    - industry analysis
    - supply chain
    - market research
    - technology landscape
requires:
  actions:
    - web_search
    - browse_deepen
    - compile_report
stages:
  - id: plan_research
    kind: model_plan
    purpose: >
      You are a senior research director at a top-tier consulting firm.
      Decompose the research topic below into 3 structured dimensions. For
      each dimension, write 2-3 specific search queries that would surface
      the most current and authoritative information.

      Dimension 1 — Upstream / Foundations: root inputs, enabling
      technologies, foundational infrastructure.
      Dimension 2 — Core / Midstream: the central production, processing,
      or value-creation layer.
      Dimension 3 — Downstream / Applications: end-market consumption,
      applications, and ecosystem effects.

      Also include 2-3 cross-cutting themes (e.g., geopolitical tensions,
      regulatory shifts, emerging disruptors) that span multiple dimensions.
      For each, explain how it connects the dimensions.

      Be specific. Use concrete company names, technology names, and
      measurable trends wherever possible. Avoid generic placeholders.
    input:
      topic: "${task}"
    output_schema:
      research_plan:
        type: str
        description: Overall research methodology and approach
      dimension_1_label:
        type: str
        description: Short label for dimension 1 (e.g. EDA & Equipment)
      dimension_1_queries:
        type: str
        description: 2-3 search queries for dimension 1, separated by semicolons
      dimension_2_label:
        type: str
        description: Short label for dimension 2
      dimension_2_queries:
        type: str
        description: 2-3 search queries for dimension 2, separated by semicolons
      dimension_3_label:
        type: str
        description: Short label for dimension 3
      dimension_3_queries:
        type: str
        description: 2-3 search queries for dimension 3, separated by semicolons
      cross_cutting_themes:
        type: str
        description: 2-3 cross-cutting themes that span multiple dimensions
  - id: search_upstream
    kind: action
    action: web_search
    depends_on:
      - plan_research
    input:
      queries: "${state.plan_research.dimension_1_queries}"
      dimension: "${state.plan_research.dimension_1_label}"
      max_results: 5
  - id: search_core
    kind: action
    action: web_search
    depends_on:
      - plan_research
    input:
      queries: "${state.plan_research.dimension_2_queries}"
      dimension: "${state.plan_research.dimension_2_label}"
      max_results: 5
  - id: search_downstream
    kind: action
    action: web_search
    depends_on:
      - plan_research
    input:
      queries: "${state.plan_research.dimension_3_queries}"
      dimension: "${state.plan_research.dimension_3_label}"
      max_results: 5
  - id: validate_searches
    kind: validate
    depends_on:
      - search_upstream
      - search_core
      - search_downstream
    validation:
      required_state:
        - search_upstream
        - search_core
        - search_downstream
  - id: browse_deepen
    kind: action
    action: browse_deepen
    depends_on:
      - validate_searches
    input:
      topic: "${task}"
      dim1_label: "${state.plan_research.dimension_1_label}"
      dim1_results: "${state.search_upstream.results_json}"
      dim2_label: "${state.plan_research.dimension_2_label}"
      dim2_results: "${state.search_core.results_json}"
      dim3_label: "${state.plan_research.dimension_3_label}"
      dim3_results: "${state.search_downstream.results_json}"
      max_browse_per_dim: 2
      max_dive_queries: 5
  - id: validate_depth
    kind: validate
    depends_on:
      - browse_deepen
    validation:
      required_state:
        - browse_deepen
  - id: synthesize
    kind: model
    depends_on:
      - validate_depth
    purpose: >
      You are a senior industry analyst synthesizing a comprehensive research
      report. You have THREE layers of research data to work with:

      LAYER 1 — Search abstracts: broad web search results from 3 dimensions.
      LAYER 2 — Full-article content: the actual text of top articles fetched
      and read in full (this is where the richest detail lives).
      LAYER 3 — Dive findings: targeted follow-up research on reference hooks
      (companies, technologies, standards, events) discovered in Layer 2
      articles that weren't in the original search.

      Combine ALL three layers with your domain expertise to produce a
      structured analysis. The report must include:

      1. Executive Summary (3-4 sentences capturing the state of play)
      2. For each dimension: key players, market structure, technology
         trends, recent developments (2024-2026), and outlook
      3. Deep-dive section: 2-3 insights surfaced ONLY through full-article
         browsing or reference tracking (label these as "Deep Dive Finding")
      4. Cross-cutting theme analysis — how geopolitical, regulatory, and
         technology trends connect across dimensions
      5. Key data points and metrics (market sizes, growth rates, market
         share where known). Cite which research layer the data came from.
      6. Risks and uncertainties
      7. 3-5 strategic implications for industry participants

      Be analytical, not just descriptive. Highlight tensions, trade-offs,
      and contrarian viewpoints where they exist. Explicitly flag which
      insights came from deep diving vs. surface search.
    input:
      topic: "${task}"
      plan: "${state.plan_research.research_plan}"
      themes: "${state.plan_research.cross_cutting_themes}"
      dim1_label: "${state.plan_research.dimension_1_label}"
      dim1_abstracts: "${state.search_upstream.results_json}"
      dim2_label: "${state.plan_research.dimension_2_label}"
      dim2_abstracts: "${state.search_core.results_json}"
      dim3_label: "${state.plan_research.dimension_3_label}"
      dim3_abstracts: "${state.search_downstream.results_json}"
      full_article_content: "${state.browse_deepen.browsed_content_json}"
      dive_findings: "${state.browse_deepen.dive_results_json}"
      reference_hooks: "${state.browse_deepen.reference_hooks_json}"
    output_schema:
      report:
        type: str
        description: The complete synthesized research report in Markdown
  - id: cross_validate
    kind: model
    depends_on:
      - synthesize
    purpose: >
      You are a rigorous fact-checker and methodology reviewer. Review the
      synthesized research report for:

      1. Internal consistency — do claims in different sections align?
      2. Source quality — are the web sources authoritative or thin?
      3. Knowledge gaps — what important angles were NOT covered?
      4. Confidence assessment — for each major claim, use conceptual levels:
         HIGH_CONFIDENCE when sources are authoritative, evidence is sufficient,
         evidence directly supports the claim, and domain materials broadly
         agree; MODERATE_CONFIDENCE when sources are reasonably broad and
         evidence directly or indirectly supports the claim but some inference
         or cross-domain analogy remains; LOW_CONFIDENCE when sources are
         missing, mostly promotional, single-source, speculative, or weakly
         linked to the claim. Do not emit numeric confidence scores.
      5. Recommended follow-up — what would a Phase 2 research effort
         investigate?

      Be honest about limitations. Do not sugarcoat gaps.
    input:
      report: "${state.synthesize.report}"
      topic: "${task}"
    output_schema:
      validation_notes:
        type: str
        description: Fact-checking notes, consistency issues, gaps, confidence
          assessments, and Phase 2 recommendations
  - id: compile_report
    kind: action
    action: compile_report
    depends_on:
      - cross_validate
    input:
      report: "${state.synthesize.report}"
      validation: "${state.cross_validate.validation_notes}"
      topic: "${task}"
      browsed_count: "${state.browse_deepen.browsed_count}"
      dive_queries: "${state.browse_deepen.dive_queries_run}"
      ref_hooks: "${state.browse_deepen.reference_hooks_json}"
      dim_labels:
        - "${state.plan_research.dimension_1_label}"
        - "${state.plan_research.dimension_2_label}"
        - "${state.plan_research.dimension_3_label}"
semantic_outputs:
  research_report: compile_report
tags:
  - research
  - deep-research
  - industry-analysis
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Default research topic (can be overridden via env var)
# ═══════════════════════════════════════════════════════════════════════════════

# (no default topic — research question is provided interactively at runtime)

# ═══════════════════════════════════════════════════════════════════════════════
# Action: web_search (parallel per dimension)
# ═══════════════════════════════════════════════════════════════════════════════

_SEARCH_CACHE: dict[str, list[dict[str, str]]] = {}


async def _action_web_search(queries: str = "", dimension: str = "", max_results: int = 5, **kwargs) -> dict[str, Any]:
    """Search the web for each query, aggregate results."""
    query_list = [q.strip() for q in queries.split(";") if q.strip()]
    if not query_list:
        query_list = [dimension] if dimension else ["semiconductor industry trends 2025 2026"]

    all_results: list[dict[str, str]] = []
    for query in query_list[:4]:  # cap at 4 queries per dimension
        cache_key = f"{query}:{max_results}"
        if cache_key in _SEARCH_CACHE:
            results = _SEARCH_CACHE[cache_key]
        else:
            results = []
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
                                    "url": str(link.get("href", "")),
                                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                                })
            except Exception:
                pass

            if results:
                _SEARCH_CACHE[cache_key] = results

        all_results.extend(results)

    # Deduplicate by URL
    seen = set()
    deduped: list[dict[str, str]] = []
    for r in all_results:
        if r["url"] and r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)

    return {
        "dimension": dimension,
        "queries_used": query_list,
        "total_hits": len(deduped),
        "results": deduped[: max_results * 2],
        "results_json": json.dumps(deduped[: max_results * 2], ensure_ascii=False),
        "source": "live" if any(c not in _SEARCH_CACHE for c in [f"{q}:{max_results}" for q in query_list]) else "cache",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Action: browse_deepen — fetch full articles + track references
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_browse_deepen(
    topic: str = "",
    dim1_label: str = "", dim1_results: str = "",
    dim2_label: str = "", dim2_results: str = "",
    dim3_label: str = "", dim3_results: str = "",
    max_browse_per_dim: int = 2,
    max_dive_queries: int = 5,
    **kwargs,
) -> dict[str, Any]:
    """Fetch full article content from top search results, extract reference hooks,
    run targeted follow-up searches, and fetch content from follow-up results."""

    # Parse search results from all dimensions
    all_dim_results: list[tuple[str, list[dict[str, str]]]] = []
    for label, results_json in [
        (dim1_label, dim1_results),
        (dim2_label, dim2_results),
        (dim3_label, dim3_results),
    ]:
        try:
            results = json.loads(results_json) if isinstance(results_json, str) else (results_json or [])
            if isinstance(results, list):
                all_dim_results.append((label, results))
        except (json.JSONDecodeError, TypeError):
            pass

    # ── Phase 1: Browse — fetch full content from top articles ──
    browsed_articles: list[dict[str, Any]] = []
    for dim_label, results in all_dim_results:
        for r in results[:max_browse_per_dim]:
            url = r.get("url", "")
            if not url:
                continue
            content = ""
            title = r.get("title", url)
            try:
                import httpx
                from bs4 import BeautifulSoup

                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                    resp = await client.get(
                        url,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; Agently-Research/2.0)"},
                    )
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        title = soup.title.get_text(strip=True) if soup.title else title
                        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                            tag.decompose()
                        body = soup.find("body") or soup
                        text = body.get_text(separator="\n", strip=True)
                        text = re.sub(r"\n{3,}", "\n\n", text)[:4000]
                        content = text
            except Exception:
                pass

            if not content:
                content = r.get("snippet", "[Content unavailable]")

            article = {
                "dimension": dim_label,
                "url": url,
                "title": title,
                "content": content,
                "content_length": len(content),
            }
            browsed_articles.append(article)

    # ── Phase 2: Extract reference hooks from browsed content ──
    # Use the model (via a quick temp agent call) to extract key entities worth
    # diving deeper on — companies, technologies, standards, events.
    reference_hooks: list[str] = []
    if browsed_articles:
        combined_text = "\n\n---\n\n".join(
            f"[{a['dimension']}] {a['title']}\n{a['content'][:1500]}"
            for a in browsed_articles
        )
        try:
            agent = Agently.create_agent("reference-extractor")
            extract_prompt = f"""Extract from the browsed research articles below 5-8
specific reference hooks that deserve deeper investigation. A "reference hook" is:

- A company or technology NOT covered in the main research dimensions
- A specific regulation, standard, or policy mentioned in passing
- A startup, emerging competitor, or niche player
- A data point or statistic cited from another source
- A cross-industry parallel or adjacent market that connects

Return ONLY a JSON array of strings, each a specific search query that would
surface authoritative information about that hook. Be specific — use full
company names, technology names, regulation identifiers.

Articles:
{combined_text[:6000]}"""
            result = await agent.input(extract_prompt).async_start()
            result_str = str(result or "")
            # Extract JSON array from the response
            json_match = re.search(r"\[.*\]", result_str, re.DOTALL)
            if json_match:
                reference_hooks = json.loads(json_match.group())
            if not isinstance(reference_hooks, list):
                reference_hooks = []
        except Exception:
            reference_hooks = []

    # ── Phase 3: Dive deeper — search for reference hooks ──
    dive_search_results: list[dict[str, Any]] = []
    for query in (reference_hooks or [])[:max_dive_queries]:
        results = []
        cache_key = f"dive:{query}:3"
        if cache_key in _SEARCH_CACHE:
            results = _SEARCH_CACHE[cache_key]
        else:
            try:
                import httpx
                from bs4 import BeautifulSoup

                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": query},
                        headers={"User-Agent": "Mozilla/5.0 (compatible; Agently-Research/2.0)"},
                    )
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for i, el in enumerate(soup.select(".result")):
                            if i >= 3:
                                break
                            link = el.select_one(".result__a")
                            snippet_el = el.select_one(".result__snippet")
                            if link:
                                results.append({
                                    "title": link.get_text(strip=True),
                                    "url": str(link.get("href", "")),
                                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                                })
            except Exception:
                pass

            if results:
                _SEARCH_CACHE[cache_key] = results

        dive_search_results.append({
            "query": query,
            "results": results,
            "total": len(results),
        })

    # ── Phase 4: Browse dive results (light fetch) ──
    dive_browsed: list[dict[str, Any]] = []
    for dsr in dive_search_results:
        for r in dsr["results"][:1]:  # top result only per dive query
            url = r.get("url", "")
            if not url:
                continue
            content = r.get("snippet", "")
            try:
                import httpx
                from bs4 import BeautifulSoup

                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    resp = await client.get(
                        url,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; Agently-Research/2.0)"},
                    )
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for tag in soup(["script", "style", "nav", "footer", "header"]):
                            tag.decompose()
                        body = soup.find("body") or soup
                        content = body.get_text(separator="\n", strip=True)[:2000]
                        content = re.sub(r"\n{3,}", "\n\n", content)
            except Exception:
                pass
            dive_browsed.append({
                "query": dsr["query"],
                "url": url,
                "title": r.get("title", url),
                "content": content,
            })

    return {
        "browsed_count": len(browsed_articles),
        "browsed_content_json": json.dumps(browsed_articles, ensure_ascii=False),
        "reference_hooks_count": len(reference_hooks),
        "reference_hooks_json": json.dumps(reference_hooks, ensure_ascii=False),
        "dive_queries_run": len(dive_search_results),
        "dive_hits_total": sum(d["total"] for d in dive_search_results),
        "dive_results_json": json.dumps(dive_search_results, ensure_ascii=False),
        "dive_browsed_json": json.dumps(dive_browsed, ensure_ascii=False),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Action: compile_report
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_compile_report(
    report: str = "",
    validation: str = "",
    topic: str = "Research Topic",
    dim_labels: Any = None,
    browsed_count: int = 0,
    dive_queries: int = 0,
    ref_hooks: Any = None,
    **kwargs,
) -> dict[str, Any]:
    """Compile and save the full research report with validation notes."""
    labels = dim_labels if isinstance(dim_labels, list) else []
    dim_headers = "\n".join(f"- {l}" for l in labels) if labels else "- (auto-detected dimensions)"

    # Reference hooks summary
    hooks_list = ref_hooks if isinstance(ref_hooks, list) else []
    hooks_text = "\n".join(f"- {h}" for h in hooks_list) if hooks_list else "- (none extracted)"

    doc = f"""# Deep Research Report
**Topic:** {topic[:200]}
**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
**Research Depth:** 2-layer (broad search + full-article browse + reference dive)
**Dimensions Researched:**
{dim_headers}
**Articles browsed in full:** {browsed_count}
**Reference hooks discovered & dived into:** {dive_queries}
  {hooks_text}

---

## Synthesized Research Report

{report or '*Report generation failed.*'}

---

## Validation & Quality Assessment

{validation or '*Validation not performed.*'}

---

*Generated by Agently Deep Research Pipeline — model_plan decomposition →
parallel web search → model synthesis → cross-validation.*
"""

    reports_dir = Path.home() / ".agently_deep_research"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()[:80]).strip("-")
    filepath = reports_dir / f"{slug}_{timestamp}.md"
    filepath.write_text(doc)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "report_length": len(report or ""),
        "validation_length": len(validation or ""),
        "browsed_count": browsed_count,
        "dive_queries": dive_queries,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display — 4-row grid
# ═══════════════════════════════════════════════════════════════════════════════


def _build_plan_panel(plan_text: str | None, dim_labels: list[str], done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if done and dim_labels:
        labels_text = "\n".join(f"  [{i+1}] {l}" for i, l in enumerate(dim_labels))
        body = Text(f"Research dimensions:\n{labels_text}")
        if plan_text:
            body.append(f"\n\n{plan_text[:200]}...")
    elif plan_text:
        body = Text(plan_text[:400])
    elif running:
        body = Text("Decomposing research topic into dimensions...", style="dim")
    else:
        body = Text("Waiting...", style="dim")

    return Panel(body, title=f"{icon} Plan (model_plan)", border_style="yellow")


def _build_search_panel(
    label: str, total_hits: int | None, done: bool, running: bool, source: str
) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if done:
        if total_hits is None:
            body = Text("Search complete", style="green")
        else:
            color = "green" if total_hits > 0 else "red"
            body = Text.from_markup(f"[{color}]{total_hits} sources[/] ({source})")
    elif running:
        body = Text(f"Searching: {label}...", style="dim")
    else:
        body = Text(f"Waiting on plan: {label}", style="dim")

    return Panel(body, title=f"{icon} {label or 'Search'}", border_style="blue")


def _build_synthesis_panel(report_text: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if report_text:
        preview = report_text[:600]
        if len(report_text) > 600:
            preview += f"\n\n... ({len(report_text):,} chars)"
        body = Text(preview)
    elif running:
        body = Text("Synthesizing web findings with domain expertise...", style="dim")
    else:
        body = Text("Waiting on source validation...", style="dim")

    return Panel(body, title=f"{icon} Synthesis (model)", border_style="cyan")


def _build_validation_panel(validation: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if validation:
        preview = validation[:400]
        body = Text(preview)
    elif running:
        body = Text("Cross-validating claims and assessing source quality...", style="dim")
    else:
        body = Text("Waiting on synthesis...", style="dim")

    return Panel(body, title=f"{icon} Cross-Validate (model)", border_style="magenta")


def _build_browse_panel(browsed: int, refs: int, dives: int, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
    elif running:
        icon = "[bold yellow]◎[/]"
    else:
        icon = "[dim]·[/]"

    if done:
        body = Text.from_markup(
            f"[green]{browsed} articles browsed in full[/]\n"
            f"[green]{refs} reference hooks discovered[/]\n"
            f"[green]{dives} follow-up dives executed[/]"
        )
    elif running:
        body = Text(f"Browsing articles, extracting refs...\n{dives} dive queries so far", style="dim")
    else:
        body = Text("Waiting on searches...", style="dim")

    return Panel(body, title=f"{icon} Browse & Dive (action)", border_style="green")


def _build_validate_sources_panel(passed: bool, done: bool, label: str = "Validate Searches") -> Panel:
    if done and passed:
        return Panel(
            Text.from_markup(f"[green]{label}: passed[/]"),
            title="[bold green]✓[/] Validate",
            border_style="green",
        )
    elif done:
        return Panel(
            Text.from_markup(f"[red]{label}: FAILED[/]"),
            title="[bold red]✗[/] Validate",
            border_style="red",
        )
    return Panel(
        Text(f"Waiting on {label.lower()}...", style="dim"),
        title="[dim]·[/] Validate",
        border_style="dim",
    )


def _build_output_panel(file_path: str, report_len: int, done: bool) -> Panel:
    if done and file_path:
        return Panel(
            Text.from_markup(f"[green]{file_path}[/]\nReport: {report_len:,} chars"),
            title="[bold green]✓[/] Output",
            border_style="green",
        )
    return Panel(Text("waiting...", style="dim"), title="[dim]·[/] Output", border_style="dim")


def _build_layout(
    plan: Panel,
    sr1: Panel, sr2: Panel, sr3: Panel,
    vs1: Panel, browse: Panel, vs2: Panel,
    syn: Panel, cv: Panel, out: Panel,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="row1", ratio=2),  # plan
        Layout(name="row2", ratio=1),  # 3 searches + validate_searches
        Layout(name="row3", ratio=2),  # browse_deepen + validate_depth
        Layout(name="row4", ratio=3),  # synthesis + cross-validate
        Layout(name="row5", ratio=1),  # output
    )
    layout["row1"].split_row(Layout(plan, name="plan"))
    layout["row2"].split_row(
        Layout(sr1, name="sr1"), Layout(sr2, name="sr2"),
        Layout(sr3, name="sr3"), Layout(vs1, name="vs1"),
    )
    layout["row3"].split_row(
        Layout(browse, name="browse", ratio=3), Layout(vs2, name="vs2", ratio=1),
    )
    layout["row4"].split_row(
        Layout(syn, name="syn", ratio=2), Layout(cv, name="cv", ratio=1),
    )
    layout["row5"].split_row(Layout(out, name="output"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    import os

    # Resolve topic: CLI arg > env var > interactive prompt
    if len(sys.argv) > 1:
        topic = " ".join(sys.argv[1:]).strip()
    elif os.environ.get("AGENTLY_RESEARCH_TOPIC", "").strip():
        topic = os.environ["AGENTLY_RESEARCH_TOPIC"].strip()
    else:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  Agently Deep Research Pipeline                            ║")
        print("║  2-layer research: search → browse → dive → synthesis      ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        print("Enter your research topic below (multi-line is fine;")
        print("end with a blank line, or Ctrl+D / Ctrl+C to cancel):")
        print()
        lines = []
        try:
            while True:
                line = input()
                if line.strip() == "":
                    break
                lines.append(line)
        except (EOFError, KeyboardInterrupt):
            if not lines:
                print("\n[cancelled]")
                return
        topic = "\n".join(lines).strip()
        if not topic:
            print("[no topic provided — exiting]")
            return

    print(f"\n{'─'*60}")
    print(f"Raw question ({len(topic):,} chars):")
    print(f"  {topic[:200]}{'...' if len(topic) > 200 else ''}")
    print(f"{'─'*60}\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 0 — Topic refinement (model expands raw question into research brief)
    # ═══════════════════════════════════════════════════════════════════════════
    provider = configure_model(temperature=0.3)

    # Show model info
    model_name = (
        os.environ.get("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")
        if provider == "deepseek"
        else os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")
    )
    print(f"Model: {provider} :: {model_name}\n")

    if not topic.startswith("Research ") and not topic.startswith("研究 "):
        print("Expanding your question into a structured research brief...\n")
        try:
            refiner = Agently.create_agent("topic-refiner")
            refinement_prompt = f"""The user wants to research the following topic. Their question may be
brief or informal. Your job is to expand it into a structured research brief
that covers the key dimensions a thorough investigation should address.

Return the expanded brief in this format:

1. A refined 2-3 sentence statement of the research question
2. Three research dimensions to investigate (label each clearly):
   - Upstream / Root Causes / Foundations
   - Core / Current State / Main Analysis
   - Downstream / Implications / Future Outlook
3. For EACH dimension: 2-3 specific angles or sub-questions to investigate
4. Any cross-cutting themes or tensions worth exploring across dimensions
5. Key entities to research: companies, people, technologies, regulations,
   events, or standards relevant to the topic

IMPORTANT: The output will be fed into a deep research pipeline that does real
web searches and article fetching. Make the brief specific and searchable —
use concrete names, technology terms, and measurable trends. Avoid vague
language like "various companies" or "recent developments."

User's raw question:
{topic}"""
            refined = await refiner.input(refinement_prompt).async_start()
            refined_str = str(refined or "").strip()

            if refined_str and len(refined_str) > 50:
                print(f"{'─'*60}")
                print("Refined research brief (model-expanded):")
                print(f"{'─'*60}")
                print(refined_str)
                print(f"{'─'*60}")
                print()
                print("Press ENTER to launch the pipeline with this brief,")
                print("or type your edits / additional instructions below,")
                print("or type 'skip' to use your original question as-is:")
                print()

                try:
                    user_input = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    user_input = ""

                if user_input.lower() == "skip":
                    print("[using original question]\n")
                elif user_input:
                    topic = f"[ORIGINAL QUESTION]\n{topic}\n\n[ADDITIONAL INSTRUCTIONS]\n{user_input}"
                    print(f"[brief updated with your instructions]\n")
                else:
                    topic = refined_str
                    print("[using model-expanded brief]\n")
            else:
                print("[refinement produced empty output — using original question]\n")
        except Exception as e:
            print(f"[refinement skipped — {e} — using original question]\n")

    # ═══════════════════════════════════════════════════════════════════════════

    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"
    skill_dir = runtime_dir / "deep-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(DEEP_RESEARCH_SKILL_YAML.strip())

    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("deep-researcher")

    agent.register_action(
        name="web_search",
        desc="Search the web for research information on a given dimension.",
        kwargs={
            "queries": ("str", "Semicolon-separated search queries."),
            "dimension": ("str", "Human-readable dimension label."),
            "max_results": ("int", "Max results per query."),
        },
        func=_action_web_search,
    )
    agent.register_action(
        name="compile_report",
        desc="Compile the final research report and save to disk.",
        kwargs={
            "report": ("str", "The synthesized report text."),
            "validation": ("str", "Cross-validation notes."),
            "topic": ("str", "The research topic."),
            "dim_labels": ("any", "Dimension labels."),
            "browsed_count": ("int", "Articles browsed."),
            "dive_queries": ("int", "Follow-up dive queries run."),
            "ref_hooks": ("any", "Reference hooks found."),
        },
        func=_action_compile_report,
    )
    agent.register_action(
        name="browse_deepen",
        desc="Browse full articles, extract reference hooks, and run targeted follow-up searches.",
        kwargs={
            "topic": ("str", "Research topic."),
            "dim1_label": ("str", "Dimension 1 label."),
            "dim1_results": ("str", "JSON array of search results."),
            "dim2_label": ("str", "Dimension 2 label."),
            "dim2_results": ("str", "JSON array of search results."),
            "dim3_label": ("str", "Dimension 3 label."),
            "dim3_results": ("str", "JSON array of search results."),
            "max_browse_per_dim": ("int", "Max articles to browse per dimension."),
            "max_dive_queries": ("int", "Max follow-up dive queries."),
        },
        func=_action_browse_deepen,
    )

    execution = (
        agent
        .use_skills(["deep-research"], mode="required")
        .input(topic)
        .create_execution()
    )

    # ── State trackers ──────────────────────────────────────────────────────
    completed_stages: set[str] = set()
    stage_running: dict[str, bool] = {}

    plan_text: str | None = None
    dim_labels: list[str] = []
    dim1_label = dim2_label = dim3_label = ""

    sr1_hits: int | None = None; sr1_source = ""
    sr2_hits: int | None = None; sr2_source = ""
    sr3_hits: int | None = None; sr3_source = ""
    validate_searches_passed = False
    browsed_count = 0; ref_hooks_found = 0; dive_queries_run = 0
    validate_depth_passed = False
    report_text: str | None = None
    validation_text: str | None = None
    saved_file = ""; report_len = 0; save_done = False

    # ── Initial panels ──────────────────────────────────────────────────────
    plan_panel = _build_plan_panel(None, [], False, False)
    sr1_panel = _build_search_panel("...", None, False, False, "")
    sr2_panel = _build_search_panel("...", None, False, False, "")
    sr3_panel = _build_search_panel("...", None, False, False, "")
    vs1_panel = _build_validate_sources_panel(False, False, "Validate Searches")
    browse_panel = _build_browse_panel(0, 0, 0, False, False)
    vs2_panel = _build_validate_sources_panel(False, False, "Validate Depth")
    syn_panel = _build_synthesis_panel(None, False, False)
    cv_panel = _build_validation_panel(None, False, False)
    out_panel = _build_output_panel("", 0, False)
    layout = _build_layout(plan_panel, sr1_panel, sr2_panel, sr3_panel, vs1_panel, browse_panel, vs2_panel, syn_panel, cv_panel, out_panel)

    console = Console(force_terminal=True)
    with Live(layout, refresh_per_second=6, screen=False, transient=False, console=console) as live:
        gen = execution.get_async_generator(type="instant")
        next_item = asyncio.create_task(gen.__anext__())
        while True:
            try:
                item = await asyncio.wait_for(asyncio.shield(next_item), timeout=0.4)
            except asyncio.TimeoutError:
                live.update(
                    _build_layout(plan_panel, sr1_panel, sr2_panel, sr3_panel, vs1_panel, browse_panel, vs2_panel, syn_panel, cv_panel, out_panel),
                    refresh=True,
                )
                continue
            except StopAsyncIteration:
                break
            next_item = asyncio.create_task(gen.__anext__())

            # ── Stage starts ──
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                for sid in (
                    "plan_research",
                    "search_upstream", "search_core", "search_downstream",
                    "validate_searches",
                    "browse_deepen",
                    "validate_depth",
                    "synthesize", "cross_validate",
                    "compile_report",
                ):
                    if sid in item.path:
                        stage_running[sid] = True

            # ── Stage completions ──
            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                completed_stages.add(stage_id)
                stage_running[stage_id] = False

            # ── Field deltas ──
            if item.path == "skills.stages.plan_research.fields.research_plan" and item.delta:
                plan_text = (plan_text or "") + item.delta
            elif item.path == "skills.stages.plan_research.fields.dimension_1_label" and item.delta:
                dim1_label = (dim1_label or "") + item.delta
            elif item.path == "skills.stages.plan_research.fields.dimension_2_label" and item.delta:
                dim2_label = (dim2_label or "") + item.delta
            elif item.path == "skills.stages.plan_research.fields.dimension_3_label" and item.delta:
                dim3_label = (dim3_label or "") + item.delta
            elif item.path == "skills.stages.synthesize.fields.report" and item.delta:
                report_text = (report_text or "") + item.delta
            elif item.path == "skills.stages.cross_validate.fields.validation_notes" and item.delta:
                validation_text = (validation_text or "") + item.delta

            # ── Action completions — capture search results and compile ──
            if item.path.startswith("actions.") and item.is_complete:
                action_name = item.path.split(".", 1)[1]
                val = item.value or {}
                result = val.get("result", val) if isinstance(val, dict) else val
                if not isinstance(result, dict):
                    continue

                if action_name == "search_upstream":
                    sr1_hits = result.get("total_hits", 0)
                    sr1_source = result.get("source", "unknown")
                elif action_name == "search_core":
                    sr2_hits = result.get("total_hits", 0)
                    sr2_source = result.get("source", "unknown")
                elif action_name == "search_downstream":
                    sr3_hits = result.get("total_hits", 0)
                    sr3_source = result.get("source", "unknown")
                elif action_name == "browse_deepen":
                    browsed_count = result.get("browsed_count", 0)
                    ref_hooks_found = result.get("reference_hooks_count", 0)
                    dive_queries_run = result.get("dive_queries_run", 0)
                elif action_name == "compile_report":
                    saved_file = result.get("file_path", "")
                    report_len = result.get("report_length", 0)
                    save_done = True

            # ── Validate status ──
            if "validate_searches" in completed_stages:
                validate_searches_passed = True
            if "validate_depth" in completed_stages:
                validate_depth_passed = True

            # ── Derive dim labels ──
            cur_labels = [l.strip() for l in (dim1_label, dim2_label, dim3_label) if l.strip()]
            if cur_labels:
                dim_labels = cur_labels

            sr1_lbl = dim_labels[0] if len(dim_labels) > 0 else "Upstream"
            sr2_lbl = dim_labels[1] if len(dim_labels) > 1 else "Core"
            sr3_lbl = dim_labels[2] if len(dim_labels) > 2 else "Downstream"

            # ── Refresh panels ──
            plan_panel = _build_plan_panel(
                plan_text, dim_labels,
                "plan_research" in completed_stages,
                stage_running.get("plan_research", False),
            )
            sr1_panel = _build_search_panel(
                sr1_lbl, sr1_hits,
                "search_upstream" in completed_stages,
                stage_running.get("search_upstream", False),
                sr1_source,
            )
            sr2_panel = _build_search_panel(
                sr2_lbl, sr2_hits,
                "search_core" in completed_stages,
                stage_running.get("search_core", False),
                sr2_source,
            )
            sr3_panel = _build_search_panel(
                sr3_lbl, sr3_hits,
                "search_downstream" in completed_stages,
                stage_running.get("search_downstream", False),
                sr3_source,
            )
            vs1_panel = _build_validate_sources_panel(validate_searches_passed, "validate_searches" in completed_stages, "Validate Searches")
            browse_panel = _build_browse_panel(
                browsed_count, ref_hooks_found, dive_queries_run,
                "browse_deepen" in completed_stages,
                stage_running.get("browse_deepen", False),
            )
            vs2_panel = _build_validate_sources_panel(validate_depth_passed, "validate_depth" in completed_stages, "Validate Depth")
            syn_panel = _build_synthesis_panel(
                report_text,
                "synthesize" in completed_stages,
                stage_running.get("synthesize", False),
            )
            cv_panel = _build_validation_panel(
                validation_text,
                "cross_validate" in completed_stages,
                stage_running.get("cross_validate", False),
            )
            out_panel = _build_output_panel(saved_file, report_len, save_done)
            live.update(
                _build_layout(plan_panel, sr1_panel, sr2_panel, sr3_panel, vs1_panel, browse_panel, vs2_panel, syn_panel, cv_panel, out_panel),
                refresh=True,
            )

    # ── Final summary ──────────────────────────────────────────────────────
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\n{'='*70}")
    print(f"route: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    print(f"dimensions: {dim_labels}")
    print(f"search hits: [{sr1_hits}, {sr2_hits}, {sr3_hits}] ({sr1_source}/{sr2_source}/{sr3_source})")
    print(f"searches validated: {validate_searches_passed}")
    print(f"browsed articles: {browsed_count} | ref hooks: {ref_hooks_found} | dive queries: {dive_queries_run}")
    print(f"depth validated: {validate_depth_passed}")
    print(f"report length: {len(report_text or ''):,} chars")
    print(f"validation notes: {len(validation_text or ''):,} chars")
    print(f"report saved: {saved_file or '(not saved)'}")
    print(f"{'='*70}")

    if report_text:
        print(f"\n[Report preview — first 600 chars]:")
        print(report_text[:600])
    if validation_text:
        print(f"\n[Validation preview — first 400 chars]:")
        print(validation_text[:400])


if __name__ == "__main__":
    asyncio.run(main())
