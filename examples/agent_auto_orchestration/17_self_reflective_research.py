"""Self-reflective research — research → evaluate → reflect → improve → repeat.

Run:
    python examples/agent_auto_orchestration/17_self_reflective_research.py

    # pass a topic on the command line:
    python examples/agent_auto_orchestration/17_self_reflective_research.py "DePIN projects 2025"

    # specify output language:
    python examples/agent_auto_orchestration/17_self_reflective_research.py --lang zh "AI agent frameworks"

    # pass topic via environment variable:
    AGENTLY_RESEARCH_TOPIC="RISC-V ecosystem" python examples/agent_auto_orchestration/17_self_reflective_research.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich httpx beautifulsoup4

How it works:
  1. RESEARCH  — model-driven agentic research (same core as example 15)
  2. EVALUATE  — model assesses report quality across 6 dimensions (same
                 framework as example 16), using conceptual levels
  3. REFLECT   — model decides: is quality sufficient? If not, what specific
                 gaps need filling?
  4. GAP-FILL  — targeted re-research on identified gaps (new searches +
                 targeted browsing)
  5. RE-SYNTH  — produce improved report incorporating gap-fill findings
  6. RE-EVAL   — final evaluation comparing v1 vs v2
  7. COMPILE   — save final report + reflection trail to disk

  The model can trigger up to 2 reflection rounds. Each round evaluates
  the previous output, identifies specific weaknesses, and performs
  targeted research to address them. The final output includes a
  reflection trail showing how the report evolved.

Skill stages (7):
  1. initial_research   (action)     — full agentic research (same as ex.15 core)
  2. evaluate_report    (model)      — 6-dimension quality evaluation
  3. reflect_and_plan   (model_plan) — decide: sufficient? or gaps to fill?
  4. gap_fill_research  (action)     — targeted research on identified gaps
  5. re_synthesize      (model)      — produce improved v2 report
  6. final_evaluate     (model)      — compare v1 vs v2, final level
  7. compile_report     (action)     — save report + reflection trail
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
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
from rich.table import Table
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

SELF_REFLECTIVE_RESEARCH_SKILL_YAML = """
skill_id: self-reflective-research
version: 1.0.0
display_name: Self-Reflective Research Pipeline
purpose: >
  Research → Evaluate → Reflect → Improve → Repeat. The model researches a
  topic, evaluates its own output against quality criteria, identifies gaps,
  performs targeted re-research, and produces an improved report. Up to 2
  reflection rounds. Fully generic — works on any research topic.
trust_level: local
kind: workflow
activation:
  keywords:
    - research
    - reflect
    - self-improve
    - iterative research
    - optimize
requires:
  actions:
    - initial_research
    - gap_fill_research
    - compile_report
