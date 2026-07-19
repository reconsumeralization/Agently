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

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from agently.types.data import ContextBlock, ContextCandidate, ContextReadIntent
from agently.types.data.context import ContextSourceCandidateWindow


@runtime_checkable
class ContextSource(Protocol):
    """Source-native candidate and exact-read port used by ContextReader."""

    source_id: str

    @property
    def source_revision(self) -> str: ...

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        cursor: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ContextSourceCandidateWindow: ...

    async def async_read(
        self,
        candidate: ContextCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
    ) -> ContextBlock: ...


__all__ = ["ContextSource", "ContextSourceCandidateWindow"]
