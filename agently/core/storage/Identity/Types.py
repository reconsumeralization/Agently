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

from dataclasses import dataclass
from typing import Literal


IdentityKind = Literal[
    "record",
    "locator",
    "content_version",
    "segment",
    "link",
    "carrier",
    "frame",
    "plan",
    "work_result",
    "evidence",
]
IdentityScopeKind = Literal["record_store", "task"]

IDENTITY_PREFIXES: dict[IdentityKind, str] = {
    "record": "rec",
    "locator": "loc",
    "content_version": "cv",
    "segment": "seg",
    "link": "lnk",
    "carrier": "car",
    "frame": "frm",
    "plan": "pln",
    "work_result": "wrk",
    "evidence": "evd",
}


@dataclass(frozen=True, slots=True)
class ScopedIdentity:
    scope_kind: IdentityScopeKind
    scope_id: str
    entity_id: str
    sequence: int

    @property
    def canonical_key(self) -> tuple[IdentityScopeKind, str, str]:
        return (self.scope_kind, self.scope_id, self.entity_id)


@dataclass(frozen=True, slots=True)
class ContentObservation:
    locator_id: str
    content_version_id: str
    digest: str
    size: int
    created: bool


@dataclass(frozen=True, slots=True)
class IdentityRetentionReport:
    roots: tuple[str, ...]
    retained_entity_ids: tuple[str, ...]
    deleted_entity_ids: tuple[str, ...]
    deleted_payloads: tuple[str, ...]
    high_water: str