stages:
  - id: initial_research
    kind: action
    action: initial_research
    input:
      topic: "${task}"
  - id: evaluate_report
    kind: model
    depends_on:
      - initial_research
    purpose: >
      You are a rigorous research quality auditor. Evaluate the research report
      below against its original topic across 6 dimensions. Use conceptual
      quality levels instead of numeric scores.

      DIMENSIONS:
      1. Content Relevance — signal-to-noise ratio. Is every substantive section
         actually about the topic? Flag off-topic or tangential content.
      2. Coverage Completeness — are all claimed dimensions adequately covered?
         Are there obvious missing angles?
      3. Source & Data Authority — are sources reputable? Are claims backed by
         identifiable sources?
      4. Depth Balance — do dimensions that need deep treatment get it? Is
         variation intentional or arbitrary?
      5. Internal Consistency — any contradictions? Do sections align?
      6. Decision Quality — were research methodology decisions sound? If a
         dimension needed DEEP_DIVE treatment but nothing was done, flag it.

      Level definitions:
      - EXCELLENT: strong evidence, direct relevance, adequate coverage, and no
        material unresolved issue for the claimed scope.
      - ADEQUATE: usable with limitations; evidence mostly supports the claim,
        but some gaps or weaker source quality remain.
      - WEAK: materially incomplete, weakly sourced, generic, or only partially
        relevant; requires substantial remediation before use.
      - FAILED: absent, off-topic, unsupported, contradictory, or misleading.

      For each dimension provide: level, 1-3 specific issues found,
      and a 1-sentence assessment.

      Also provide an overall conceptual level using the same labels.

      Be honest. If the report is poor, say so. If there are fundamental
      relevance problems, call them out.
    input:
      topic: "${task}"
      report: "${state.initial_research.report_text}"
      decision_log: "${state.initial_research.decision_log_json}"
      dim_count: "${state.initial_research.dim_count}"
      browsed_count: "${state.initial_research.browsed_count}"
      dive_count: "${state.initial_research.dive_count}"
    output_schema:
      dimension_scores_json:
        type: str
        description: >
          JSON object. Keys: "Content Relevance", "Coverage Completeness",
          "Source & Data Authority", "Depth Balance", "Internal Consistency",
          "Decision Quality". Each value: {"level": "EXCELLENT|ADEQUATE|WEAK|FAILED",
          "issues": ["specific issue", ...], "assessment": "1 sentence"}
      overall_level:
        type: str
        description: Overall quality level. One of EXCELLENT, ADEQUATE, WEAK, FAILED.
      critical_gaps:
        type: str
        description: >
          JSON array of the most critical gaps to address. Each:
          {"gap": "description", "dimension": "which dimension", "severity": "CRITICAL|MAJOR",
           "suggested_queries": ["search query 1", "search query 2"]}
          Max 5 gaps. Only include gaps that NEW RESEARCH could actually fill.
  - id: reflect_and_plan
    kind: model_plan
    depends_on:
      - evaluate_report
    purpose: >
      You are a research director reviewing your team's first draft. Based on
      the evaluation levels and identified gaps, decide:

      1. Is the report quality sufficient to ship? EXCELLENT and ADEQUATE are
         typically shippable unless CRITICAL gaps remain; WEAK and FAILED need
         reflection when new research or filtering can realistically improve them.
      2. If not sufficient: which gaps should be addressed in a reflection round?
         Prioritize gaps where targeted re-research would make the biggest
         difference. Skip gaps that are inherent limitations (e.g., "no
         primary sources exist for this niche topic").
      3. For each gap to address: provide specific search queries that would
         yield relevant, high-quality sources.

      IMPORTANT: If the critical issue is content relevance (the report
      contains off-topic material), the fix is NOT more research — it's
      better filtering in synthesis. Flag this distinction clearly.

      If the report is already EXCELLENT or ADEQUATE and has no CRITICAL gaps,
      recommend skipping reflection and proceeding directly to compilation.
    input:
      topic: "${task}"
      overall_level: "${state.evaluate_report.overall_level}"
      dimension_scores: "${state.evaluate_report.dimension_scores_json}"
      critical_gaps: "${state.evaluate_report.critical_gaps}"
    output_schema:
      reasoning:
        type: str
        description: 2-3 sentences explaining the decision
      gaps_to_address_json:
        type: str
        description: >
          JSON array of gaps to address (empty if should_reflect=false).
          Each: {"gap": "...", "dimension": "...", "queries": ["q1", "q2"]}
      synthesis_instructions:
        type: str
        description: >
          Specific instructions for the re-synthesis stage. What to keep,
          what to drop, what to emphasize, what to add. If relevance was
          the issue, include explicit filtering instructions.
      should_reflect:
        type: bool
        description: true if reflection round is needed
  - id: gap_fill_research
    kind: action
    action: gap_fill_research
    depends_on:
      - reflect_and_plan
    input:
      topic: "${task}"
      should_reflect: "${state.reflect_and_plan.should_reflect}"
      gaps_json: "${state.reflect_and_plan.gaps_to_address_json}"
  - id: re_synthesize
    kind: model
    depends_on:
      - gap_fill_research
    purpose: >
      You are a senior analyst producing the FINAL version of a research report.
      You have:
      - The v1 report (initial research)
      - Gap-fill research findings (targeted re-research on identified weaknesses)
      - Specific synthesis instructions from the reflection analysis
      - The original evaluation showing what was wrong with v1

      Produce a substantially improved v2 report. Follow the synthesis
      instructions carefully. If the instructions say to filter for relevance,
      be ruthless about cutting off-topic content. If they say to deepen
      specific dimensions, use the gap-fill data to do so.

      Structure the report with the same sections as v1, but:
      - Add a "Reflection Notes" section at the top summarizing what was
        improved and why
      - Clearly mark sections that were significantly revised

      The goal is not a longer report — it's a BETTER one. Quality over quantity.
    input:
      topic: "${task}"
      v1_report: "${state.initial_research.report_text}"
      evaluation_summary: "${state.evaluate_report.dimension_scores_json}"
      overall_level: "${state.evaluate_report.overall_level}"
      should_reflect: "${state.reflect_and_plan.should_reflect}"
      synthesis_instructions: "${state.reflect_and_plan.synthesis_instructions}"
      gap_fill_results: "${state.gap_fill_research.gap_results_json}"
    output_schema:
      v2_report:
        type: str
        description: The improved research report in Markdown
      improvement_summary:
        type: str
        description: Summary of what changed from v1 to v2
  - id: final_evaluate
    kind: model
    depends_on:
      - re_synthesize
    purpose: >
      You are a quality auditor doing the final review. Compare v1 and v2.

      Grade v2 on the same 6 dimensions using the conceptual levels:
      EXCELLENT, ADEQUATE, WEAK, FAILED.
      For each dimension, note whether it IMPROVED, STAYED SAME, or DECLINED vs v1.

      Also assess: was the reflection process worth it? Did the gap-fill
      research actually improve the report, or was it marginal?

      Return structured evaluation.
    input:
      topic: "${task}"
      v1_report: "${state.initial_research.report_text}"
      v2_report: "${state.re_synthesize.v2_report}"
      v1_scores: "${state.evaluate_report.dimension_scores_json}"
      v1_overall_level: "${state.evaluate_report.overall_level}"
      improvement_summary: "${state.re_synthesize.improvement_summary}"
    output_schema:
      v2_dimension_scores_json:
        type: str
        description: Same format as v1 dimension levels, with added "trend" field
      v2_overall_level:
        type: str
        description: V2 overall quality level. One of EXCELLENT, ADEQUATE, WEAK, FAILED.
      reflection_assessment:
        type: str
        description: >
          2-3 sentences on whether the reflection process was effective.
          What improved, what didn't, and what this implies for future
          research processes.
  - id: compile_report
    kind: action
    action: compile_report
    depends_on:
      - final_evaluate
    input:
      topic: "${task}"
      v1_report: "${state.initial_research.report_text}"
      v2_report: "${state.re_synthesize.v2_report}"
      improvement_summary: "${state.re_synthesize.improvement_summary}"
      v1_overall_level: "${state.evaluate_report.overall_level}"
      v2_overall_level: "${state.final_evaluate.v2_overall_level}"
      v1_scores: "${state.evaluate_report.dimension_scores_json}"
      v2_scores: "${state.final_evaluate.v2_dimension_scores_json}"
      reflection_assessment: "${state.final_evaluate.reflection_assessment}"
      should_reflect: "${state.reflect_and_plan.should_reflect}"
      reflection_reasoning: "${state.reflect_and_plan.reasoning}"
semantic_outputs:
  final_report: compile_report
tags:
  - research
  - reflection
  - self-improve
  - iterative
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Shared HTTP cache
# ═══════════════════════════════════════════════════════════════════════════════

_SEARCH_CACHE: dict[str, list[dict[str, str]]] = {}
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Agently-ReflectiveResearch/1.0)"}
_REFLECTIVE_PROGRESS: dict[str, Any] = {}


