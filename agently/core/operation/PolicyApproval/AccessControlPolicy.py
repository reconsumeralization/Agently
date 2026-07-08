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
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agently.utils import Settings


def _ensure_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def access_policy_auto_allow(policy: Mapping[str, Any] | None = None) -> bool:
    """Return whether host-trusted access policy grants automatic approval."""
    if not isinstance(policy, Mapping):
        return False
    return bool(policy.get("auto_allow", False))


def resolve_access_control_policy(
    settings: "Settings | None" = None,
    *trusted_overlays: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge framework access policy settings with host-trusted overlays.

    The settings object may already inherit global settings from an Agent parent.
    Overlays are reserved for host/framework-owned execution or selector policy,
    never model-generated command payloads.
    """
    policy: dict[str, Any] = {}
    if settings is not None:
        policy.update(_ensure_dict(settings.get("access_control_policy", {})))
    for overlay in trusted_overlays:
        policy.update(_ensure_dict(overlay))
    policy.setdefault("auto_allow", False)
    return policy


def merge_access_control_policy(
    policy: Mapping[str, Any] | None,
    settings: "Settings | None" = None,
    *trusted_overlays: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach effective access-control defaults to an operation policy."""
    merged = _ensure_dict(policy)
    access_policy = resolve_access_control_policy(settings, *trusted_overlays)
    if "auto_allow" in merged:
        merged["auto_allow"] = bool(merged["auto_allow"])
    else:
        merged["auto_allow"] = bool(access_policy.get("auto_allow", False))
    return cast(dict[str, Any], merged)
