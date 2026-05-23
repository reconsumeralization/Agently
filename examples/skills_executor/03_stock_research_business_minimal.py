"""Business-first Skills Executor example: stock research brief.

Run:
    python examples/skills_executor/03_stock_research_business_minimal.py

    The script also works when invoked by absolute path from outside the repo.

Environment:
    DEEPSEEK_API_KEY may be available in the shell or a .env file.
    If it is absent, set DYNAMIC_TASK_MODEL_PROVIDER=ollama and make sure the
    local Ollama OpenAI-compatible endpoint is running.

Expected key output from a real DeepSeek run on 2026-05-21:
    provider=deepseek
    quote_source=stooq
    data_status=fresh
    latest_closes=NVDA=223.47,AMD=447.58,AVGO=417.76
    quote_dates=NVDA=2026-05-20,AMD=2026-05-20,AVGO=2026-05-20
    companies=NVDA,AMD,AVGO
    non_investment_advice=True
    risk_items=3

This is intentionally the shortest business-facing shape: install a local
skills pack of one standard ``SKILL.md`` and run it prompt-only to receive a
structured research brief. The controlled side effect — fetching current quote
data — runs in the HOST before the model analysis, not inside the Skill. The
fetched data is passed to the prompt-only Skill as task context. No third-party
Skill package scripts are executed.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model


RUNTIME_ROOT = ROOT / ".example_runtime" / "skills_executor" / "stock_research_business_minimal"
SKILLS_PACK_NAME = "equity-research-demo"
QUOTE_CACHE_PATH = RUNTIME_ROOT / "quote_cache.json"
TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}

EQUITY_RESEARCH_SKILL = """---
name: equity-research-brief
description: Compare public companies from provided market, financial, filing, and earnings-call facts. Produce research-only conclusions, risks, watch indicators, and a clear non-investment-advice boundary.
keywords:
  - stock
  - equity
  - research
  - earnings
  - SEC
  - valuation
---

# Equity Research Brief

Use this skill when the user asks for a research-only comparison of public
companies.

Guidance:

- Use only the provided data or explicitly state the data boundary.
- If quote retrieval degraded to cache or unavailable data, explicitly say so.
- Compare companies across market performance, valuation context, financial
  quality, earnings-call focus, SEC risk factors, and analyst sentiment.
- Separate evidence from conclusion. Do not imply certainty from incomplete
  data.