def _reset_reflective_progress() -> None:
    _REFLECTIVE_PROGRESS.clear()
    _REFLECTIVE_PROGRESS.update({
        "phase": "starting",
        "detail": "Preparing self-reflective research action",
        "dim_count": 0,
        "search_hits": 0,
        "browsed_count": 0,
        "gaps_filled": 0,
        "new_sources": 0,
        "started_at": time.monotonic(),
    })


def _update_reflective_progress(**updates: Any) -> None:
    _REFLECTIVE_PROGRESS.update(updates)


def _get_reflective_progress() -> dict[str, Any]:
    return dict(_REFLECTIVE_PROGRESS)


async def _http_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
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
                params={"q": query}, headers=_HTTP_HEADERS,
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
    return results


async def _http_fetch(url: str, max_chars: int = 4000) -> str:
    try:
        import httpx
        from bs4 import BeautifulSoup
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=_HTTP_HEADERS)
            if resp.status_code != 200:
                return ""
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            body = soup.find("body") or soup
            text = body.get_text(separator="\n", strip=True)
            return re.sub(r"\n{3,}", "\n\n", text)[:max_chars]
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Action: initial_research — full agentic research (same core as example 15)
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_initial_research(topic: str = "", **kwargs) -> dict[str, Any]:
    """Run the full agentic research pipeline. Returns search results, browsed
    articles, dive results, and a model-synthesized report."""
    _reset_reflective_progress()
    _update_reflective_progress(phase="planning", detail="Model planning research dimensions")

    # Phase 0: Model plans the research dimensions (reuse model_plan logic inline)
    plan_prompt = f"""You are a senior research director. Design a research plan for:

{topic}

Decide how many dimensions to investigate (2-5) based on topic complexity.
For each dimension: label (1-5 words), complexity (surface/moderate/deep), 2-3 search queries.
Also note 2-3 cross-cutting themes.

Return JSON:
{{
  "dimensions": [
    {{"label": "...", "complexity": "surface|moderate|deep", "queries": ["q1", "q2"]}}
  ],
  "themes": "cross-cutting themes"
}}"""

    agent = Agently.create_agent("reflective-planner")
    plan_str = str(await agent.input(plan_prompt).async_start())
    try:
        json_match = re.search(r"\{.*\}", plan_str, re.DOTALL)
        plan = json.loads(json_match.group()) if json_match else {}
    except (json.JSONDecodeError, TypeError):
        plan = {}
    dimensions = plan.get("dimensions", [{"label": "Overview", "complexity": "moderate", "queries": [topic]}])
    _update_reflective_progress(
        phase="search",
        detail=f"Searching {len(dimensions)} planned dimensions",
        dim_count=len(dimensions),
    )

    # Phase 1: Search all dimensions
    all_results: dict[str, list[dict[str, str]]] = {}
    for dim in dimensions:
        label = dim.get("label", "dimension")
        queries = dim.get("queries", [dim.get("label", topic)])
        if isinstance(queries, str):
            queries = [q.strip() for q in queries.split(";") if q.strip()]
        dim_results: list[dict[str, str]] = []
        for q in (queries or [])[:4]:
            _update_reflective_progress(phase="search", detail=f"{label}: {q}")
            try:
                sr = await asyncio.wait_for(_http_search(q, max_results=4), timeout=20.0)
                dim_results.extend(sr)
                _update_reflective_progress(
                    search_hits=sum(len(v) for v in all_results.values()) + len(dim_results)
                )
            except asyncio.TimeoutError:
                _update_reflective_progress(detail=f"{label}: timed out on {q}")
                pass
        seen: set[str] = set()
        deduped: list[dict[str, str]] = []
        for r in dim_results:
            if r["url"] and r["url"] not in seen:
                seen.add(r["url"])
                deduped.append(r)
        all_results[dim["label"]] = deduped[:8]
        _update_reflective_progress(search_hits=sum(len(v) for v in all_results.values()))

    # Phase 2: Browse top articles (2 per dimension)
    browsed: list[dict[str, Any]] = []
    for dim in dimensions:
        for r in all_results.get(dim["label"], [])[:2]:
            _update_reflective_progress(
                phase="browse",
                detail=f"Fetching article {len(browsed) + 1}: {r.get('title', r.get('url', ''))[:80]}",
                browsed_count=len(browsed),
            )
            content = await _http_fetch(r["url"], max_chars=4000) or r.get("snippet", "")
            browsed.append({
                "dimension": dim["label"],
                "url": r["url"],
                "title": r["title"],
                "content": content,
            })
            _update_reflective_progress(browsed_count=len(browsed))

    # Phase 3: Synthesize report
    _update_reflective_progress(phase="synthesis", detail="Model synthesizing initial report")
    search_summary = "\n".join(
        f"Dimension: {label}\n" + "\n".join(
            f"  [{i+1}] {r['title']} — {r['snippet'][:150]}"
            for i, r in enumerate(results[:4])
        )
        for label, results in all_results.items()
    )
    browsed_summary = "\n\n---\n\n".join(
        f"[{b['dimension']}] {b['title']}\n{b['content'][:1000]}"
        for b in browsed[:8]
    )

    syn_prompt = f"""You are a senior industry analyst. Synthesize a research report on:

{topic}

Research dimensions: {[d['label'] for d in dimensions]}

Search results by dimension:
{search_summary[:4000]}

Full-text excerpts from browsed articles:
{browsed_summary[:4000]}

RELEVANCE FILTER: Before using any source, verify it is actually about the topic.
Exclude tangentially related or off-domain content. Better a shorter, focused
report than one padded with irrelevant material.

Produce a structured analysis with:
1. Executive Summary
2. Per-dimension analysis
3. Cross-cutting themes
4. Key data points and metrics
5. Research methodology note
6. Risks, uncertainties, and known unknowns
7. Strategic implications"""

    syn_agent = Agently.create_agent("reflective-synthesizer")
    report_text = str(await syn_agent.input(syn_prompt).async_start())
    _update_reflective_progress(phase="initial_complete", detail="Initial research report complete")

    return {
        "dim_count": len(dimensions),
        "browsed_count": len(browsed),
        "dive_count": 0,
        "report_text": report_text,
        "search_results_json": json.dumps(
            {label: [{"title": r["title"], "url": r["url"], "snippet": r["snippet"]} for r in results]
             for label, results in all_results.items()},
            ensure_ascii=False,
        ),
        "browsed_json": json.dumps(browsed, ensure_ascii=False),
        "decision_log_json": json.dumps([
            {"phase": "planning", "dimensions": [d["label"] for d in dimensions]},
            {"phase": "search", "total_hits": sum(len(v) for v in all_results.values())},
            {"phase": "browse", "articles_browsed": len(browsed)},
            {"phase": "synthesis", "report_length": len(report_text)},
        ], ensure_ascii=False),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Action: gap_fill_research — targeted re-research on identified gaps
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_gap_fill_research(
    topic: str = "",
    should_reflect: bool = False,
    gaps_json: str = "",
    **kwargs,
) -> dict[str, Any]:
    """Execute targeted research on identified gaps."""
    if not should_reflect:
        _update_reflective_progress(phase="gap_fill_skipped", detail="Reflection decided no gap-fill research is needed")
        return {"gap_results_json": "[]", "gaps_filled": 0, "total_new_sources": 0}

    _update_reflective_progress(phase="gap_fill", detail="Parsing gaps for targeted research")
    try:
        gaps = json.loads(gaps_json) if isinstance(gaps_json, str) else json.loads(str(gaps_json))
    except (json.JSONDecodeError, TypeError):
        gaps = []

    if not gaps:
        _update_reflective_progress(phase="gap_fill_skipped", detail="No actionable research gaps were provided")
        return {"gap_results_json": "[]", "gaps_filled": 0, "total_new_sources": 0}

    gap_results: list[dict[str, Any]] = []
    total_new = 0

    for gap_index, gap in enumerate(gaps, start=1):
        queries = gap.get("queries", [])
        if isinstance(queries, str):
            queries = [q.strip() for q in queries.split(";") if q.strip()]

        gap_sources: list[dict[str, Any]] = []
        for query_index, q in enumerate(queries[:3], start=1):
            _update_reflective_progress(
                phase="gap_fill",
                detail=f"Gap {gap_index}/{len(gaps)} query {query_index}/{min(len(queries), 3)}: {q}",
                gaps_filled=len(gap_results),
                new_sources=total_new,
            )
            results = await _http_search(q, max_results=3)
            for r in results[:1]:
                content = await _http_fetch(r["url"], max_chars=3000) or r.get("snippet", "")
                gap_sources.append({
                    "query": q,
                    "title": r["title"],
                    "url": r["url"],
                    "content": content,
                })
                total_new += 1
                _update_reflective_progress(new_sources=total_new)

        gap_results.append({
            "gap": gap.get("gap", ""),
            "dimension": gap.get("dimension", ""),
            "sources": gap_sources,
        })
        _update_reflective_progress(gaps_filled=len(gap_results))

    _update_reflective_progress(phase="gap_fill_complete", detail="Targeted gap-fill research complete")
    return {
        "gap_results_json": json.dumps(gap_results, ensure_ascii=False),
        "gaps_filled": len(gap_results),
        "total_new_sources": total_new,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Action: compile_report
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_compile_report(
    topic: str = "",
    v1_report: str = "",
    v2_report: str = "",
    improvement_summary: str = "",
    v1_overall_level: str = "",
    v2_overall_level: str = "",
    v1_scores: str = "",
    v2_scores: str = "",
    reflection_assessment: str = "",
    should_reflect: bool = False,
    reflection_reasoning: str = "",
    **kwargs,
) -> dict[str, Any]:
    """Compile final report with reflection trail."""

    # Build score comparison table
    score_table = ""
    try:
        v1s = json.loads(v1_scores) if isinstance(v1_scores, str) else json.loads(str(v1_scores))
        v2s = json.loads(v2_scores) if isinstance(v2_scores, str) else json.loads(str(v2_scores))
        if isinstance(v1s, dict) and isinstance(v2s, dict):
            rows = []
            for dim in v1s:
                l1 = _level_from_info(v1s.get(dim, {}))
                l2 = _level_from_info(v2s.get(dim, {}))
                s1 = _score_from_level_info(v1s.get(dim, {}))
                s2 = _score_from_level_info(v2s.get(dim, {}))
                delta = s2 - s1
                arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                rows.append(f"| {dim} | {l1} ({s1}/10 mapped) | {l2} ({s2}/10 mapped) | {arrow} {delta:+.1f} |")
            score_table = "\n".join(rows)
    except Exception:
        score_table = "(score comparison unavailable)"
    v1_mapped = _score_from_level(v1_overall_level)
    v2_mapped = _score_from_level(v2_overall_level)

    doc = f"""# Self-Reflective Research Report

**Topic:** {topic[:200]}
**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
**Reflection performed:** {should_reflect}
**V1 Overall Level:** {v1_overall_level or '*Not available*'} ({v1_mapped}/10 mapped) → **V2 Overall Level:** {v2_overall_level or '*Not available*'} ({v2_mapped}/10 mapped)

---

## Reflection Trail

**Decision:** {reflection_reasoning or 'No reflection needed'}

### Score Evolution

| Dimension | V1 Level | V2 Level | Mapped Delta |
|-----------|----|----|-------|
{score_table}

### Reflection Assessment

{reflection_assessment or '*Not available*'}

### Improvement Summary

{improvement_summary or '*Not available*'}

---

## Final Report (V2{" — Reflected & Improved" if should_reflect else ""})

{v2_report or v1_report or '*Report generation failed.*'}

---

## Appendix: V1 Report (Initial Draft)

{v1_report[:3000]}{'...' if len(v1_report or '') > 3000 else ''}

---

*Generated by Agently Self-Reflective Research Pipeline — research → evaluate → reflect → improve*
"""

    reports_dir = Path.home() / ".agently_deep_research"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()[:80]).strip("-")
    filepath = reports_dir / f"reflective-{slug}_{timestamp}.md"
    filepath.write_text(doc)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════

def _score_color(score: float) -> str:
    if score >= 7:
        return "green"
    if score >= 4:
        return "yellow"
    return "red"


_EVALUATION_LEVEL_SCORES = {
    "EXCELLENT": 9.0,
    "ADEQUATE": 6.0,
    "WEAK": 3.0,
    "FAILED": 0.0,
}


def _level_from_info(info: Any) -> str:
    if isinstance(info, dict):
        value = info.get("level") or info.get("grade") or info.get("quality_level")
        if value is not None:
            normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
            aliases = {
                "HIGH": "EXCELLENT",
                "MEDIUM": "ADEQUATE",
                "LOW": "WEAK",
                "CRITICAL": "FAILED",
            }
            return aliases.get(normalized, normalized)
    return "WEAK"


def _score_from_level(level: str) -> float:
    return _EVALUATION_LEVEL_SCORES.get(str(level).strip().upper().replace("-", "_").replace(" ", "_"), 3.0)


def _score_from_level_info(info: Any) -> float:
    if isinstance(info, dict) and "score" in info:
        try:
            return float(info.get("score"))
        except (TypeError, ValueError):
            pass
    return _score_from_level(_level_from_info(info))


def _overall_score_from_levels(scores: dict[str, Any]) -> float:
    if not scores:
        return 0.0
    values = [_score_from_level_info(info) for info in scores.values()]
    return round(sum(values) / len(values), 2) if values else 0.0


def _build_score_table(scores: dict[str, Any] | None, title: str, status: str = "queued") -> Table:
    border = "green" if status == "done" else ("yellow" if status == "active" else "dim")
    label = {"queued": "queued", "active": "active", "done": "done"}.get(status, status)
    table = Table(title=f"{title} ({label})", border_style=border)
    table.add_column("Dimension", style="cyan")
    table.add_column("Level / mapped", justify="center")
    if scores:
        for dim, info in scores.items():
            s = _score_from_level_info(info)
            level = _level_from_info(info)
            bar = "█" * int(s) + "░" * (10 - int(s))
            color = _score_color(s)
            table.add_row(dim, f"[bold {color}]{level}[/] ({s}/10) [dim]{bar}[/]")
    else:
        message = "Active: model evaluating..." if status == "active" else "Queued: waiting for prior step"
        table.add_row(message, "[dim]--[/]")
    return table


def _build_phase_panel(phase: str, icon: str, status: str, detail: str, border: str) -> Panel:
    body = Text(status, style="bold")
    if detail:
        body.append("\n")
        body.append(detail)
    return Panel(body, title=f"{icon} {phase}", border_style=border)


def _build_reflection_panel(
    should_reflect: bool | None,
    v1_score: float | None,
    v2_score: float | None,
    reasoning: str,
    done: bool,
    running: bool = False,
) -> Panel:
    if done:
        if should_reflect:
            icon = "[bold green]✓[/]"
            delta = ""
            if v1_score is not None and v2_score is not None:
                diff = v2_score - v1_score
                delta = f" ({diff:+.1f})"
            lines = [
                f"Decision: [bold yellow]REFLECT[/]",
                f"Mapped V1: {v1_score}/10 → V2: {v2_score}/10{delta}",
                f"Reasoning: {reasoning[:200]}" if reasoning else "",
            ]
        else:
            icon = "[bold green]✓[/]"
            lines = [
                f"Decision: [bold green]SHIP[/] (mapped score {v1_score}/10 sufficient)",
                f"Reasoning: {reasoning[:200]}" if reasoning else "",
            ]
    elif should_reflect is not None:
        icon = "[bold yellow]◎[/]"
        lines = [f"Evaluating...", f"Mapped V1 score: {v1_score}/10"]
    elif running:
        icon = "[bold yellow]◎[/]"
        lines = ["Active: model deciding whether reflection is needed."]
    else:
        icon = "[dim]3[/]"
        lines = ["Queued: starts after V1 evaluation."]
    border = "green" if done else ("yellow" if running or should_reflect is not None else "dim")
    return Panel(Text.from_markup("\n".join(lines)), title=f"{icon} Step 3 Reflect & Plan", border_style=border)


def _build_output_panel(file_path: str, done: bool, running: bool = False) -> Panel:
    if done and file_path:
        return Panel(Text.from_markup(f"[green]{file_path}[/]"), title="[bold green]✓[/] Step 5 Output", border_style="green")
    if running:
        return Panel(Text("Active: formatting and saving final report..."), title="[bold yellow]◎[/] Step 5 Output", border_style="yellow")
    return Panel(Text("Queued: final save happens after V2 evaluation.", style="dim"), title="[dim]5[/] Step 5 Output", border_style="dim")


def _build_layout(
    research: Panel, v1_scores: Table,
    reflect: Panel, v2_scores: Table, output: Panel,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=2),
        Layout(name="mid", ratio=3),
        Layout(name="bottom", ratio=1),
    )
    layout["top"].split_row(
        Layout(research, name="research", ratio=2),
        Layout(v1_scores, name="v1_scores", ratio=1),
    )
    layout["mid"].split_row(
        Layout(reflect, name="reflect", ratio=2),
        Layout(v2_scores, name="v2_scores", ratio=1),
    )
    layout["bottom"].split_row(Layout(output, name="output"))
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    import os

    # ── Parse CLI args ──
    args = sys.argv[1:]
    topic_parts: list[str] = []
    language = os.environ.get("AGENTLY_RESEARCH_LANGUAGE", "").strip()
    i = 0
    while i < len(args):
        if args[i] in ("--lang", "-l") and i + 1 < len(args):
            language = args[i + 1]
            i += 2
        elif args[i].startswith("--lang="):
            language = args[i].split("=", 1)[1]
            i += 1
        elif not args[i].startswith("-"):
            topic_parts.append(args[i])
            i += 1
        else:
            i += 1

    if topic_parts:
        topic = " ".join(topic_parts).strip()
    elif os.environ.get("AGENTLY_RESEARCH_TOPIC", "").strip():
        topic = os.environ["AGENTLY_RESEARCH_TOPIC"].strip()
    else:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  Agently Self-Reflective Research Pipeline                 ║")
        print("║  Research → Evaluate → Reflect → Improve → Repeat          ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()
        print("Enter your research topic (end with blank line):")
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
    print(f"Topic ({len(topic):,} chars):")
    print(f"  {topic[:200]}{'...' if len(topic) > 200 else ''}")
    print(f"{'─'*60}\n")

    provider = configure_model(temperature=0.3)
    model_name = (
        os.environ.get("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")
        if provider == "deepseek"
        else os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")
    )
    print(f"Model: {provider} :: {model_name}\n")

    # Apply language
    LANG_NAMES = {
        "zh": "Chinese (Simplified)", "zh-tw": "Chinese (Traditional)",
        "en": "English", "ja": "Japanese", "ko": "Korean",
        "fr": "French", "de": "German", "es": "Spanish",
        "pt": "Portuguese", "ru": "Russian", "ar": "Arabic",
    }
    if language and language != "auto":
        lang_display = LANG_NAMES.get(language.lower(), language)
        topic = (
            f"[OUTPUT LANGUAGE: {lang_display} — "
            f"ALL content MUST be written in {lang_display}]\n\n"
            f"{topic}"
        )
        print(f"Output language: {lang_display}")

    # ── Setup ──────────────────────────────────────────────────────────────
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_reflective_"))
    registry_root = runtime_dir / "registry"
    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)

    skill_dir = runtime_dir / "self-reflective-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(SELF_REFLECTIVE_RESEARCH_SKILL_YAML.strip())
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("reflective-researcher")
    agent.register_action(
        name="initial_research",
        desc="Execute full agentic research: plan dimensions, search, browse, synthesize.",
        kwargs={"topic": ("str", "Research topic.")},
        func=_action_initial_research,
    )
    agent.register_action(
        name="gap_fill_research",
        desc="Execute targeted re-research on identified gaps.",
        kwargs={
            "topic": ("str", "Research topic."),
            "should_reflect": ("bool", "Whether reflection is needed."),
            "gaps_json": ("str", "JSON array of gaps to address."),
        },
        func=_action_gap_fill_research,
    )
    agent.register_action(
        name="compile_report",
        desc="Compile final report with reflection trail.",
        kwargs={
            "topic": ("str", "Research topic."),
            "v1_report": ("str", "V1 report text."),
            "v2_report": ("str", "V2 report text."),
            "improvement_summary": ("str", "Summary of improvements."),
            "v1_overall_level": ("str", "V1 overall conceptual level."),
            "v2_overall_level": ("str", "V2 overall conceptual level."),
            "v1_scores": ("str", "V1 dimension level JSON."),
            "v2_scores": ("str", "V2 dimension level JSON."),
            "reflection_assessment": ("str", "Reflection effectiveness assessment."),
            "should_reflect": ("bool", "Whether reflection occurred."),
            "reflection_reasoning": ("str", "Reflection decision reasoning."),
        },
        func=_action_compile_report,
    )

    execution = (
        agent
        .use_skills(["self-reflective-research"], mode="required")
        .input(topic)
        .create_execution()
    )
    _reset_reflective_progress()
    _update_reflective_progress(phase="starting", detail="Waiting for initial_research action to start")

    # ── State trackers ─────────────────────────────────────────────────────
    completed_stages: set[str] = set()
    stage_running: dict[str, bool] = {"initial_research": True}

    research_done = False
    v1_scores: dict[str, Any] = {}
    v1_overall: float | None = None
    should_reflect: bool | None = None
    reflection_reasoning = ""
    v2_scores: dict[str, Any] = {}
    v2_overall: float | None = None
    saved_file = ""
    save_done = False
    progress_phase = ""
    progress_detail = ""
    progress_dim_count = 0
    progress_search_hits = 0
    progress_browsed = 0
    progress_gaps_filled = 0
    progress_new_sources = 0
    progress_started_at = time.monotonic()

    # ── Initial panels ─────────────────────────────────────────────────────
    research_panel = _build_phase_panel("Step 1 Initial Research", "[bold yellow]◎[/]", "Starting...", "Waiting for initial_research action to start", "yellow")
    v1_table = _build_score_table(None, "Step 2 V1 Evaluation", "queued")
    reflect_panel = _build_reflection_panel(None, None, None, "", False)
    v2_table = _build_score_table(None, "Step 4 Improve & V2 Evaluation", "queued")
    output_panel = _build_output_panel("", False)
    layout = _build_layout(research_panel, v1_table, reflect_panel, v2_table, output_panel)

    def sync_reflective_progress() -> None:
        nonlocal progress_phase, progress_detail, progress_dim_count, progress_search_hits
        nonlocal progress_browsed, progress_gaps_filled, progress_new_sources, progress_started_at
        progress = _get_reflective_progress()
        if not progress:
            return
        progress_phase = str(progress.get("phase") or progress_phase)
        progress_detail = str(progress.get("detail") or progress_detail)
        progress_dim_count = int(progress.get("dim_count") or progress_dim_count or 0)
        progress_search_hits = int(progress.get("search_hits") or progress_search_hits or 0)
        progress_browsed = int(progress.get("browsed_count") or progress_browsed or 0)
        progress_gaps_filled = int(progress.get("gaps_filled") or progress_gaps_filled or 0)
        progress_new_sources = int(progress.get("new_sources") or progress_new_sources or 0)
        progress_started_at = float(progress.get("started_at") or progress_started_at)

    def research_progress_detail() -> str:
        elapsed = max(0, int(time.monotonic() - progress_started_at))
        lines = [
            f"Phase: {progress_phase or 'waiting'}",
            f"Elapsed: {elapsed}s",
            f"Dimensions: {progress_dim_count}",
            f"Search hits: {progress_search_hits}",
            f"Browsed: {progress_browsed}",
        ]
        if progress_gaps_filled or progress_new_sources:
            lines.append(f"Gap-fill: {progress_gaps_filled} gaps, {progress_new_sources} new sources")
        if progress_detail:
            lines.extend(["", progress_detail[:180]])
        return "\n".join(lines)

    console = Console(force_terminal=True)
    with Live(layout, console=console, refresh_per_second=6, screen=False, transient=False) as live:
        gen = execution.get_async_generator(type="instant")
        next_item = asyncio.create_task(gen.__anext__())
        while True:
            try:
                item = await asyncio.wait_for(asyncio.shield(next_item), timeout=0.4)
            except asyncio.TimeoutError:
                sync_reflective_progress()
                if not research_done:
                    research_panel = _build_phase_panel(
                        "Step 1 Initial Research", "[bold yellow]◎[/]",
                        "Running agentic research...", research_progress_detail(), "yellow",
                    )
                elif stage_running.get("gap_fill_research"):
                    research_panel = _build_phase_panel(
                        "Step 4 Gap-Fill Research", "[bold yellow]◎[/]",
                        "Running targeted research...", research_progress_detail(), "yellow",
                    )
                v1_status = "done" if v1_scores else ("active" if stage_running.get("evaluate_report") else "queued")
                v2_status = "done" if v2_scores else ("active" if stage_running.get("re_synthesize") or stage_running.get("final_evaluate") else "queued")
                v1_table = _build_score_table(v1_scores if v1_scores else None, "Step 2 V1 Evaluation", v1_status)
                reflect_panel = _build_reflection_panel(
                    should_reflect, v1_overall, v2_overall,
                    reflection_reasoning,
                    "reflect_and_plan" in completed_stages,
                    stage_running.get("reflect_and_plan", False),
                )
                v2_table = _build_score_table(v2_scores if v2_scores else None, "Step 4 Improve & V2 Evaluation", v2_status)
                output_panel = _build_output_panel(saved_file, save_done, stage_running.get("compile_report", False))
                live.update(
                    _build_layout(research_panel, v1_table, reflect_panel, v2_table, output_panel),
                    refresh=True,
                )
                continue
            except StopAsyncIteration:
                break
            next_item = asyncio.create_task(gen.__anext__())

            # Stage starts
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                for sid in ("initial_research", "evaluate_report", "reflect_and_plan",
                            "gap_fill_research", "re_synthesize", "final_evaluate", "compile_report"):
                    if sid in item.path:
                        stage_running[sid] = True

            # Stage completions
            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                completed_stages.add(stage_id)
                stage_running[stage_id] = False

            # Action completions
            if item.path.startswith("actions.") and item.is_complete:
                val = item.value or {}
                result = val.get("result", val) if isinstance(val, dict) else val
                if not isinstance(result, dict):
                    continue

                action_name = item.path.split(".", 1)[1]
                if action_name == "initial_research":
                    research_done = True
                elif action_name == "compile_report":
                    saved_file = result.get("file_path", "")
                    save_done = True

            # Parse evaluation levels from evaluate_report completion
            if "evaluate_report" in completed_stages and not v1_scores:
                for stage_id, stage_data in (item.value or {}).items():
                    if isinstance(stage_data, dict):
                        dims_json = stage_data.get("dimension_scores_json", "")
                        ov = stage_data.get("overall_level")
                        if isinstance(dims_json, str) and dims_json:
                            try:
                                v1_scores = json.loads(dims_json)
                                if isinstance(v1_scores, dict):
                                    v1_overall = _overall_score_from_levels(v1_scores)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        if v1_overall is None and ov is not None:
                            v1_overall = _score_from_level(str(ov))

            # Parse reflection decision
            if "reflect_and_plan" in completed_stages and should_reflect is None:
                for stage_id, stage_data in (item.value or {}).items():
                    if isinstance(stage_data, dict):
                        sr = stage_data.get("should_reflect")
                        if sr is not None:
                            should_reflect = bool(sr) if not isinstance(sr, bool) else sr
                        reflection_reasoning = str(stage_data.get("reasoning", ""))

            # Parse V2 evaluation levels from final_evaluate
            if "final_evaluate" in completed_stages and not v2_scores:
                for stage_id, stage_data in (item.value or {}).items():
                    if isinstance(stage_data, dict):
                        dims_json = stage_data.get("v2_dimension_scores_json", "")
                        ov = stage_data.get("v2_overall_level")
                        if isinstance(dims_json, str) and dims_json:
                            try:
                                v2_scores = json.loads(dims_json)
                                if isinstance(v2_scores, dict):
                                    v2_overall = _overall_score_from_levels(v2_scores)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        if v2_overall is None and ov is not None:
                            v2_overall = _score_from_level(str(ov))

            # Update panels
            sync_reflective_progress()
            if research_done:
                research_panel = _build_phase_panel(
                    "Step 1 Initial Research", "[bold green]✓[/]",
                    "Complete",
                    f"Evaluating... (mapped score: {v1_overall}/10)\n{research_progress_detail()}"
                    if v1_overall is not None else research_progress_detail(),
                    "green",
                )
            elif stage_running.get("initial_research"):
                research_panel = _build_phase_panel(
                    "Step 1 Initial Research", "[bold yellow]◎[/]",
                    "Running agentic research...", research_progress_detail(), "yellow",
                )
            elif stage_running.get("gap_fill_research"):
                research_panel = _build_phase_panel(
                    "Step 4 Gap-Fill Research", "[bold yellow]◎[/]",
                    "Running targeted research...", research_progress_detail(), "yellow",
                )
            else:
                research_panel = _build_phase_panel(
                    "Step 1 Initial Research", "[dim]1[/]", "Queued...", "Waiting for execution events.", "dim",
                )

            v1_status = "done" if v1_scores else ("active" if stage_running.get("evaluate_report") else "queued")
            v1_table = _build_score_table(v1_scores if v1_scores else None, "Step 2 V1 Evaluation", v1_status)

            reflect_panel = _build_reflection_panel(
                should_reflect, v1_overall, v2_overall,
                reflection_reasoning,
                "reflect_and_plan" in completed_stages,
                stage_running.get("reflect_and_plan", False),
            )

            v2_status = "done" if v2_scores else ("active" if stage_running.get("re_synthesize") or stage_running.get("final_evaluate") else "queued")
            v2_table = _build_score_table(v2_scores if v2_scores else None, "Step 4 Improve & V2 Evaluation", v2_status)

            output_panel = _build_output_panel(saved_file, save_done, stage_running.get("compile_report", False))
            live.update(
                _build_layout(research_panel, v1_table, reflect_panel, v2_table, output_panel),
                refresh=True,
            )

    # ── Final summary ─────────────────────────────────────────────────────
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\n{'='*70}")
    print(f"route: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    print(f"reflection triggered: {should_reflect}")
    if v1_overall is not None:
        print(f"V1 mapped score: {v1_overall}/10")
    if v2_overall is not None:
        delta = v2_overall - (v1_overall or 0)
        print(f"V2 mapped score: {v2_overall}/10 ({delta:+.1f})")
    print(f"\nsaved: {saved_file or '(not saved)'}")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
