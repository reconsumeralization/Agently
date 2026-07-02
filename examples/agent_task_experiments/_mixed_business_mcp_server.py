from __future__ import annotations

import asyncio
import os
from typing import Any

from fastmcp import FastMCP


app = FastMCP("agent-task-experiment-business-systems")


@app.tool
def travel_city_snapshot(city: str) -> dict[str, Any]:
    """Return bounded example travel facts for a city."""

    _ = city
    return {
        "source": "example_mcp.travel_city_snapshot",
        "boundary": "Example local MCP data, not live travel data.",
        "city": "Kyoto",
        "weather_window": "mild mornings, warm afternoons, chance of evening rain",
        "neighborhoods": [
            {"name": "Kyoto Station", "use": "arrival and rail logistics"},
            {"name": "Karasuma Oike", "use": "quiet central hotel base"},
            {"name": "Higashiyama", "use": "client dinner and cultural walk"},
        ],
        "constraints": [
            "keep transfers under 35 minutes when possible",
            "avoid outdoor-only evening plan if rain risk remains high",
        ],
    }


@app.tool
def travel_route_options(origin: str, destination: str) -> dict[str, Any]:
    """Return bounded example route options between two travel points."""

    return {
        "source": "example_mcp.travel_route_options",
        "boundary": "Example local MCP route data, not live navigation.",
        "origin": origin,
        "destination": destination,
        "options": [
            {
                "mode": "rail + taxi",
                "duration_minutes": 32,
                "cost_band": "medium",
                "reliability": "high",
                "notes": "best default for luggage and punctuality",
            },
            {
                "mode": "taxi",
                "duration_minutes": 26,
                "cost_band": "high",
                "reliability": "medium",
                "notes": "sensitive to evening traffic near Higashiyama",
            },
            {
                "mode": "subway + walk",
                "duration_minutes": 38,
                "cost_band": "low",
                "reliability": "medium",
                "notes": "only suitable for light luggage and dry weather",
            },
        ],
    }


@app.tool
def equity_market_snapshot(ticker: str) -> dict[str, Any]:
    """Return bounded example equity facts for a ticker."""

    data = {
        "NVDA": {
            "price": 141.2,
            "one_day_change_pct": 1.8,
            "revenue_signal": "data-center demand remains the primary growth driver",
            "risk_signal": "export controls and supply concentration remain material",
        },
        "AMD": {
            "price": 164.7,
            "one_day_change_pct": -0.6,
            "revenue_signal": "AI accelerator ramp is improving but still execution-sensitive",
            "risk_signal": "gross margin and software ecosystem gaps are investor watchpoints",
        },
        "AVGO": {
            "price": 238.4,
            "one_day_change_pct": 0.9,
            "revenue_signal": "custom silicon and infrastructure software provide diversification",
            "risk_signal": "integration workload and valuation multiple expansion require monitoring",
        },
    }
    normalized = ticker.strip().upper()
    return {
        "source": "example_mcp.equity_market_snapshot",
        "boundary": "Example local MCP market data, not live prices or investment advice.",
        "ticker": normalized,
        **data.get(normalized, {"price": None, "one_day_change_pct": None, "revenue_signal": "", "risk_signal": ""}),
    }


@app.tool
def equity_news_digest(ticker: str) -> dict[str, Any]:
    """Return bounded example news-style factors for a ticker."""

    normalized = ticker.strip().upper()
    return {
        "source": "example_mcp.equity_news_digest",
        "boundary": "Example local MCP digest, not live news.",
        "ticker": normalized,
        "positive_factors": [
            "enterprise AI budget growth continues to support semiconductor demand",
            "cloud infrastructure spending guidance remains constructive",
        ],
        "negative_factors": [
            "regulatory and export-control headlines can reprice the group quickly",
            "capacity, packaging, and customer concentration can create delivery risk",
        ],
    }


@app.tool
def crm_pipeline_snapshot(segment: str) -> dict[str, Any]:
    """Return bounded example CRM pipeline facts for a market segment."""

    _ = segment
    return {
        "source": "example_mcp.crm_pipeline_snapshot",
        "boundary": "Example local MCP CRM data, not a real CRM export.",
        "segment": "mid-market healthcare operations",
        "pipeline": [
            {"stage": "discovery", "accounts": 18, "arr": 420000},
            {"stage": "technical_validation", "accounts": 7, "arr": 390000},
            {"stage": "procurement", "accounts": 3, "arr": 210000},
        ],
        "conversion_notes": [
            "security review is the most common delay",
            "workflow integration proof shortens technical validation",
        ],
    }


@app.tool
def competitor_signal_snapshot(segment: str) -> dict[str, Any]:
    """Return bounded example competitive movement facts."""

    _ = segment
    return {
        "source": "example_mcp.competitor_signal_snapshot",
        "boundary": "Example local MCP market-intelligence data.",
        "segment": "mid-market healthcare operations",
        "signals": [
            {"competitor": "OpsPilot", "move": "bundled compliance templates", "threat": "medium"},
            {"competitor": "FlowDesk", "move": "new EHR integration partnership", "threat": "high"},
            {"competitor": "ManualOps", "move": "discounting annual renewals", "threat": "low"},
        ],
        "open_questions": [
            "whether EHR integration depth is production-ready",
            "whether discounting is broad strategy or end-of-quarter pressure",
        ],
    }


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    try:
        if transport == "http":
            app.run(show_banner=False, transport="http", port=int(os.getenv("MCP_PORT", "8080")))
        else:
            app.run(show_banner=False, transport="stdio")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