- Do not output buy, sell, hold, allocation, order, or timing instructions.
- End with a non-investment-advice boundary and concrete watch indicators.
"""


class QuoteSourceUnavailable(RuntimeError):
    pass


def install_demo_skills_pack() -> None:
    """Example setup: create and install one local Skill Pack."""

    skill_dir = RUNTIME_ROOT / "skills_pack" / "equity-research-brief"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(EQUITY_RESEARCH_SKILL, encoding="utf-8")

    Agently.skills_executor.configure(registry_root=tempfile.mkdtemp(prefix="agently_skills_reg_"), allowed_trust_levels=["local"])
    Agently.skills_executor.install_skills_pack(
        skill_dir.parent,
        name=SKILLS_PACK_NAME,
        trust_level="local",
        update=True,
    )


def _extract_tickers(task: str) -> list[str]:
    candidates = re.findall(r"\b[A-Z]{2,5}\b", task.upper())
    stopwords = {"AND", "THE", "FOR", "FETCH", "CURRENT", "MARKET", "QUOTE", "DATA"}
    tickers = []
    for item in candidates:
        if item in stopwords:
            continue
        if item not in tickers:
            tickers.append(item)
    return tickers


def _read_stooq_csv(url: str, *, retries: int = 2, retry_delay: float = 0.5) -> list[dict[str, str]]:
    request = Request(url, headers={"User-Agent": "Agently Skills Executor example"})
    failures = []
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=20) as response:
                text = response.read().decode("utf-8")
            return list(csv.DictReader(text.splitlines()))
        except HTTPError as error:
            failures.append(f"HTTP { error.code }")
            if error.code not in TRANSIENT_HTTP_STATUS:
                raise
        except (TimeoutError, URLError) as error:
            failures.append(type(error).__name__)
        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))
    raise QuoteSourceUnavailable(f"Stooq request failed after retries: { ', '.join(failures) }")


def _quote_url(ticker: str) -> str:
    return "https://stooq.com/q/l/?" + urlencode({"s": f"{ ticker.lower() }.us", "f": "sd2t2ohlcv", "h": "", "e": "csv"})


def _history_url(ticker: str, *, end_date: datetime) -> str:
    start_date = end_date - timedelta(days=370)
    return "https://stooq.com/q/d/l/?" + urlencode(
        {
            "s": f"{ ticker.lower() }.us",
            "i": "d",
            "d1": start_date.strftime("%Y%m%d"),
            "d2": end_date.strftime("%Y%m%d"),
        }
    )


def _to_float(value: Any) -> float | None:
    try:
        if value in {None, "", "N/D"}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_quote_cache() -> dict[str, Any]:
    try:
        return json.loads(QUOTE_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_quote_cache(cache: dict[str, Any]) -> None:
    QUOTE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUOTE_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _cache_key(ticker: str) -> str:
    return ticker.upper()


def _fetch_quote_from_stooq(ticker: str, *, end_date: datetime) -> dict[str, Any]:
    quote_rows = _read_stooq_csv(_quote_url(ticker))
    if not quote_rows:
        raise RuntimeError(f"No quote row returned for { ticker }.")
    quote = quote_rows[0]
    close = _to_float(quote.get("Close"))
    if close is None:
        raise RuntimeError(f"Missing current quote close for { ticker }: { quote }")

    history_rows = _read_stooq_csv(_history_url(ticker, end_date=end_date))
    history_closes = [
        {
            "date": row.get("Date"),
            "close": _to_float(row.get("Close")),
        }
        for row in history_rows
        if _to_float(row.get("Close")) is not None
    ]
    one_year_change_pct = None
    if history_closes:
        first_close = history_closes[0]["close"]
        if first_close:
            one_year_change_pct = round(((close - first_close) / first_close) * 100, 2)

    return {
        "ticker": ticker,
        "source_symbol": quote.get("Symbol"),
        "data_status": "fresh",
        "quote": {
            "provider_date": quote.get("Date"),
            "provider_time": quote.get("Time"),
            "open": _to_float(quote.get("Open")),
            "high": _to_float(quote.get("High")),
            "low": _to_float(quote.get("Low")),
            "close": close,
            "volume": int(float(quote.get("Volume") or 0)),
        },
        "one_year_change_pct": one_year_change_pct,
    }


def _fetch_quote(ticker: str, *, end_date: datetime, cache: dict[str, Any]) -> dict[str, Any]:
    key = _cache_key(ticker)
    try:
        quote = _fetch_quote_from_stooq(ticker, end_date=end_date)
    except QuoteSourceUnavailable as error:
        cached = _ensure_cache_record(cache.get(key))
        if cached:
            cached["data_status"] = "cached_fallback"
            cached["fallback_reason"] = str(error)
            return cached
        return {
            "ticker": ticker,
            "source_symbol": f"{ ticker }.US",
            "data_status": "unavailable",
            "fallback_reason": str(error),
            "quote": {},
            "one_year_change_pct": None,
        }
    cache[key] = {**quote, "cached_at_utc": datetime.now(timezone.utc).isoformat()}
    return quote


def _ensure_cache_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cached = json.loads(json.dumps(value))
    cached.pop("cached_at_utc", None)
    return cached


def fetch_equity_market_data(task: str) -> dict[str, Any]:
    tickers = _extract_tickers(task)
    if not tickers:
        raise RuntimeError(f"No ticker symbols found in task: { task }")
    retrieved_at = datetime.now(timezone.utc)
    cache = _load_quote_cache()
    companies = [_fetch_quote(ticker, end_date=retrieved_at, cache=cache) for ticker in tickers]
    _save_quote_cache(cache)
    statuses = {str(item.get("data_status") or "unknown") for item in companies}
    data_status = "fresh" if statuses == {"fresh"} else "degraded"
    fallback_notes = [
        f"{ item.get('ticker') }: { item.get('data_status') } - { item.get('fallback_reason') }"
        for item in companies
        if item.get("data_status") != "fresh"
    ]
    return {
        "data_boundary": (
            "Current public quote data fetched from Stooq CSV at "
            f"{ retrieved_at.isoformat() }. Provider timestamps may be delayed; not live exchange-direct data. "
            f"Retrieval status: { data_status }."
        ),
        "data_status": data_status,
        "fallback_notes": fallback_notes,
        "source": {
            "name": "stooq",
            "quote_endpoint": "https://stooq.com/q/l/",
            "history_endpoint": "https://stooq.com/q/d/l/",
        },
        "retrieved_at_utc": retrieved_at.isoformat(),
        "companies": companies,
    }


ANALYSIS_OUTPUTS: dict[str, Any] = {
    "input_boundary": (str, "Copy market_data.data_boundary exactly.", True),
    "comparison_basis": [(str, "Evidence dimensions used before the final summary.", True)],
    "company_views": [
        (
            {
                "ticker": (str, "Company ticker.", True),
                "relative_strengths": [(str, "Evidence-backed strength.", True)],
                "key_risks": [(str, "Evidence-backed risk.", True)],
                "watch_indicators": [(str, "Metric or event to monitor.", True)],
            },
            "Per-company research view.",
            True,
        )
    ],
    "cross_company_takeaways": [(str, "Cross-company synthesis point.", True)],
    "risk_watchlist": [(str, "Portfolio-level or sector-level risk to monitor.", True)],
    "non_investment_advice": (bool, "True when no buy/sell/hold/order guidance is provided.", True),
    "client_summary": (str, "Final concise business-facing summary.", True),
}


def build_stock_research_brief() -> dict[str, Any]:
    install_demo_skills_pack()
    provider = configure_model(temperature=0.0)
    agent = Agently.create_agent("stock-research-demo")

    # Controlled side effect runs in the HOST, before model analysis — not inside
    # the Skill. The Skill is pure guidance and never touches the network.
    market_data = fetch_equity_market_data("Fetch current market quote data for NVDA, AMD, and AVGO.")

    task = (
        "Analyze NVDA, AMD, and AVGO for an internal research memo. Compare recent "
        "performance, valuation context, financial quality, earnings-call themes, "
        "SEC-style risks, and analyst sentiment. Do not give investment advice.\n\n"
        "Base the answer ONLY on the fetched market data below. Copy "
        "market_data.data_boundary EXACTLY into input_boundary and preserve all dates "
        "exactly. If data_status is degraded, explain which quotes used cache or are "
        "unavailable. Do not invent SEC filings, earnings-call facts, analyst ratings, "
        "or valuation metrics not present; when a requested category is unavailable, say "
        "so and put it in watch indicators.\n\n"
        f"market_data (JSON):\n{ json.dumps(market_data, ensure_ascii=False, indent=2) }"
    )

    execution = agent.run_skills_task(
        task,
        skills_packs=[SKILLS_PACK_NAME],
        mode="required",
        semantic_outputs=ANALYSIS_OUTPUTS,
    )
    if execution.status != "success":
        raise RuntimeError(f"Equity research Skill execution failed: { execution.to_dict() }")

    result = cast(dict[str, Any], dict(execution.output or {}))
    result["_provider"] = provider
    result["_market_data"] = market_data
    return result


def main() -> None:
    result = build_stock_research_brief()
    tickers = ",".join(item.get("ticker", "") for item in result.get("company_views", []))
    market_data = result.get("_market_data", {})
    quote_source = market_data.get("source", {}).get("name", "")
    data_status = market_data.get("data_status", "")
    latest_closes = ",".join(
        f"{ item.get('ticker') }={ item.get('quote', {}).get('close') }"
        for item in market_data.get("companies", [])
    )
    quote_dates = ",".join(
        f"{ item.get('ticker') }={ item.get('quote', {}).get('provider_date') }"
        for item in market_data.get("companies", [])
    )

    print(f"provider={ result.get('_provider') }")
    print(f"quote_source={ quote_source }")
    print(f"data_status={ data_status }")
    print(f"latest_closes={ latest_closes }")
    print(f"quote_dates={ quote_dates }")
    print(f"companies={ tickers }")
    print(f"non_investment_advice={ result.get('non_investment_advice') }")
    print(f"risk_items={ len(result.get('risk_watchlist', [])) }")
    print("client_summary=" + str(result.get("client_summary", "")).replace("\n", " "))
    print("\nstructured_result=")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"_provider", "_quote_execution"}},
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
