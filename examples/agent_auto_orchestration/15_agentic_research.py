"""Agentic research — model-driven adaptive research with dynamic depth and gap detection.

Run:
    python examples/agent_auto_orchestration/15_agentic_research.py

    # pass a topic on the command line:
    python examples/agent_auto_orchestration/15_agentic_research.py "DePIN projects 2025"

    # specify output language (auto-detects from topic if omitted):
    python examples/agent_auto_orchestration/15_agentic_research.py --lang zh "AI agent frameworks"
    python examples/agent_auto_orchestration/15_agentic_research.py -l ja "日本の半導体産業"
    AGENTLY_RESEARCH_LANGUAGE=zh python examples/agent_auto_orchestration/15_agentic_research.py

    # pass topic via environment variable:
    AGENTLY_RESEARCH_TOPIC="RISC-V ecosystem" python examples/agent_auto_orchestration/15_agentic_research.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.
    Requires: pip install rich httpx beautifulsoup4

Key difference from example 14 (deep_research):
  Example 14 has a fixed pipeline: always 3 dimensions, always 2-layer depth,
  always browse + dive + cross-validate. The structure is predetermined.

  Example 15 lets the model control the research process:
  - Model decides how many dimensions (2–5) based on topic complexity
  - After initial search, model assesses quality per dimension and decides
    which need deep-dives vs. which are already well-covered
  - Model selects which specific articles to browse (not blind top-N)
  - Model decides whether reference hooks are worth pursuing
  - Model checks overall sufficiency and can trigger additional rounds
  - Max 3 rounds, but model can stop early when satisfied

  All model decisions are streamed to the display so you can watch the
  agentic reasoning unfold in real-time.

Skill stages (6):
  1. plan_research     (model_plan) — decompose topic into N dimensions (model decides N)
  2. execute_research  (action)     — the agentic core: search → assess → browse → decide
  3. validate_coverage (validate)   — gates on research completion
  4. synthesize        (model)      — synthesize findings into structured report
  5. cross_validate    (model)      — consistency check + conceptual confidence labels
  6. compile_report    (action)     — format and save to disk
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

from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════════════════
# Skill definition
# ═══════════════════════════════════════════════════════════════════════════════

AGENTIC_RESEARCH_SKILL_YAML = """
skill_id: agentic-research
version: 1.0.0
display_name: Agentic Research Pipeline
purpose: >
  Model-driven adaptive research: the model decides how many dimensions to
  investigate, assesses initial results to decide which areas need deep-dives,
  selects specific articles to browse, determines whether reference hooks are
  worth pursuing, and checks sufficiency — triggering additional rounds if
  needed. Max 3 rounds, but the model can stop early.
trust_level: local
kind: workflow
activation:
  keywords:
    - research
    - deep research
    - agentic
    - adaptive research
    - flexible research
requires:
  actions:
    - execute_research
    - compile_report
stages:
  - id: plan_research
    kind: model_plan
    purpose: >
      You are a senior research director. Your task is to design a research plan
      for the topic below. Unlike a rigid template, YOU decide how many dimensions
      to investigate (minimum 2, maximum 5) based on the topic's nature and
      complexity.

      For each dimension provide:
      - A short label (1-5 words)
      - A complexity rating: "surface" (well-known, easily searchable),
        "moderate" (requires synthesizing multiple sources), or
        "deep" (nuanced, rapidly evolving, or technically dense)
      - 2-3 specific, concrete search queries

      Also note 2-3 cross-cutting themes or tensions.

      Be specific. Use concrete names, not generic placeholders.
    input:
      topic: "${task}"
    output_schema:
      research_strategy:
        type: str
        description: 2-3 sentences on overall research approach and rationale for the dimension count chosen
      dimensions_json:
        type: str
        description: >
          JSON array of dimension objects. Each object: {"label": "...", "complexity":
          "surface|moderate|deep", "queries": ["q1", "q2", "q3"]}. 2-5 dimensions.
          Example: [{"label": "GPU Architecture", "complexity": "deep", "queries":
          ["NVIDIA Blackwell B200 architecture 2025", "AMD MI300X vs H200 benchmark",
          "chiplet-based GPU design trends 2026"]}]
      cross_cutting_themes:
        type: str
        description: 2-3 cross-cutting themes or tensions spanning multiple dimensions
  - id: execute_research
    kind: action
    action: execute_research
    depends_on:
      - plan_research
    input:
      topic: "${task}"
      strategy: "${state.plan_research.research_strategy}"
      dimensions_json: "${state.plan_research.dimensions_json}"
      themes: "${state.plan_research.cross_cutting_themes}"
  - id: validate_coverage
    kind: validate
    depends_on:
      - execute_research
    validation:
      required_state:
        - execute_research
  - id: synthesize
    kind: model
    depends_on:
      - validate_coverage
    purpose: >
      You are a senior industry analyst synthesizing an adaptive research report.
      You have research data gathered through a model-driven process — the model
      decided which dimensions to investigate, which to deep-dive, and when enough
      was enough.

      The research data includes:
      - Search results from an initial round (all dimensions)
      - Full-article content from model-selected articles
      - Reference-dive findings (if the model decided to pursue them)
      - Additional gap-filling search results (if the model decided more was needed)
      - A decision log showing what the model decided and why at each step

      CRITICAL — RELEVANCE FILTER:
      Before using any piece of research data, assess whether it is actually
      about the topic. Web searches often return tangentially related or
      completely unrelated results. If a source is about a different domain,
      uses the same keywords in an unrelated context, or only superficially
      mentions the topic, exclude it. It is better to have a shorter report
      with high signal-to-noise ratio than to pad with irrelevant material.
      If you encounter content that seems off-topic, explicitly note it in
      the methodology section rather than silently incorporating it.

      Produce a structured analysis covering:

      1. Executive Summary
      2. Per-dimension analysis: key players, market structure, trends, outlook
         (depth will naturally vary by dimension based on what the model chose)
      3. Cross-cutting theme analysis
      4. Key data points and metrics
      5. Research methodology note: explain which dimensions received deep
         investigation and why (use the decision log). Also note any research
         data that was excluded for relevance reasons.
      6. Risks, uncertainties, and known unknowns
      7. Strategic implications

      Be analytical, not descriptive. The report should reflect the adaptive
      research process — some dimensions will have richer detail than others,
      and that's by design.
    input:
      topic: "${task}"
      strategy: "${state.plan_research.research_strategy}"
      themes: "${state.plan_research.cross_cutting_themes}"
      search_results: "${state.execute_research.search_results_json}"
      browsed_content: "${state.execute_research.browsed_json}"
      dive_results: "${state.execute_research.dive_results_json}"
      decision_log: "${state.execute_research.decision_log_json}"
      rounds: "${state.execute_research.total_rounds}"
    output_schema:
      report:
        type: str
        description: The complete synthesized research report in Markdown
  - id: cross_validate
    kind: model
    depends_on:
      - synthesize
    purpose: >
      You are a rigorous fact-checker. Review the report for:

      1. Content relevance: Is every substantive section actually about the
         stated topic? Flag any passages, claims, or entire sections that are
         off-topic, belong to a different domain, or are generic filler that
         could apply to any topic. Estimate the signal-to-noise ratio.
      2. Internal consistency: Do sections agree with each other? Are there
         contradictions or inconsistent numbers?
      3. Source quality assessment: Are cited sources authoritative? Are
         claims backed by identifiable sources?
      4. Knowledge gaps (especially in dimensions the model chose NOT to deep-dive)
      5. Confidence ratings for major claims using conceptual levels only:
         HIGH_CONFIDENCE when sources are authoritative, evidence is sufficient,
         evidence directly supports the claim, and domain materials broadly
         agree; MODERATE_CONFIDENCE when sources are reasonably broad and
         evidence directly or indirectly supports the claim but some inference
         or cross-domain analogy remains; LOW_CONFIDENCE when sources are
         missing, mostly promotional, single-source, speculative, or weakly
         linked to the claim. Do not emit numeric confidence scores.
      6. Recommended Phase 2 follow-up

      Be honest about limitations. If a dimension got only surface treatment,
      flag what's missing. If the report contains irrelevant content, call it
      out specifically — do not gloss over it.
    input:
      report: "${state.synthesize.report}"
      topic: "${task}"
      decision_log: "${state.execute_research.decision_log_json}"
    output_schema:
      validation_notes:
        type: str
        description: Validation findings, confidence ratings, gaps, Phase 2 recommendations
  - id: compile_report
    kind: action
    action: compile_report
    depends_on:
      - cross_validate
    input:
      report: "${state.synthesize.report}"
      validation: "${state.cross_validate.validation_notes}"
      topic: "${task}"
      decision_log: "${state.execute_research.decision_log_json}"
      total_rounds: "${state.execute_research.total_rounds}"
      dim_count: "${state.execute_research.dim_count}"
      browsed_count: "${state.execute_research.browsed_count}"
      dive_count: "${state.execute_research.dive_count}"
