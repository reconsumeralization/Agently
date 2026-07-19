from __future__ import annotations

from typing import Any

import pytest

from agently import Agently
from agently.core import ExecutionResourceError, ExecutionResourceManager
from agently.types.data import required_code_execution_isolation
from agently.utils import Settings


def _manager() -> ExecutionResourceManager:
    return ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=Settings(name="ProviderSelectionTests", parent=Agently.settings),
        event_center=Agently.event_center,
    )


class _Provider:
    DEFAULT_SETTINGS: dict[str, Any] = {}
    supported_kinds = ("code_execution",)

    def __init__(
        self,
        provider_id: str,
        *,
        available: bool = True,
        languages: tuple[str, ...] = ("python",),
        isolation: Any = None,
        toolchains: dict[str, dict[str, Any]] | None = None,
        availability_sequence: tuple[bool, ...] = (),
    ) -> None:
        self.provider_id = provider_id
        self.name = provider_id
        self.available = available
        self.languages = languages
        self.isolation = isolation or {
            **required_code_execution_isolation(),
            "mechanism": "synthetic-test-provider",
        }
        self.toolchains = dict(toolchains or {})
        self.availability_sequence = list(availability_sequence)
        self.probe_count = 0
        self.ensure_count = 0
        self.release_count = 0
        self.probe_configs: list[dict[str, Any]] = []
        self.ensure_configs: list[dict[str, Any]] = []

    async def async_probe(self, *, requirement, policy):
        _ = requirement, policy
        self.probe_count += 1
        self.probe_configs.append(dict(requirement.get("config", {})))
        available = (
            self.availability_sequence.pop(0)
            if self.availability_sequence
            else self.available
        )
        return {
            "provider_id": self.provider_id,
            "available": available,
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": list(self.languages),
                "isolation": self.isolation,
                "toolchains": self.toolchains,
            },
            "reason": "available" if available else "not installed",
        }

    async def async_ensure(self, *, requirement, policy, existing_handle=None):
        _ = requirement, policy, existing_handle
        self.ensure_count += 1
        self.ensure_configs.append(dict(requirement.get("config", {})))
        return {
            "handle_id": f"{self.provider_id}:{self.ensure_count}",
            "resource": self,
            "status": "ready",
        }

    async def async_health_check(self, handle):
        _ = handle
        return "ready"

    async def async_release(self, handle):
        _ = handle
        self.release_count += 1


@pytest.mark.asyncio
async def test_manager_skips_unavailable_and_ineligible_providers() -> None:
    manager = _manager()
    missing = _Provider("missing", available=False)
    wrong_language = _Provider("wrong-language", languages=("nodejs",))
    eligible = _Provider("eligible")
    manager.register_provider(missing)
    manager.register_provider(wrong_language)
    manager.register_provider(eligible)

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "resource_key": "run",
            "provider_candidates": ["missing", "wrong-language", "eligible"],
            "required_capabilities": {
                "language": "python",
                "isolation": required_code_execution_isolation(),
            },
        }
    )

    assert handle["provider_id"] == "eligible"
    assert [item["provider_id"] for item in handle["meta"]["provider_probes"]] == [
        "missing",
        "wrong-language",
        "eligible",
    ]
    assert missing.ensure_count == wrong_language.ensure_count == 0
    assert eligible.ensure_count == 1


@pytest.mark.asyncio
async def test_code_execution_requirement_rejects_legacy_isolation_labels() -> None:
    manager = _manager()
    manager.register_provider(_Provider("eligible"))

    with pytest.raises(ValueError, match="isolation.*mapping"):
        await manager.async_ensure(
            {
                "kind": "code_execution",
                "resource_key": "run",
                "provider_candidates": ["eligible"],
                "required_capabilities": {
                    "language": "python",
                    "isolation": "required",
                },
            }
        )


@pytest.mark.asyncio
async def test_provider_is_reprobed_before_ensure_and_priority_falls_through() -> None:
    manager = _manager()
    changed = _Provider("changed", availability_sequence=(True, False))
    fallback = _Provider("fallback")
    manager.register_provider(changed)
    manager.register_provider(fallback)

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_candidates": ["changed", "fallback"],
            "required_capabilities": {"language": "python"},
        }
    )

    assert handle["provider_id"] == "fallback"
    assert changed.probe_count == 2
    assert changed.ensure_count == 0


@pytest.mark.asyncio
async def test_explicit_provider_and_release_use_the_selected_provider() -> None:
    manager = _manager()
    first = _Provider("first")
    selected = _Provider("selected")
    manager.register_provider(first)
    manager.register_provider(selected)

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_id": "selected",
            "required_capabilities": {"language": "python"},
        }
    )
    await manager.async_release(handle)

    assert handle["provider_id"] == "selected"
    assert first.ensure_count == 0
    assert selected.ensure_count == 1
    assert selected.release_count == 1


