from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agently.types.data import SkillSourceRequest, SkillSourceSnapshot
from agently.types.plugins import SkillSourceProvider


def test_skill_source_request_and_snapshot_keep_requested_and_resolved_identity() -> None:
    request = SkillSourceRequest(
        source="https://user:secret@example.com/owner/repo.git?token=hidden",
        source_type="git",
        ref="main",
        subpath="skills/agently-triggerflow",
        update=False,
        options={"authorization": "private"},
    )
    snapshot = SkillSourceSnapshot(
        provider_id="git",
        source_type="git",
        requested_source=request.source,
        requested_ref=request.ref,
        resolved_revision="a" * 40,
        subpath=request.subpath,
        materialized_path="/private/cache/snapshot",
        source_digest="sha256:" + "b" * 64,
        metadata={"transport": "https"},
    )

    assert snapshot.requested_ref == "main"
    assert snapshot.resolved_revision == "a" * 40
    assert snapshot.source_digest == "sha256:" + "b" * 64
    assert snapshot.to_dict()["requested_source"] == (
        "https://example.com/owner/repo.git"
    )
    assert "materialized_path" not in snapshot.to_dict()
    assert "secret" not in repr(snapshot.to_dict())
    assert "hidden" not in repr(snapshot.to_dict())
    with pytest.raises(FrozenInstanceError):
        snapshot.provider_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "subpath",
    ["../escape", "/absolute", "skills/../../escape", ".agently/private"],
)
def test_skill_source_request_rejects_unsafe_subpath(subpath: str) -> None:
    with pytest.raises(ValueError, match="subpath"):
        SkillSourceRequest(
            source="https://example.com/owner/repo.git",
            source_type="git",
            subpath=subpath,
        )


def test_git_skill_source_snapshot_requires_exact_commit() -> None:
    with pytest.raises(ValueError, match="40-character|commit"):
        SkillSourceSnapshot(
            provider_id="git",
            source_type="git",
            requested_source="https://example.com/owner/repo.git",
            requested_ref="main",
            resolved_revision="main",
            subpath=None,
            materialized_path="/tmp/snapshot",
            source_digest="sha256:" + "b" * 64,
        )


def test_skill_source_provider_is_a_runtime_checkable_protocol() -> None:
    class Provider:
        name = "ExampleSkillSourceProvider"
        DEFAULT_SETTINGS: dict[str, object] = {}
        provider_id = "example"
        source_types = ("example",)

        @staticmethod
        def _on_register() -> None:
            return None

        @staticmethod
        def _on_unregister() -> None:
            return None

        async def async_materialize(self, request: SkillSourceRequest) -> SkillSourceSnapshot:
            raise NotImplementedError

    assert isinstance(Provider(), SkillSourceProvider)
