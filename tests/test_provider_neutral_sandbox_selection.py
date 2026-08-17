"""Core must not recognize optional ExecutionResourceProvider identities."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agently.core.operation.Action.ActionResourceRegistrar import ActionResourceRegistrar


class _Settings:
    def get(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Action:
    def __init__(self) -> None:
        self.settings = _Settings()
        self.registered: dict[str, Any] = {}

    def _normalize_tags(self, _tags: Any) -> list[str]:
        return []

    def _create_executor(self, *_args: Any, **_kwargs: Any) -> object:
        return object()

    def register_action(self, **kwargs: Any) -> None:
        self.registered = kwargs


@pytest.mark.parametrize(
    "provider_id",
    ["gvisor", "runsc", "gvisor/runsc", "seatbelt", "landlock", "bubblewrap"],
)
def test_compatibility_sandbox_rejects_optional_provider_ids(provider_id: str) -> None:
    with pytest.raises(ValueError):
        ActionResourceRegistrar._normalize_code_sandbox(provider_id)


@pytest.mark.parametrize("provider_id", ["gvisor", "seatbelt", "landlock"])
def test_python_compatibility_helper_rejects_optional_provider_ids(provider_id: str) -> None:
    with pytest.raises(ValueError):
        ActionResourceRegistrar(cast(Any, _Action())).register_python_sandbox_action(
            sandbox=cast(Any, provider_id)
        )


@pytest.mark.parametrize("provider_id", ["custom-isolator", "gvisor", "seatbelt", "landlock"])
def test_generic_provider_candidate_descriptor_passes_through_unchanged(provider_id: str) -> None:
    action = _Action()
    candidate = {"provider_id": provider_id, "config": {"profile": "strict"}}

    ActionResourceRegistrar(cast(Any, action)).register_code_runtime_action(
        language="python",
        providers=[candidate],
        isolation="preferred",
    )

    requirement = action.registered["execution_resources"][0]
    assert requirement["provider_candidates"] == [candidate]
    assert requirement["meta"]["isolation_preference"] == "preferred"
