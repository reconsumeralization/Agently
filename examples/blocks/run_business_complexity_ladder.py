# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Run all Blocks business complexity examples as one usability suite.

Run all cases:
    python examples/blocks/run_business_complexity_ladder.py

Run selected case ids:
    BLOCKS_COMPLEXITY_CASES=03_tool_mcp_sandbox,06_real_complex_bundle \
        python examples/blocks/run_business_complexity_ladder.py

The full suite includes 06_real_complex_bundle, which requires network access,
AMAP_API_KEY, built-in Search, remote AMap MCP, and runtime installation of the
public CocoonAI architecture-diagram Skill.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Callable, cast

from _business_ladder_runtime import BusinessCase, run_business_cases


CASE_FILES = [
    "02_single_tool_support_ticket_stream.py",
    "03_tool_composition_refund_review_stream.py",
    "04_mcp_sandbox_settlement_stream.py",
    "05_single_skill_support_reply_stream.py",
    "06_multi_skills_travel_memo_stream.py",
    "07_real_complex_bundle_stream.py",
]


def load_case_module(filename: str) -> ModuleType:
    path = Path(__file__).resolve().with_name(filename)
    module_name = f"_blocks_business_case_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import business case from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cases() -> list[BusinessCase]:
    cases: list[BusinessCase] = []
    for filename in CASE_FILES:
        module = load_case_module(filename)
        build_case = cast(Callable[[], BusinessCase], getattr(module, "build_case"))
        cases.append(build_case())
    return cases


async def main() -> None:
    await run_business_cases(load_cases(), write_artifact=True)


if __name__ == "__main__":
    asyncio.run(main())
