# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""FlowBlock Protocol — the contract for reusable execution blocks."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FlowBlock(Protocol):
    """A reusable block that contributes operators to a TriggerFlow blueprint.

    Blocks are strategy-agnostic building blocks. A strategy runner calls
    ``build_operators`` on each block, then wires the returned operator IDs
    together with signals appropriate to the strategy.
    """

    name: str

    def build_operators(
        self,
        *,
        blueprint: Any,
        context: Any,
        settings: dict[str, Any],
    ) -> list[str]:
        """Add operators to *blueprint* and return their IDs.

        The returned IDs are wired together by the strategy runner. A block
        may create one or more operators; each must have a unique stable ID.
        """
        ...