semantic_outputs:
  research_report: compile_report
tags:
  - research
  - agentic
  - adaptive
  - workflow
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Shared HTTP cache
# ═══════════════════════════════════════════════════════════════════════════════

_SEARCH_CACHE: dict[str, list[dict[str, str]]] = {}
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Agently-AgenticResearch/1.0)"}
_RESEARCH_PROGRESS: dict[str, Any] = {}


def _reset_research_progress() -> None:
    _RESEARCH_PROGRESS.clear()
    _RESEARCH_PROGRESS.update({
        "phase": "starting",
        "detail": "Preparing adaptive research action",
        "dim_count": 0,
        "total_rounds": 1,
        "search_hits": 0,
        "browsed_count": 0,
        "dive_count": 0,
        "decisions": [],
    })


def _update_research_progress(**updates: Any) -> None:
    _RESEARCH_PROGRESS.update(updates)


def _get_research_progress() -> dict[str, Any]:
    progress = dict(_RESEARCH_PROGRESS)
    progress["decisions"] = list(progress.get("decisions") or [])
    return progress


async def _http_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search DuckDuckGo HTML, with cache."""
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
                headers=_HTTP_HEADERS,
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
    """Fetch and extract readable text from a URL."""
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
# Action: execute_research — the agentic core
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_execute_research(
    topic: str = "",
    strategy: str = "",
    dimensions_json: str = "",
    themes: str = "",
    **kwargs,
) -> dict[str, Any]:
    """Model-driven adaptive research loop.

    Phases:
      1. Parse the model's research plan (dynamic N dimensions)
      2. Round 1 — search all dimensions
      3. Model assesses quality per dimension, decides depth needs
      4. Model selects which articles to browse (not blind top-N)
      5. Browse selected articles
      6. Model decides whether to pursue reference hooks
      7. If yes: extract hooks, search, browse dive results
      8. Model checks sufficiency — stop or trigger gap-filling round
      9. Max 3 rounds; model can stop early
    """
    import httpx  # ensure available
    _reset_research_progress()
    _update_research_progress(phase="parse_plan", detail="Parsing model-generated dimensions")

    # Parse dimensions from model output
    dimensions: list[dict[str, Any]] = []
    try:
        parsed = json.loads(dimensions_json) if isinstance(dimensions_json, str) else dimensions_json
        if isinstance(parsed, list):
            dimensions = parsed
    except (json.JSONDecodeError, TypeError):
        pass

    if not dimensions:
        # Fallback: treat the topic itself as one dimension
        dimensions = [{"label": "Overview", "complexity": "moderate", "queries": [topic]}]

    decision_log: list[dict[str, Any]] = []
    _update_research_progress(
        phase="search",
        detail=f"Searching {len(dimensions)} model-selected dimensions",
        dim_count=len(dimensions),
    )

    # ── Phase 1: Round 1 search (all dimensions sequentially with timeout guard) ──
    all_results: dict[str, list[dict[str, str]]] = {}
    for dim in dimensions:
        label = dim.get("label", "dimension")
        queries = dim.get("queries", [dim.get("label", topic)])
        if isinstance(queries, str):
            queries = [q.strip() for q in queries.split(";") if q.strip()]
        dim_results: list[dict[str, str]] = []
        for q in (queries or [])[:4]:
            _update_research_progress(phase="search", detail=f"{label}: {q}")
            try:
                search_result = await asyncio.wait_for(
                    _http_search(q, max_results=4), timeout=20.0
                )
                dim_results.extend(search_result)
                _update_research_progress(
                    search_hits=sum(len(v) for v in all_results.values()) + len(dim_results)
                )
            except asyncio.TimeoutError:
                _update_research_progress(detail=f"{label}: timed out on {q}")
                pass
        # Deduplicate
        seen: set[str] = set()
        deduped: list[dict[str, str]] = []
        for r in dim_results:
            if r["url"] and r["url"] not in seen:
                seen.add(r["url"])
                deduped.append(r)
        all_results[dim["label"]] = deduped[:8]
        _update_research_progress(search_hits=sum(len(v) for v in all_results.values()))

    # ── Phase 2: Model assesses initial results ──
    _update_research_progress(phase="assess", detail="Model assessing source quality and depth needs")
    assessment = await _model_assess_results(topic, dimensions, all_results)
    decision_log.append({
        "phase": "initial_assessment",
        "summary": assessment.get("summary", ""),
        "per_dimension": assessment.get("per_dimension", {}),
    })
    _update_research_progress(decisions=decision_log.copy())

    # ── Phase 3: Model selects articles to browse ──
    _update_research_progress(phase="select_articles", detail="Model selecting articles for full-text browsing")
    browse_selection = await _model_select_articles(topic, dimensions, all_results, assessment)
    articles_to_browse: list[dict[str, str]] = browse_selection.get("articles", [])
    decision_log.append({
        "phase": "article_selection",
        "reasoning": browse_selection.get("reasoning", ""),
        "selected_count": len(articles_to_browse),
        "selected_urls": [a["url"] for a in articles_to_browse],
    })
    _update_research_progress(decisions=decision_log.copy())

    # ── Phase 4: Browse selected articles ──
    browsed: list[dict[str, Any]] = []
    for index, art in enumerate(articles_to_browse, start=1):
        url = art.get("url", "")
        if not url:
            continue
        _update_research_progress(
            phase="browse",
            detail=f"Fetching article {index}/{len(articles_to_browse)}: {art.get('title', url)[:80]}",
            browsed_count=len(browsed),
        )
        content = await _http_fetch(url, max_chars=4000)
        if not content:
            content = art.get("snippet", "[unavailable]")
        browsed.append({
            "dimension": art.get("dimension", ""),
            "url": url,
            "title": art.get("title", url),
            "content": content,
            "content_length": len(content),
            "selection_reason": art.get("reason", ""),
        })
        _update_research_progress(browsed_count=len(browsed))

    # ── Phase 5: Model decides whether to dive on reference hooks ──
    _update_research_progress(phase="dive_decision", detail="Model deciding whether reference hooks merit deeper research")
    dive_decision = await _model_decide_dive(topic, browsed, assessment)
    decision_log.append({
        "phase": "dive_decision",
        "should_dive": dive_decision.get("should_dive", False),
        "reasoning": dive_decision.get("reasoning", ""),
    })
    _update_research_progress(decisions=decision_log.copy())

    dive_results: list[dict[str, Any]] = []
    if dive_decision.get("should_dive") and dive_decision.get("hooks"):
        hooks = dive_decision["hooks"][:6]
        for index, hook in enumerate(hooks, start=1):
            _update_research_progress(phase="dive", detail=f"Investigating hook {index}/{len(hooks)}: {hook}")
            results = await _http_search(hook, max_results=3)
            dive_browsed = []
            for r in results[:1]:
                content = await _http_fetch(r["url"], max_chars=2000) or r.get("snippet", "")
                dive_browsed.append({"title": r["title"], "url": r["url"], "content": content})
            dive_results.append({
                "hook": hook,
                "search_results": results,
                "browsed": dive_browsed,
            })
            _update_research_progress(dive_count=len(dive_results))

    # ── Phase 6: Sufficiency check + optional additional rounds ──
    max_rounds = 3
    total_rounds = 1
    gap_results: list[dict[str, Any]] = []

    _update_research_progress(phase="sufficiency", detail="Model checking whether gathered evidence is sufficient", total_rounds=total_rounds)
    sufficiency = await _model_check_sufficiency(
        topic, dimensions, all_results, browsed, dive_results, total_rounds, assessment
    )
    decision_log.append({
        "phase": f"sufficiency_round_{total_rounds}",
        "sufficient": sufficiency.get("sufficient", True),
        "gaps": sufficiency.get("gaps", []),
        "reasoning": sufficiency.get("reasoning", ""),
    })
    _update_research_progress(decisions=decision_log.copy())

    while not sufficiency.get("sufficient", True) and total_rounds < max_rounds:
        total_rounds += 1
        _update_research_progress(
            phase="gap_fill",
            detail=f"Running gap-fill round {total_rounds}/{max_rounds}",
            total_rounds=total_rounds,
        )
        gap_queries = sufficiency.get("gap_queries", [])
        if isinstance(gap_queries, str):
            gap_queries = [q.strip() for q in gap_queries.split(";") if q.strip()]

        for index, gq in enumerate(gap_queries[:4], start=1):
            _update_research_progress(
                phase="gap_fill",
                detail=f"Gap query {index}/{min(len(gap_queries), 4)}: {gq}",
                total_rounds=total_rounds,
            )
            results = await _http_search(gq, max_results=3)
            gap_browsed = []
            for r in results[:1]:
                content = await _http_fetch(r["url"], max_chars=2000) or r.get("snippet", "")
                gap_browsed.append({"title": r["title"], "url": r["url"], "content": content})
            gap_results.append({
                "query": gq,
                "reason": sufficiency.get("gaps", ""),
                "search_results": results,
                "browsed": gap_browsed,
            })

        _update_research_progress(phase="sufficiency", detail=f"Model checking sufficiency after round {total_rounds}")
        sufficiency = await _model_check_sufficiency(
            topic, dimensions, all_results, browsed, dive_results, total_rounds, assessment
        )
        decision_log.append({
            "phase": f"sufficiency_round_{total_rounds}",
            "sufficient": sufficiency.get("sufficient", True),
            "gaps": sufficiency.get("gaps", []),
            "reasoning": sufficiency.get("reasoning", ""),
        })
        _update_research_progress(decisions=decision_log.copy())

    _update_research_progress(phase="research_complete", detail="Adaptive research action complete")
    return {
        "dim_count": len(dimensions),
        "total_rounds": total_rounds,
        "browsed_count": len(browsed),
        "dive_count": len(dive_results),
        "gap_rounds": total_rounds - 1,
        "search_results_json": json.dumps(
            {label: [{"title": r["title"], "url": r["url"], "snippet": r["snippet"]} for r in results]
             for label, results in all_results.items()},
            ensure_ascii=False,
        ),
        "browsed_json": json.dumps(browsed, ensure_ascii=False),
        "dive_results_json": json.dumps(dive_results, ensure_ascii=False),
        "gap_results_json": json.dumps(gap_results, ensure_ascii=False),
        "decision_log_json": json.dumps(decision_log, ensure_ascii=False),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Internal model-call helpers (each makes a temp agent call)
# ═══════════════════════════════════════════════════════════════════════════════


async def _model_assess_results(
    topic: str,
    dimensions: list[dict[str, Any]],
    all_results: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    """Model reviews initial search results and rates quality/completeness per dimension."""
    dim_summaries = []
    for dim in dimensions:
        label = dim["label"]
        results = all_results.get(label, [])
        snippets = "\n".join(
            f"  [{i+1}] {r['title']}\n      {r['snippet'][:200]}"
            for i, r in enumerate(results[:5])
        )
        dim_summaries.append(f"Dimension: {label} (rated complexity: {dim.get('complexity', 'unknown')})\nResults:\n{snippets}")

    prompt = f"""Assess the quality and completeness of initial web search results for the research topic.