@pytest.mark.asyncio
async def test_all_ineligible_providers_fail_closed_with_probe_facts() -> None:
    manager = _manager()
    manager.register_provider(_Provider("missing", available=False))
    manager.register_provider(_Provider("wrong", languages=("go",)))

    with pytest.raises(ExecutionResourceError) as raised:
        await manager.async_ensure(
            {
                "kind": "code_execution",
                "provider_candidates": ["missing", "wrong"],
                "required_capabilities": {"language": "python"},
            }
        )

    assert raised.value.code == "execution_resource.provider_unavailable"
    assert [item["provider_id"] for item in raised.value.payload["provider_probes"]] == [
        "missing",
        "wrong",
    ]


@pytest.mark.asyncio
async def test_provider_selection_enforces_minimum_and_exact_toolchain_versions() -> None:
    manager = _manager()
    manager.register_provider(
        _Provider(
            "too-old",
            toolchains={"python": {"version": "3.9.19"}},
        )
    )
    manager.register_provider(
        _Provider(
            "eligible",
            toolchains={"python": {"version": "3.10.13"}},
        )
    )

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_candidates": ["too-old", "eligible"],
            "required_capabilities": {
                "language": "python",
                "toolchains": {"python": {"minimum_version": "3.10"}},
            },
        }
    )

    assert handle["provider_id"] == "eligible"

    with pytest.raises(ExecutionResourceError):
        await manager.async_ensure(
            {
                "kind": "code_execution",
                "provider_candidates": ["eligible"],
                "required_capabilities": {
                    "language": "python",
                    "toolchains": {"python": {"exact_version": "3.10.12"}},
                },
            }
        )


def test_duplicate_provider_id_is_rejected() -> None:
    manager = _manager()
    manager.register_provider(_Provider("duplicate"))

    with pytest.raises(ValueError, match="duplicate"):
        manager.register_provider(_Provider("duplicate"))


@pytest.mark.asyncio
async def test_candidate_descriptor_merges_only_selected_provider_config() -> None:
    manager = _manager()
    first = _Provider("runtime", availability_sequence=(False,))
    selected = _Provider("host-policy")
    manager.register_provider(first)
    manager.register_provider(selected)

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "config": {"common": "base"},
            "provider_candidates": [
                {"provider_id": "runtime", "config": {"runtime": "alternative"}},
                {"provider_id": "host-policy", "config": {"profile": "strict"}},
            ],
            "required_capabilities": {"language": "python"},
        }
    )

    assert handle["provider_id"] == "host-policy"
    assert first.probe_configs == [{"common": "base", "runtime": "alternative"}]
    assert selected.probe_configs == [
        {"common": "base", "profile": "strict"},
        {"common": "base", "profile": "strict"},
    ]
    assert selected.ensure_configs == [{"common": "base", "profile": "strict"}]
    assert handle["meta"]["selected_provider_candidate"] == {
        "index": 1,
        "provider_id": "host-policy",
    }


@pytest.mark.asyncio
async def test_preferred_capabilities_choose_isolated_provider_before_unsafe_priority() -> None:
    manager = _manager()
    unsafe = _Provider(
        "unsafe",
        isolation={
            "process_contained": False,
            "host_filesystem_restricted": False,
            "privilege_escalation_blocked": False,
            "syscalls_restricted": False,
        },
    )
    isolated = _Provider(
        "isolated",
        isolation={
            "process_contained": True,
            "host_filesystem_restricted": True,
            "privilege_escalation_blocked": True,
            "syscalls_restricted": True,
        },
    )
    manager.register_provider(unsafe)
    manager.register_provider(isolated)

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_candidates": ["unsafe", "isolated"],
            "required_capabilities": {"language": "python"},
            "preferred_capabilities": {
                "isolation": {
                    "process_contained": True,
                    "host_filesystem_restricted": True,
                    "privilege_escalation_blocked": True,
                    "syscalls_restricted": True,
                }
            },
        }
    )

    assert handle["provider_id"] == "isolated"
    assert handle["meta"]["selected_provider_candidate"][
        "preferred_capabilities_satisfied"
    ] is True


@pytest.mark.asyncio
async def test_preferred_capabilities_record_explicit_fallback_when_unavailable() -> None:
    manager = _manager()
    unsafe = _Provider(
        "unsafe",
        isolation={
            "process_contained": False,
            "host_filesystem_restricted": False,
            "privilege_escalation_blocked": False,
            "syscalls_restricted": False,
        },
    )
    unavailable = _Provider(
        "isolated",
        available=False,
        isolation={
            "process_contained": True,
            "host_filesystem_restricted": True,
            "privilege_escalation_blocked": True,
            "syscalls_restricted": True,
        },
    )
    manager.register_provider(unsafe)
    manager.register_provider(unavailable)

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_candidates": ["unsafe", "isolated"],
            "required_capabilities": {"language": "python"},
            "preferred_capabilities": {
                "isolation": {"process_contained": True}
            },
        }
    )

    assert handle["provider_id"] == "unsafe"
    assert handle["meta"]["selected_provider_candidate"][
        "preferred_capabilities_satisfied"
    ] is False
    assert handle["meta"]["selected_provider_candidate"][
        "preference_fallback"
    ] is True