Topic: {topic}

Search results by dimension:
{chr(10).join(dim_summaries)}

For each dimension, use conceptual levels instead of numeric scores:
- source_authority_level:
  - HIGH: primary, official, peer-reviewed, or clearly authoritative sources dominate.
  - MEDIUM: credible industry or technical sources are present but authority is mixed.
  - LOW: sources are thin, promotional, generic, or mostly indirect.
- coverage_level:
  - COMPLETE: key aspects appear directly covered by multiple relevant results.
  - PARTIAL: some important aspects are covered but visible gaps remain.
  - THIN: results are sparse, generic, off-topic, or missing core angles.
- depth_need_level:
  - SUFFICIENT: search results are enough for surface synthesis.
  - FOCUSED: targeted browsing should fill a few specific gaps.
  - DEEP_DIVE: substantial full-text browsing or follow-up research is needed.

Return a JSON object:
{{
  "summary": "one-sentence overall assessment",
  "per_dimension": {{
    "<dimension_label>": {{
      "source_authority_level": "HIGH|MEDIUM|LOW",
      "coverage_level": "COMPLETE|PARTIAL|THIN",
      "depth_need_level": "SUFFICIENT|FOCUSED|DEEP_DIVE",
      "note": "brief note on what's missing when coverage is PARTIAL or THIN"
    }}
  }}
}}"""

    try:
        agent = Agently.create_agent("research-assessor")
        result_str = str(await agent.input(prompt).async_start())
        json_match = re.search(r"\{.*\}", result_str, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return {"summary": "Assessment unavailable", "per_dimension": {}}


async def _model_select_articles(
    topic: str,
    dimensions: list[dict[str, Any]],
    all_results: dict[str, list[dict[str, str]]],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Model selects which specific articles to browse, with reasons."""
    candidates = []
    idx = 0
    for dim in dimensions:
        label = dim["label"]
        for r in all_results.get(label, [])[:5]:
            idx += 1
            candidates.append({
                "id": idx,
                "dimension": label,
                "title": r["title"],
                "url": r["url"],
                "snippet": r["snippet"][:250],
            })

    if not candidates:
        return {"articles": [], "reasoning": "No candidates available"}

    per_dim = assessment.get("per_dimension", {})
    depth_hints = "\n".join(
        f"  {label}: depth_need={per_dim.get(label, {}).get('depth_need_level', '?')}"
        for label in [d["label"] for d in dimensions]
    )

    prompt = f"""Select which articles to browse in full. You have {len(candidates)} candidates across {len(dimensions)} dimensions.

Topic: {topic}
Dimension depth needs (DEEP_DIVE first, then FOCUSED, then SUFFICIENT):
{depth_hints}

Candidates:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Rules:
- Browse at most 8 articles total
- RELEVANCE FIRST: before considering depth or diversity, assess whether each
  article is actually about the research topic. Skip articles that only
  tangentially mention the topic or are about a different domain altogether.
- Prioritize dimensions with depth_need=DEEP_DIVE, then FOCUSED
- Skip articles that are clearly low-quality (e.g., obvious aggregators, thin content)
- Select diverse sources (avoid picking 3 from the same domain)
- For each selected article, give a 5-10 word reason that includes why it is relevant

Return JSON:
{{
  "reasoning": "2-3 sentences on your selection strategy",
  "articles": [
    {{"dimension": "...", "title": "...", "url": "...", "snippet": "...", "reason": "..."}}
  ]
}}"""

    try:
        agent = Agently.create_agent("article-selector")
        result_str = str(await agent.input(prompt).async_start())
        json_match = re.search(r"\{.*\}", result_str, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    # Fallback: top 2 per dimension
    fallback = []
    for dim in dimensions:
        for r in all_results.get(dim["label"], [])[:2]:
            fallback.append({**r, "dimension": dim["label"], "reason": "fallback selection"})
    return {"articles": fallback[:8], "reasoning": "Fallback: model selection failed, using top-N"}


async def _model_decide_dive(
    topic: str,
    browsed: list[dict[str, Any]],
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Model decides whether browsed articles contain references worth diving into."""
    if not browsed:
        return {"should_dive": False, "reasoning": "No articles were browsed", "hooks": []}

    excerpts = "\n\n---\n\n".join(
        f"[{a['dimension']}] {a['title']}\n{a['content'][:800]}"
        for a in browsed[:6]
    )

    prompt = f"""Review excerpts from {len(browsed)} browsed articles on "{topic}".

Article excerpts:
{excerpts[:5000]}

Do these articles mention specific companies, technologies, standards, regulations,
data sources, or events that:
1. Are NOT already covered by the initial search results?
2. Would significantly deepen the report if investigated?
3. Are specific enough to form a useful follow-up search query?

If yes, list up to 5 specific search queries. If the articles already provide
sufficient depth, say no.

Return JSON:
{{
  "should_dive": true/false,
  "reasoning": "1-2 sentences explaining your decision",
  "hooks": ["specific search query 1", "specific search query 2", ...]
}}"""

    try:
        agent = Agently.create_agent("dive-decider")
        result_str = str(await agent.input(prompt).async_start())
        json_match = re.search(r"\{.*\}", result_str, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return {"should_dive": False, "reasoning": "Decision unavailable", "hooks": []}


async def _model_check_sufficiency(
    topic: str,
    dimensions: list[dict[str, Any]],
    all_results: dict[str, list[dict[str, str]]],
    browsed: list[dict[str, Any]],
    dive_results: list[dict[str, Any]],
    current_round: int,
    assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Model checks whether enough research has been gathered, using the
    initial quality assessment to detect depth mismatches."""
    dim_labels = [d["label"] for d in dimensions]
    total_search_hits = sum(len(v) for v in all_results.values())

    # Summarize assessment ratings for the model to cross-reference
    assessment_summary = ""
    if assessment:
        per_dim = assessment.get("per_dimension", {})
        if per_dim:
            lines = []
            for label, info in per_dim.items():
                dn = info.get("depth_need_level", "?")
                auth = info.get("source_authority_level", "?")
                cov = info.get("coverage_level", "?")
                note = info.get("note", "")
                note_suffix = f" — {note}" if note else ""
                lines.append(f"  {label}: depth_need={dn}, authority={auth}, coverage={cov}{note_suffix}")
            assessment_summary = "\n".join(lines)

    prompt = f"""You are assessing whether enough research has been gathered to write a comprehensive report.

Topic: {topic}
Dimensions: {dim_labels}
Current research round: {current_round} (max 3)
Search results: {total_search_hits} total hits across {len(dimensions)} dimensions
Articles browsed in full: {len(browsed)}
Dive investigations: {len(dive_results)}

{"Initial quality assessment per dimension:" if assessment_summary else ""}
{assessment_summary}

Cross-reference the assessment ratings with what was actually done:
- If a dimension was rated depth_need_level=DEEP_DIVE and has zero browsed
  articles and zero dive results, that is a RED FLAG — you MUST mark insufficient.
- If a dimension was rated coverage_level=THIN and nothing was done to fill it,
  that is also a gap.
- If the assessment noted specific missing content and those gaps were not
  addressed by browsing or diving, flag them.
- Only mark sufficient if the actual investigation depth matches the need.

If this is round 3 (final), be pragmatic — mark sufficient and note what
could not be covered so the report can acknowledge the gaps honestly.

Return JSON:
{{
  "sufficient": true/false,
  "reasoning": "1-2 sentences",
  "gaps": "what specifically is missing (empty string if sufficient)",
  "gap_queries": ["query1", "query2"]  // only if not sufficient, max 3
}}"""

    try:
        agent = Agently.create_agent("sufficiency-checker")
        result_str = str(await agent.input(prompt).async_start())
        json_match = re.search(r"\{.*\}", result_str, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception:
        pass
    return {"sufficient": True, "reasoning": "Default: proceeding to synthesis", "gaps": "", "gap_queries": []}


# ═══════════════════════════════════════════════════════════════════════════════
# Action: compile_report
# ═══════════════════════════════════════════════════════════════════════════════


async def _action_compile_report(
    report: str = "",
    validation: str = "",
    topic: str = "Research Topic",
    decision_log: str = "",
    total_rounds: int = 1,
    dim_count: int = 0,
    browsed_count: int = 0,
    dive_count: int = 0,
    **kwargs,
) -> dict[str, Any]:
    """Compile and save the full research report with decision log."""
    # Parse decision log for display
    decisions_md = ""
    try:
        dl = json.loads(decision_log) if isinstance(decision_log, str) else decision_log
        if isinstance(dl, list):
            decisions_md = "\n".join(
                f"- **{d.get('phase', '?')}**: {str(d.get('reasoning', d.get('summary', '')))[:200]}"
                for d in dl
            )
    except (json.JSONDecodeError, TypeError):
        decisions_md = "(decision log unavailable)"

    doc = f"""# Agentic Research Report
**Topic:** {topic[:200]}
**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
**Research Mode:** Model-driven adaptive
**Dimensions:** {dim_count} | **Rounds:** {total_rounds} | **Articles browsed:** {browsed_count} | **Dives:** {dive_count}

---

## Model Decision Log

{decisions_md}

---

## Synthesized Research Report

{report or '*Report generation failed.*'}

---

## Validation & Quality Assessment

{validation or '*Validation not performed.*'}

---

*Generated by Agently Agentic Research Pipeline — the model decided what to investigate, how deep to go, and when to stop.*
"""

    reports_dir = Path.home() / ".agently_deep_research"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()[:80]).strip("-")
    filepath = reports_dir / f"agentic-{slug}_{timestamp}.md"
    filepath.write_text(doc)

    return {
        "file_path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "report_length": len(report or ""),
        "validation_length": len(validation or ""),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rich display
# ═══════════════════════════════════════════════════════════════════════════════


def _build_plan_panel(strategy: str, dim_count: int, dim_labels: list[str], done: bool, running: bool = False) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
        lines = [f"Strategy: {strategy[:200]}..." if len(strategy) > 200 else f"Strategy: {strategy}"]
        for i, (label, comp) in enumerate(dim_labels):
            lines.append(f"  [{i+1}] {label} (complexity: {comp})")
        body = Text("\n".join(lines))
        border = "green"
    else:
        icon = "[bold yellow]◎[/]" if running else "[dim]1[/]"
        body = Text("Active: model decomposing topic into research dimensions..." if running else "Queued: first model stage.", style=None if running else "dim")
        border = "yellow" if running else "dim"
    return Panel(body, title=f"{icon} Step 1 Plan (model_plan → {dim_count or '?'} dims)", border_style=border)


def _build_decision_panel(decisions: list[dict[str, Any]], dim_count: int, running: bool = False, done: bool = False) -> Panel:
    """Shows the model's decision history in a running log."""
    if not decisions:
        if running:
            title = "[bold yellow]◎[/] Step 3 Model Decisions"
            return Panel(Text("Active: waiting for the first model decision from the research action..."), title=title, border_style="yellow")
        return Panel(Text("Queued: decision log appears during Step 2 research.", style="dim"), title="[dim]3[/] Step 3 Model Decisions", border_style="dim")

    lines: list[str] = []
    for d in decisions:
        phase = d.get("phase", "?")
        if phase == "initial_assessment":
            per_dim = d.get("per_dimension", {})
            dim_ratings = ", ".join(
                f"{label}: depth={info.get('depth_need_level','?')}"
                for label, info in per_dim.items()
            )
            lines.append(f"[bold]Assessed {len(per_dim)} dims:[/] {dim_ratings}")
        elif phase == "article_selection":
            lines.append(f"[bold]Selected[/] {d.get('selected_count', 0)} articles for full browse")
        elif phase == "dive_decision":
            action = "[green]dive[/]" if d.get("should_dive") else "[dim]skip dive[/]"
            lines.append(f"[bold]Dive decision:[/] {action}")
        elif "sufficiency" in phase:
            status = "[green]sufficient ✓[/]" if d.get("sufficient") else "[yellow]needs more →[/]"
            lines.append(f"[bold]{phase}:[/] {status}")
        else:
            summary = str(d.get("summary", d.get("reasoning", "")))[:120]
            if summary:
                lines.append(f"[bold]{phase}:[/] {summary}")

    body = Text.from_markup("\n".join(lines[-12:]))  # show last 12 decisions
    icon = "[bold green]✓[/]" if done else "[bold yellow]◎[/]"
    border = "green" if done else "yellow"
    return Panel(body, title=f"{icon} Step 3 Model Decisions", border_style=border)


def _build_progress_panel(
    dim_count: int, total_rounds: int, max_rounds: int,
    search_hits: int, browsed: int, dives: int,
    done: bool, running: bool,
    phase: str = "",
    detail: str = "",
) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
        border = "green"
    elif running:
        icon = "[bold yellow]◎[/]"
        border = "yellow"
    else:
        icon = "[dim]2[/]"
        border = "dim"

    lines = [
        f"Phase: {phase or ('complete' if done else 'queued')}",
        f"Dimensions: {dim_count}",
        f"Round: {total_rounds}/{max_rounds}",
        f"Search hits: {search_hits}",
        f"Articles browsed: {browsed}",
        f"Dive investigations: {dives}",
    ]
    if detail:
        lines.append("")
        lines.append(detail[:180])
    if not running and not done and not detail:
        lines.append("")
        lines.append("Queued: starts after Step 1 plan.")
    return Panel(Text("\n".join(lines)), title=f"{icon} Step 2 Research Progress", border_style=border)


def _build_synthesis_panel(report_text: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
        preview = (report_text or "")[:500]
        body = Text(preview + (f"\n\n... ({len(report_text or ''):,} chars)" if len(report_text or "") > 500 else ""))
        border = "green"
    elif running:
        icon = "[bold yellow]◎[/]"
        body = Text("Active: synthesizing from adaptive research data...")
        border = "yellow"
    else:
        icon = "[dim]4[/]"
        body = Text("Queued: starts after research and decision log are complete.", style="dim")
        border = "dim"
    return Panel(body, title=f"{icon} Step 4 Synthesis (model)", border_style=border)


def _build_validation_panel(validation: str | None, done: bool, running: bool) -> Panel:
    if done:
        icon = "[bold green]✓[/]"
        body = Text((validation or "")[:400])
        border = "green"
    elif running:
        icon = "[bold yellow]◎[/]"
        body = Text("Active: cross-validating claims...")
        border = "yellow"
    else:
        icon = "[dim]5[/]"
        body = Text("Queued: starts after Step 4 synthesis.", style="dim")
        border = "dim"
    return Panel(body, title=f"{icon} Step 5 Cross-Validate (model)", border_style=border)


def _build_output_panel(file_path: str, done: bool, running: bool = False) -> Panel:
    if done and file_path:
        return Panel(Text.from_markup(f"[green]{file_path}[/]"), title="[bold green]✓[/] Step 6 Output", border_style="green")
    if running:
        return Panel(Text("Active: formatting and saving Markdown report..."), title="[bold yellow]◎[/] Step 6 Output", border_style="yellow")
    return Panel(Text("Queued: final save happens after validation.", style="dim"), title="[dim]6[/] Step 6 Output", border_style="dim")


def _build_layout(
    plan: Panel, decisions: Panel, progress: Panel,
    syn: Panel, cv: Panel, out: Panel,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=3),
        Layout(name="mid", ratio=3),
        Layout(name="bottom", ratio=2),
    )
    layout["top"].split_row(Layout(plan, name="plan", ratio=2), Layout(progress, name="progress", ratio=1))
    layout["mid"].split_row(Layout(decisions, name="decisions"))
    layout["bottom"].split_row(
        Layout(syn, name="syn", ratio=2),
        Layout(cv, name="cv", ratio=1),
        Layout(out, name="output", ratio=1),
    )
    return layout


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main() -> None:
    import os

    # ── Parse CLI args: [--lang <code>] [topic words...] ──
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
        elif args[i] == "-l" and len(args[i]) > 2:
            language = args[i][2:]
            i += 1
        else:
            topic_parts.append(args[i])
            i += 1

    # Resolve topic: CLI arg > env var > interactive prompt
    topic_source = "interactive"
    if topic_parts:
        topic = " ".join(topic_parts).strip()
        topic_source = "cli"
    elif os.environ.get("AGENTLY_RESEARCH_TOPIC", "").strip():
        topic = os.environ["AGENTLY_RESEARCH_TOPIC"].strip()
        topic_source = "env"
    else:
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  Agently Agentic Research Pipeline                         ║")
        print("║  Model-driven adaptive research — the model decides:       ║")
        print("║  how many dimensions, how deep to go, when to stop         ║")
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
    print(f"Topic ({len(topic):,} chars):")
    print(f"  {topic[:200]}{'...' if len(topic) > 200 else ''}")
    print(f"{'─'*60}\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 0 — Topic refinement (streaming via inline skill)
    # ═══════════════════════════════════════════════════════════════════════════
    provider = configure_model(temperature=0.3)

    # Show model info
    model_name = (
        os.environ.get("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")
        if provider == "deepseek"
        else os.environ.get("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")
    )
    print(f"Model: {provider} :: {model_name}\n")

    # Set up a temp registry that both the refiner skill and the main research
    # skill will use — avoids polluting the user's default skills registry.
    runtime_dir = Path(tempfile.mkdtemp(prefix="agently_skills_"))
    registry_root = runtime_dir / "registry"
    Agently.settings.set("skills.registry.root", str(registry_root))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)

    if not topic.startswith("Research ") and not topic.startswith("研究 "):
        print("Expanding your question into a structured research brief...\n")
        try:
            # Use a minimal inline skill so the model response streams via
            # skills.stages.refine.fields.brief deltas (bare async_start()
            # does not emit field deltas).
            REFINER_SKILL_YAML = """\
skill_id: topic-refiner
version: 1.0.0
display_name: Topic Refiner
trust_level: local
kind: workflow
stages:
  - id: refine
    kind: model
    purpose: >
      The user wants to research the following topic. Their question may be
      brief or informal. Expand it into a structured brief with:

      1. A refined 2-3 sentence research question
      2. Key dimensions to investigate (the downstream research model will decide
         exactly how many, but suggest the important angles)
      3. For each dimension: 2-3 specific angles or sub-questions
      4. Cross-cutting themes or tensions
      5. Key entities: companies, technologies, regulations, events, standards

      Be specific and searchable. Use concrete names.
    input:
      topic: "${task}"
    output_schema:
      brief:
        type: str
        description: The expanded research brief
"""
            refiner_dir = runtime_dir / "topic-refiner"
            refiner_dir.mkdir(parents=True)
            (refiner_dir / "skill.yaml").write_text(REFINER_SKILL_YAML.strip())
            Agently.skills_executor.install_skills(refiner_dir, trust_level="local", update=True)

            refiner = Agently.create_agent("topic-refiner-agent")
            execution = (
                refiner
                .use_skills(["topic-refiner"], mode="required")
                .input(topic)
                .create_execution()
            )
            refined_str = ""
            print(f"{'─'*60}")
            print("Refined research brief (generating...):")
            print(f"{'─'*60}")
            async for item in execution.get_async_generator(type="instant"):
                if item.path == "skills.stages.refine.fields.brief" and item.delta:
                    print(item.delta, end="", flush=True)
                    refined_str += item.delta
            print(f"\n{'─'*60}")

            if refined_str and len(refined_str) > 50:
                if topic_source == "interactive" and sys.stdin.isatty():
                    print()
                    print("Press ENTER to launch with this brief,")
                    print("type edits / additional instructions,")
                    print("or 'skip' to use your original question:")
                    print()
                    try:
                        user_input = input("> ").strip()
                    except (EOFError, KeyboardInterrupt):
                        user_input = ""
                    if user_input.lower() == "skip":
                        print("[using original question]\n")
                    elif user_input:
                        topic = f"[ORIGINAL]\n{topic}\n\n[INSTRUCTIONS]\n{user_input}"
                        print("[brief updated]\n")
                    else:
                        topic = refined_str
                        print("[using model-expanded brief]\n")
                else:
                    topic = refined_str
                    print("[using model-expanded brief]\n")
            else:
                print("[refinement produced empty output — using original question]\n")
        except Exception as e:
            print(f"[refinement skipped — {e}]\n")

    # ── Apply output language if specified ──
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
            f"ALL report content, synthesis, and validation MUST be written in {lang_display}]\n\n"
            f"{topic}"
        )
        print(f"Output language: {lang_display}")
    elif language == "auto" or not language:
        print("Output language: auto (matching topic language)")

    # ═══════════════════════════════════════════════════════════════════════════

    skill_dir = runtime_dir / "agentic-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(AGENTIC_RESEARCH_SKILL_YAML.strip())
    Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    agent = Agently.create_agent("agentic-researcher")

    agent.register_action(
        name="execute_research",
        desc="Execute model-driven adaptive research: search, assess, browse, dive, and optionally re-research gaps.",
        kwargs={
            "topic": ("str", "Research topic."),
            "strategy": ("str", "Research strategy from plan."),
            "dimensions_json": ("str", "JSON array of dimension objects."),
            "themes": ("str", "Cross-cutting themes."),
        },
        func=_action_execute_research,
    )
    agent.register_action(
        name="compile_report",
        desc="Compile the final research report and save to disk.",
        kwargs={
            "report": ("str", "The synthesized report text."),
            "validation": ("str", "Cross-validation notes."),
            "topic": ("str", "The research topic."),
            "decision_log": ("str", "JSON decision log."),
            "total_rounds": ("int", "Research rounds completed."),
            "dim_count": ("int", "Number of dimensions."),
            "browsed_count": ("int", "Articles browsed."),
            "dive_count": ("int", "Dive investigations."),
        },
        func=_action_compile_report,
    )

    execution = (
        agent
        .use_skills(["agentic-research"], mode="required")
        .input(topic)
        .create_execution()
    )

    # ── State trackers ──────────────────────────────────────────────────────
    completed_stages: set[str] = set()
    stage_running: dict[str, bool] = {}

    strategy_text = ""
    dim_count = 0
    dim_labels: list[tuple[str, str]] = []  # (label, complexity)
    decisions: list[dict[str, Any]] = []

    search_hits = 0
    browsed_count = 0
    dive_count = 0
    total_rounds = 1
    max_rounds = 3
    research_phase = ""
    research_detail = ""

    report_text: str | None = None
    validation_text: str | None = None
    saved_file = ""
    save_done = False

    # ── Initial panels ──────────────────────────────────────────────────────
    plan_panel = _build_plan_panel("", 0, [], False, True)
    decisions_panel = _build_decision_panel([], 0)
    progress_panel = _build_progress_panel(0, 0, max_rounds, 0, 0, 0, False, False)
    syn_panel = _build_synthesis_panel(None, False, False)
    cv_panel = _build_validation_panel(None, False, False)
    out_panel = _build_output_panel("", False)
    layout = _build_layout(plan_panel, decisions_panel, progress_panel, syn_panel, cv_panel, out_panel)

    def sync_research_progress() -> None:
        nonlocal dim_count, search_hits, browsed_count, dive_count, total_rounds
        nonlocal decisions, research_phase, research_detail
        progress = _get_research_progress()
        if not progress:
            return
        dim_count = int(progress.get("dim_count") or dim_count or 0)
        search_hits = int(progress.get("search_hits") or search_hits or 0)
        browsed_count = int(progress.get("browsed_count") or browsed_count or 0)
        dive_count = int(progress.get("dive_count") or dive_count or 0)
        total_rounds = int(progress.get("total_rounds") or total_rounds or 1)
        research_phase = str(progress.get("phase") or research_phase)
        research_detail = str(progress.get("detail") or research_detail)
        progress_decisions = progress.get("decisions") or []
        if progress_decisions:
            decisions = progress_decisions

    console = Console(force_terminal=True)
    with Live(layout, console=console, refresh_per_second=6, screen=False, transient=False) as live:
        gen = execution.get_async_generator(type="instant")
        next_item = asyncio.create_task(gen.__anext__())
        while True:
            # Wait for the next event with a short timeout so the Live display
            # refreshes periodically even when the action is busy (no intermediate
            # events emitted during long-running action stages).
            try:
                item = await asyncio.wait_for(asyncio.shield(next_item), timeout=0.4)
            except asyncio.TimeoutError:
                # No event yet — refresh display with current state and keep waiting
                sync_research_progress()
                decisions_panel = _build_decision_panel(
                    decisions, dim_count,
                    stage_running.get("execute_research", False),
                    "execute_research" in completed_stages,
                )
                progress_panel = _build_progress_panel(
                    dim_count, total_rounds, max_rounds,
                    search_hits, browsed_count, dive_count,
                    "execute_research" in completed_stages,
                    stage_running.get("execute_research", False),
                    research_phase, research_detail,
                )
                live.update(
                    _build_layout(plan_panel, decisions_panel, progress_panel, syn_panel, cv_panel, out_panel),
                    refresh=True,
                )
                continue
            except StopAsyncIteration:
                break
            next_item = asyncio.create_task(gen.__anext__())

            # ── Stage starts ──
            if item.path.startswith("task_dag.tasks.") and item.path.endswith(".start"):
                for sid in ("plan_research", "execute_research", "validate_coverage",
                            "synthesize", "cross_validate", "compile_report"):
                    if sid in item.path:
                        stage_running[sid] = True

            # ── Stage completions ──
            if item.path.startswith("skills.stages.") and ".fields." not in item.path and item.is_complete:
                stage_id = item.path.split(".")[-1]
                completed_stages.add(stage_id)
                stage_running[stage_id] = False

            # ── Field deltas from model stages ──
            if item.path == "skills.stages.plan_research.fields.research_strategy" and item.delta:
                strategy_text += item.delta
            elif item.path == "skills.stages.plan_research.fields.dimensions_json" and item.delta:
                # Parse on completion, not delta
                pass
            elif item.path == "skills.stages.synthesize.fields.report" and item.delta:
                report_text = (report_text or "") + item.delta
            elif item.path == "skills.stages.cross_validate.fields.validation_notes" and item.delta:
                validation_text = (validation_text or "") + item.delta

            # ── Plan completion — parse dimensions ──
            if "plan_research" in completed_stages and not dim_labels:
                # Try to get value from stage completion or action result
                pass

            # ── Action completions ──
            if item.path.startswith("actions.") and item.is_complete:
                val = item.value or {}
                result = val.get("result", val) if isinstance(val, dict) else val
                if not isinstance(result, dict):
                    continue

                action_name = item.path.split(".", 1)[1]
                if action_name == "execute_research":
                    dim_count = result.get("dim_count", 0)
                    browsed_count = result.get("browsed_count", 0)
                    dive_count = result.get("dive_count", 0)
                    total_rounds = result.get("total_rounds", 1)
                    # Compute total search hits
                    try:
                        sr = json.loads(result.get("search_results_json", "{}"))
                        search_hits = sum(len(v) for v in sr.values())
                    except Exception:
                        pass
                    # Parse decision log
                    try:
                        dl = json.loads(result.get("decision_log_json", "[]"))
                        if isinstance(dl, list):
                            decisions = dl
                    except Exception:
                        pass
                elif action_name == "compile_report":
                    saved_file = result.get("file_path", "")
                    save_done = True

            # ── Parse dimensions from plan fields on completion ──
            if "plan_research" in completed_stages and not dim_labels:
                for stage_id, stage_data in (item.value or {}).items():
                    if isinstance(stage_data, dict):
                        dims_json = stage_data.get("dimensions_json", "")
                        if isinstance(dims_json, str) and dims_json:
                            try:
                                parsed = json.loads(dims_json)
                                if isinstance(parsed, list):
                                    dim_labels = [(d.get("label", "?"), d.get("complexity", "?")) for d in parsed]
                                    dim_count = len(dim_labels)
                            except (json.JSONDecodeError, TypeError):
                                pass

            # ── Refresh panels ──
            sync_research_progress()
            plan_panel = _build_plan_panel(
                strategy_text, dim_count, dim_labels,
                "plan_research" in completed_stages,
                stage_running.get("plan_research", False),
            )
            decisions_panel = _build_decision_panel(
                decisions, dim_count,
                stage_running.get("execute_research", False),
                "execute_research" in completed_stages,
            )
            progress_panel = _build_progress_panel(
                dim_count, total_rounds, max_rounds,
                search_hits, browsed_count, dive_count,
                "execute_research" in completed_stages,
                stage_running.get("execute_research", False),
                research_phase, research_detail,
            )
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
            out_panel = _build_output_panel(saved_file, save_done, stage_running.get("compile_report", False))
            live.update(
                _build_layout(plan_panel, decisions_panel, progress_panel, syn_panel, cv_panel, out_panel),
                refresh=True,
            )

    # ── Final summary ──────────────────────────────────────────────────────
    meta = await execution.async_get_meta()
    route = (meta.get("route_plan") or {}).get("selected_route", "")

    print(f"\n{'='*70}")
    print(f"route: {route}")
    print(f"stages completed: {sorted(completed_stages)}")
    print(f"model decided: {dim_count} dimensions across {total_rounds} round(s)")
    if dim_labels:
        for i, (label, comp) in enumerate(dim_labels):
            print(f"  [{i+1}] {label} (complexity: {comp})")
    if decisions:
        print(f"\nModel decision log:")
        for d in decisions:
            phase = d.get("phase", "?")
            summary = str(d.get("reasoning", d.get("summary", "")))[:150]
            print(f"  [{phase}] {summary}")
    print(f"\nsearch hits: {search_hits} | browsed: {browsed_count} | dives: {dive_count}")
    print(f"report: {len(report_text or ''):,} chars | validation: {len(validation_text or ''):,} chars")
    print(f"saved: {saved_file or '(not saved)'}")
    print(f"{'='*70}")

    if report_text:
        print(f"\n[Report preview — first 600 chars]:")
        print(report_text[:600])


if __name__ == "__main__":
    asyncio.run(main())
