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

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agently.types.data import (
    ContextBlock,
    ContextBudget,
    ContextCandidate,
    ContextConsumer,
    ContextDiagnostic,
    ContextOmission,
    ContextPackage,
    ContextReadIntent,
    ContextSourceDescriptor,
    ContextSourceRead,
    TaskContextEntrySnapshot,
    TaskContextSnapshot,
)
from agently.types.plugins.ContextSource import ContextSourceScopedRead

from .Selection import ContextSelection, ContextSemanticSelector
from ._Index import (
    _InvalidContextIndexQueryError,
    _RequiredVectorUnavailableError,
    _UnknownContextSourceKindError,
)


_TEXT_FILE_EXTENSIONS = {
    "",
    ".c",
    ".cc",
    ".cjs",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".cts",
    ".csv",
    ".cxx",
    ".env",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".html",
    ".hxx",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".mjs",
    ".mts",
    ".py",
    ".pyi",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_OFFICE_FILE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
_IMAGE_FILE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_BINARY_FILE_EXTENSIONS = {
    ".7z",
    ".bin",
    ".db",
    ".dll",
    ".doc",
    ".dylib",
    ".exe",
    ".gz",
    ".jar",
    ".mp3",
    ".mp4",
    ".parquet",
    ".ppt",
    ".rar",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".wav",
    ".xls",
    ".zip",
}
_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/json5",
    "application/javascript",
    "application/toml",
    "application/x-yaml",
    "application/xml",
}
_BINARY_MEDIA_TYPES = {
    "application/gzip",
    "application/octet-stream",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/zip",
}
_SAFE_NON_TEXT_METADATA_KEYS = {
    "bytes",
    "content_kind",
    "content_type",
    "context_representation",
    "filename",
    "media_type",
    "mime_type",
    "path",
    "sha256",
    "size_bytes",
    "total_bytes",
}


class ContextStaleError(RuntimeError):
    """Raised when a reader's pinned TaskContext/source snapshot is no longer current."""


@dataclass(frozen=True)
class _CollectedCandidate:
    offered: ContextCandidate
    source_descriptor: ContextSourceDescriptor | None
    direct_entry: TaskContextEntrySnapshot | None


@dataclass(frozen=True)
class _ContinuationState:
    offset: int
    exhaustive: bool
    scope: Mapping[str, Any]


def _canonical_intent_value(value: Any) -> Any:
    if value is None:
        return {"type": "none", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": repr(value)}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, Mapping):
        return {
            "type": "mapping",
            "value": [
                [str(key), _canonical_intent_value(item)]
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            ],
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return {
            "type": "sequence",
            "value": [_canonical_intent_value(item) for item in value],
        }
    value_type = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
    return {"type": value_type, "value": repr(value)}


def _intent_fingerprint(intent: ContextReadIntent) -> str:
    canonical = _canonical_intent_value(
        {
            "query": intent.query,
            "explicit_refs": intent.explicit_refs,
            "roles": intent.roles,
            "filters": intent.filters,
        }
    )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    return len(str(content))


class ContextReader:
    """Consumer-bound progressive-disclosure session over one TaskContext snapshot."""

    def __init__(
        self,
        task_context: Any,
        *,
        consumer: ContextConsumer,
        phase: str,
        budget: ContextBudget,
        semantic_selector: ContextSemanticSelector | None = None,
        _owner_token: object | None = None,
    ) -> None:
        if not bool(getattr(task_context, "_owns_reader_token", lambda _token: False)(_owner_token)):
            raise TypeError(
                "ContextReader instances must be created by TaskContext.reader(...) "
                "or TaskContext.restore_reader(...)."
            )
        self.task_context = task_context
        self.consumer = consumer
        self.phase = str(phase)
        self.budget = budget
        self.semantic_selector = semantic_selector
        self._snapshot: TaskContextSnapshot = task_context.snapshot()
        self._disclosed: set[tuple[str, str, str, str, str]] = set()
        self._packages: list[ContextPackage] = []
        self._continuations: dict[
            tuple[str, str, str], _ContinuationState
        ] = {}

    @property
    def snapshot(self) -> TaskContextSnapshot:
        return self._snapshot

    @property
    def packages(self) -> tuple[ContextPackage, ...]:
        return tuple(self._packages)

    @property
    def is_current(self) -> bool:
        return self.task_context.is_snapshot_current(self._snapshot)

    def refresh(self) -> None:
        """Explicitly rebase this consumer session onto the current source snapshot.

        Disclosure identities include source revisions, so unchanged content stays
        disclosed while a changed source revision becomes eligible for a new read.
        Prior packages remain available as the audit history for this reader.
        """

        previous = self._snapshot
        current = self.task_context.snapshot()
        current_revisions = dict(current.source_revisions)
        previous_revisions = dict(previous.source_revisions)
        self._continuations = {
            key: continuation
            for key, continuation in self._continuations.items()
            if previous_revisions.get(key[0]) == key[1]
            and current_revisions.get(key[0]) == key[1]
        }
        self._snapshot = current

    def ensure_required_delivery(self, package: ContextPackage) -> ContextPackage:
        """Fail closed when a required binding or block was not delivered."""

        if (
            package.task_context_id != self._snapshot.context_id
            or package.consumer_id != self.consumer.consumer_id
            or package.phase != self.phase
        ):
            raise ValueError("ContextPackage does not belong to this ContextReader.")
        required_omissions = [item for item in package.omissions if item.required]
        failed_binding_ids = {
            str(item.details.get("binding_id") or "")
            for item in package.diagnostics
            if item.code == "context.source_candidates_failed"
        }
        required_binding_ids = {
            binding.binding_id
            for binding in self._snapshot.bindings
            if binding.required
        }
        if required_omissions or failed_binding_ids.intersection(
            required_binding_ids
        ):
            raise RuntimeError(
                "Required TaskContext content could not be delivered completely to "
                f"consumer {self.consumer.consumer_id!r}."
            )
        return package

    def _export_state(self) -> dict[str, Any]:
        """Serialize consumer-local progressive disclosure state for durable resume."""

        return {
            "task_context_id": self._snapshot.context_id,
            "consumer": {
                "consumer_id": self.consumer.consumer_id,
                "model": self.consumer.model,
                "capabilities": dict(self.consumer.capabilities),
            },
            "phase": self.phase,
            "budget": {
                "max_chars": self.budget.max_chars,
                "max_blocks": self.budget.max_blocks,
                "max_block_chars": self.budget.max_block_chars,
            },
            "disclosed": [list(identity) for identity in sorted(self._disclosed)],
            "continuations": [
                {
                    "binding_id": binding_id,
                    "source_revision": source_revision,
                    "intent_fingerprint": intent_fingerprint,
                    "offset": continuation.offset,
                    "exhaustive": continuation.exhaustive,
                    "scope": dict(continuation.scope),
                }
                for (
                    binding_id,
                    source_revision,
                    intent_fingerprint,
                ), continuation in sorted(self._continuations.items())
            ],
        }

    def _restore_state(
        self,
        state: Mapping[str, Any],
        *,
        packages: Sequence[ContextPackage] = (),
        _owner_token: object | None = None,
    ) -> None:
        """Restore only state owned by this exact consumer/phase reader."""

        if not self.task_context._owns_reader_token(_owner_token):
            raise TypeError(
                "ContextReader state must be restored by TaskContext.restore_reader(...)."
            )

        if str(state.get("task_context_id") or "") != self._snapshot.context_id:
            raise ValueError("ContextReader state belongs to a different TaskContext.")
        consumer = state.get("consumer")
        consumer_id = (
            str(consumer.get("consumer_id") or "")
            if isinstance(consumer, Mapping)
            else ""
        )
        if consumer_id != self.consumer.consumer_id or str(state.get("phase") or "") != self.phase:
            raise ValueError("ContextReader state belongs to a different consumer or phase.")
        disclosed: set[tuple[str, str, str, str, str]] = set()
        raw_disclosed = state.get("disclosed")
        if isinstance(raw_disclosed, Sequence) and not isinstance(
            raw_disclosed,
            str | bytes | bytearray,
        ):
            for raw_identity in raw_disclosed:
                if not isinstance(raw_identity, Sequence) or isinstance(
                    raw_identity,
                    str | bytes | bytearray,
                ):
                    raise ValueError("ContextReader disclosed identities must be sequences.")
                identity = tuple(str(item) for item in raw_identity)
                if len(identity) != 5 or any(not item for item in identity):
                    raise ValueError("ContextReader disclosed identities require five non-empty fields.")
                disclosed.add(
                    (identity[0], identity[1], identity[2], identity[3], identity[4])
                )
        self._disclosed = disclosed
        continuations: dict[tuple[str, str, str], _ContinuationState] = {}
        raw_continuations = state.get("continuations")
        if raw_continuations is not None:
            if not isinstance(raw_continuations, Sequence) or isinstance(
                raw_continuations,
                str | bytes | bytearray,
            ):
                raise ValueError("ContextReader continuations must be a sequence.")
            for raw_continuation in raw_continuations:
                if not isinstance(raw_continuation, Mapping):
                    raise ValueError("ContextReader continuation entries must be mappings.")
                binding_id = str(raw_continuation.get("binding_id") or "").strip()
                source_revision = str(
                    raw_continuation.get("source_revision") or ""
                ).strip()
                intent_fingerprint = str(
                    raw_continuation.get("intent_fingerprint") or ""
                ).strip()
                if not binding_id or not source_revision or len(intent_fingerprint) != 64:
                    raise ValueError("ContextReader continuation identity is invalid.")
                offset = raw_continuation.get("offset")
                if (
                    not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or offset < 0
                ):
                    raise ValueError("ContextReader continuation offset is invalid.")
                exhaustive = raw_continuation.get("exhaustive")
                if not isinstance(exhaustive, bool):
                    raise ValueError("ContextReader continuation exhaustive must be boolean.")
                scope = raw_continuation.get("scope")
                if not isinstance(scope, Mapping):
                    raise ValueError("ContextReader continuation scope must be a mapping.")
                key = (binding_id, source_revision, intent_fingerprint)
                if key in continuations:
                    raise ValueError("ContextReader continuation identities cannot repeat.")
                continuations[key] = _ContinuationState(
                    offset=offset,
                    exhaustive=exhaustive,
                    scope=dict(scope),
                )
        self._continuations = continuations
        self._packages = [
            package
            for package in packages
            if package.consumer_id == self.consumer.consumer_id
            and package.phase == self.phase
        ]

    def _assert_current(self) -> None:
        if self.is_current:
            return
        current = self.task_context.snapshot()
        if current.revision != self._snapshot.revision:
            raise ContextStaleError(
                "TaskContext revision changed after this ContextReader was created."
            )
        raise ContextStaleError(
            "A bound Context source revision changed after this ContextReader was created."
        )

    @staticmethod
    def _descriptor_content_kind(
        metadata: Mapping[str, Any],
        source_ref: str = "",
    ) -> str:
        def classify_type_hint(value: Any) -> str:
            normalized = str(value or "").split(";", 1)[0].strip().lower()
            if not normalized:
                return ""
            if normalized in {
                "text",
                "pdf",
                "office",
                "image",
                "binary",
                "unknown",
            }:
                return normalized
            if normalized.startswith("image/"):
                return "image"
            if normalized == "application/pdf":
                return "pdf"
            if normalized.startswith(
                "application/vnd.openxmlformats-officedocument"
            ):
                return "office"
            if normalized.startswith("text/") or normalized in _TEXT_MEDIA_TYPES:
                return "text"
            if (
                normalized in _BINARY_MEDIA_TYPES
                or normalized.startswith("audio/")
                or normalized.startswith("video/")
            ):
                return "binary"
            return "unknown"

        declared_kind = classify_type_hint(metadata.get("content_kind"))
        media_kind = classify_type_hint(
            metadata.get("media_type")
            or metadata.get("mime_type")
            or metadata.get("content_type")
            or ""
        )

        locator = str(
            source_ref
            or metadata.get("path")
            or metadata.get("filename")
            or ""
        ).split("?", 1)[0].split("#", 1)[0].lower()
        filename = locator.rsplit("/", 1)[-1]
        suffix = f".{filename.rsplit('.', 1)[-1]}" if "." in filename else ""
        if suffix == ".pdf":
            suffix_kind = "pdf"
        elif suffix in _OFFICE_FILE_EXTENSIONS:
            suffix_kind = "office"
        elif suffix in _IMAGE_FILE_EXTENSIONS:
            suffix_kind = "image"
        elif suffix in _BINARY_FILE_EXTENSIONS:
            suffix_kind = "binary"
        elif suffix in _TEXT_FILE_EXTENSIONS:
            suffix_kind = "text" if suffix else ""
        elif suffix:
            suffix_kind = "unknown"
        else:
            suffix_kind = ""

        concrete_non_text = {
            kind
            for kind in (declared_kind, media_kind, suffix_kind)
            if kind in {"pdf", "office", "image", "binary"}
        }
        if len(concrete_non_text) > 1:
            return "unknown"
        if concrete_non_text:
            return next(iter(concrete_non_text))
        if declared_kind == "unknown":
            return "unknown"
        if declared_kind == "text":
            return "text"
        if "text" in {media_kind, suffix_kind}:
            return "text"
        if "unknown" in {media_kind, suffix_kind}:
            return "unknown"
        return ""

    def _supports_image_attachments(self) -> bool:
        capabilities = self.consumer.capabilities
        attachments = capabilities.get("attachments")
        if isinstance(attachments, Mapping) and attachments.get("image") is True:
            return True
        return capabilities.get("image_attachments") is True

    @staticmethod
    def _valid_image_attachment_content(content: Any) -> bool:
        if not isinstance(content, Sequence) or isinstance(
            content,
            str | bytes | bytearray,
        ):
            return False
        if not content:
            return False
        for item in content:
            if not isinstance(item, Mapping) or str(item.get("type") or "") != "image_url":
                return False
            image_url = item.get("image_url")
            if not isinstance(image_url, Mapping):
                return False
            url = str(image_url.get("url") or "").strip()
            if url.startswith(("https://", "http://")):
                continue
            if not url.startswith("data:image/") or ";base64," not in url:
                return False
            payload = url.split(";base64,", 1)[1].strip()
            if not payload:
                return False
            try:
                decoded = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                return False
            if not decoded:
                return False
        return True

    def _candidate_representation(self, candidate: ContextCandidate) -> str:
        declared = str(
            candidate.metadata.get("context_representation") or ""
        ).strip().lower()
        if declared == "metadata_only":
            return "metadata_only"
        content_kind = self._descriptor_content_kind(
            candidate.metadata,
            candidate.source_ref,
        )
        if content_kind in {"pdf", "office"}:
            return "parsed_text" if declared == "parsed_text" else "metadata_only"
        if content_kind == "image":
            return (
                "image_attachment"
                if self._supports_image_attachments()
                else "metadata_only"
            )
        if content_kind in {"binary", "unknown"}:
            return "metadata_only"
        return "text"

    @classmethod
    def _safe_non_text_metadata(
        cls,
        metadata: Mapping[str, Any],
        source_ref: str,
        *,
        representation: str,
    ) -> dict[str, Any]:
        """Keep only host-verifiable file facts for opaque media."""

        safe = {
            str(key): value
            for key, value in metadata.items()
            if str(key) in _SAFE_NON_TEXT_METADATA_KEYS
        }
        content_kind = cls._descriptor_content_kind(metadata, source_ref)
        if content_kind:
            safe["content_kind"] = content_kind
        safe["context_representation"] = representation
        return safe

    @classmethod
    def _safe_descriptor_projection(
        cls,
        descriptor: ContextSourceDescriptor,
    ) -> tuple[str, int, dict[str, Any]]:
        metadata = dict(descriptor.metadata)
        content_kind = cls._descriptor_content_kind(
            metadata,
            descriptor.source_ref,
        )
        declared = str(
            metadata.get("context_representation") or ""
        ).strip().lower()
        document_without_parsed_text = (
            content_kind in {"pdf", "office"} and declared != "parsed_text"
        )
        if (
            declared == "metadata_only"
            or document_without_parsed_text
            or content_kind in {"image", "binary", "unknown"}
        ):
            # A descriptor is an index projection, not authority to interpret
            # arbitrary bytes. For non-text media, only the canonical ref/name
            # may participate in semantic selection.
            representation = (
                "image_attachment_or_metadata"
                if content_kind == "image"
                else "metadata_only"
            )
            metadata = cls._safe_non_text_metadata(
                metadata,
                descriptor.source_ref,
                representation=representation,
            )
            return (
                descriptor.source_ref,
                len(descriptor.source_ref),
                metadata,
            )
        return descriptor.summary, descriptor.estimated_chars, metadata

    @staticmethod
    def _coerce_intent(intent: str | ContextReadIntent) -> ContextReadIntent:
        if isinstance(intent, ContextReadIntent):
            return intent
        return ContextReadIntent(query=str(intent))

    async def _collect(
        self,
        intent: ContextReadIntent,
    ) -> tuple[
        list[_CollectedCandidate],
        list[ContextDiagnostic],
        dict[str, dict[str, Any]],
        dict[tuple[str, str, str], _ContinuationState],
    ]:
        collected: list[_CollectedCandidate] = []
        diagnostics: list[ContextDiagnostic] = []
        source_coverage: dict[str, dict[str, Any]] = {}
        pending_continuations: dict[
            tuple[str, str, str], _ContinuationState
        ] = {}
        sequence = 0

        for entry in self.task_context._entry_snapshots():
            if (
                bool(intent.metadata.get("exclude_already_in_prompt"))
                and bool(entry.metadata.get("already_in_prompt"))
            ):
                continue
            if intent.roles and entry.role not in intent.roles:
                continue
            sequence += 1
            source_ref = entry.source_ref or entry.entry_id
            entry_metadata = dict(entry.metadata)
            entry_content_kind = self._descriptor_content_kind(
                entry_metadata,
                source_ref,
            )
            entry_representation = str(
                entry_metadata.get("context_representation") or ""
            ).strip().lower()
            entry_summary = str(entry_metadata.get("summary") or source_ref)
            entry_estimated_chars = _content_chars(entry.content)
            if entry_content_kind in {"image", "binary", "unknown"} or (
                entry_content_kind in {"pdf", "office"}
                and entry_representation != "parsed_text"
            ):
                entry_summary = source_ref
                entry_estimated_chars = len(source_ref)
                representation = (
                    "image_attachment_or_metadata"
                    if entry_content_kind == "image"
                    else "metadata_only"
                )
                entry_metadata = self._safe_non_text_metadata(
                    entry_metadata,
                    source_ref,
                    representation=representation,
                )
            offered = ContextCandidate(
                block_key=f"context-block:{sequence}",
                source_id=f"task-context:{self._snapshot.context_id}",
                source_revision=f"context-revision:{self._snapshot.revision}",
                source_ref=source_ref,
                binding_id=entry.entry_id,
                role=entry.role,
                summary=entry_summary,
                estimated_chars=entry_estimated_chars,
                required=entry.required,
                priority=entry.priority,
                metadata=entry_metadata,
            )
            collected.append(
                _CollectedCandidate(
                    offered=offered,
                    source_descriptor=None,
                    direct_entry=entry,
                )
            )

        source_limit = max(self.budget.max_blocks * 4, self.budget.max_blocks)
        requested_candidate_limit = intent.metadata.get("candidate_limit")
        if requested_candidate_limit is not None:
            if (
                not isinstance(requested_candidate_limit, int)
                or isinstance(requested_candidate_limit, bool)
                or requested_candidate_limit <= 0
            ):
                raise ValueError("Context candidate_limit must be a positive integer.")
            source_limit = min(source_limit, requested_candidate_limit)
        intent_fingerprint = _intent_fingerprint(intent)
        offsets: dict[str, int] = {}
        for binding in self._snapshot.bindings:
            continuation = self._continuations.get(
                (
                    binding.binding_id,
                    binding.source_revision,
                    intent_fingerprint,
                )
            )
            offsets[binding.binding_id] = (
                continuation.offset if continuation is not None else 0
            )
        try:
            result = await self.task_context._query_index(
                self._snapshot,
                intent,
                offsets=offsets,
                limit=source_limit,
            )
        except (
            _InvalidContextIndexQueryError,
            _RequiredVectorUnavailableError,
            _UnknownContextSourceKindError,
        ):
            raise
        except Exception as error:
            for binding in self._snapshot.bindings:
                diagnostics.append(
                    ContextDiagnostic(
                        code="context.source_candidates_failed",
                        message="A Context source could not build or query its index.",
                        details={
                            "binding_id": binding.binding_id,
                            "source_id": binding.source_id,
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                        },
                    )
                )
            return collected, diagnostics, source_coverage, pending_continuations
        source_coverage.update(
            {
                str(binding_id): dict(coverage)
                for binding_id, coverage in result.source_coverage.items()
            }
        )
        for binding_id, failure in result.source_failures.items():
            diagnostics.append(
                ContextDiagnostic(
                    code="context.source_candidates_failed",
                    message="A Context source could not build or query its index.",
                    details={"binding_id": binding_id, **dict(failure)},
                )
            )
        diagnostics.append(
            ContextDiagnostic(
                code="context.index_query",
                message="TaskContext internal index query facts.",
                details=result.diagnostics,
            )
        )
        for binding in self._snapshot.bindings:
            coverage = result.source_coverage.get(binding.binding_id)
            if coverage is None:
                continue
            continuation_key = (
                binding.binding_id,
                binding.source_revision,
                intent_fingerprint,
            )
            pending_continuations[continuation_key] = _ContinuationState(
                offset=int(result.next_offsets[binding.binding_id]),
                exhaustive=bool(coverage["exhaustive"]),
                scope=dict(coverage["scope"]),
            )
        for match in result.matches:
            binding = match.binding
            descriptor = match.descriptor
            sequence += 1
            summary, estimated_chars, descriptor_metadata = (
                self._safe_descriptor_projection(descriptor)
            )
            offered = ContextCandidate(
                block_key=f"context-block:{sequence}",
                source_id=binding.source_id,
                source_revision=binding.source_revision,
                source_ref=descriptor.source_ref,
                binding_id=binding.binding_id,
                role=descriptor.role,
                summary=summary,
                estimated_chars=estimated_chars,
                required=descriptor.required,
                priority=max(binding.priority, descriptor.priority),
                metadata=descriptor_metadata,
            )
            collected.append(
                _CollectedCandidate(
                    offered=offered,
                    source_descriptor=descriptor,
                    direct_entry=None,
                )
            )
        return collected, diagnostics, source_coverage, pending_continuations

    def _disclosure_identity(
        self,
        candidate: ContextCandidate,
        intent: ContextReadIntent,
    ) -> tuple[str, str, str, str, str]:
        scope = "canonical_source"
        try:
            source = self.task_context._binding_source(candidate.binding_id)
        except KeyError:
            # Direct TaskContext entries use their entry id as the disclosure
            # binding identity and have no attached ContextSource mechanism.
            source = None
        if intent.query.strip() and isinstance(source, ContextSourceScopedRead):
            # A source-local scoped read may disclose multiple bounded ranges of
            # the same canonical ref.  The ref remains the trusted identity;
            # the intent fingerprint only scopes this reader's dedupe history.
            scope = f"scoped_intent:{_intent_fingerprint(intent)}"
        return (
            candidate.binding_id,
            candidate.source_revision,
            candidate.source_ref,
            candidate.role,
            scope,
        )

    @staticmethod
    def _single_candidate_is_exactly_scoped(
        intent: ContextReadIntent,
        candidate: ContextCandidate,
    ) -> bool:
        raw_path = intent.filters.get("path")
        if not isinstance(raw_path, str):
            return False
        path = raw_path.strip()
        if not path or any(character in path for character in "*?["):
            return False
        candidate_path = str(candidate.metadata.get("path") or candidate.source_ref).strip()
        return path == candidate_path or path == candidate.source_ref

    async def _select_optional(
        self,
        intent: ContextReadIntent,
        candidates: list[_CollectedCandidate],
        *,
        available_chars: int,
        available_blocks: int,
    ) -> tuple[tuple[str, ...], list[ContextDiagnostic], str | None]:
        if not candidates:
            return (), [], None
        if str(intent.metadata.get("optional_selection") or "").strip() == "none":
            return (), [], "explicitly_skipped"
        if available_chars <= 0 or available_blocks <= 0:
            return (), [], "selection_budget_exhausted"
        # One bounded candidate has no inter-candidate choice to rank.  When a
        # semantic selector is configured, an unscoped candidate still goes
        # through it; without one, reading the only candidate merely discloses
        # evidence and does not declare semantic usefulness or acceptance.
        # An exact structural path has no semantic choice even when a selector
        # is configured.
        if len(candidates) == 1:
            exactly_scoped = self._single_candidate_is_exactly_scoped(
                intent,
                candidates[0].offered,
            )
            if self.semantic_selector is None or exactly_scoped:
                return (candidates[0].offered.block_key,), [], None
        if self.semantic_selector is None:
            return (
                (),
                [
                    ContextDiagnostic(
                        code="context.semantic_selector_unavailable",
                        message=(
                            "Optional prose relevance required semantic selection, "
                            "but no selector was available."
                        ),
                        details={"candidate_count": len(candidates)},
                    )
                ],
                "semantic_selector_unavailable",
            )
        offered = tuple(item.offered for item in candidates)
        selection_intent = ContextReadIntent(
            query=intent.query,
            explicit_refs=intent.explicit_refs,
            roles=intent.roles,
            filters=intent.filters,
            metadata={
                **dict(intent.metadata),
                "selection_budget": {
                    "available_chars": available_chars,
                    "available_blocks": available_blocks,
                    "max_block_chars": self.budget.max_block_chars,
                },
            },
        )
        try:
            result = await self.semantic_selector.async_select(
                intent=selection_intent,
                candidates=offered,
                consumer=self.consumer,
                phase=self.phase,
            )
        except Exception as error:
            return (
                (),
                [
                    ContextDiagnostic(
                        code="context.selection_failed",
                        message="Context semantic selection failed closed.",
                        details={
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                            "candidate_count": len(candidates),
                            "offered_refs": [
                                item.offered.source_ref for item in candidates[:16]
                            ],
                        },
                    )
                ],
                "selection_failed",
            )
        if not isinstance(result, ContextSelection):
            return (
                (),
                [
                    ContextDiagnostic(
                        code="context.selection_invalid",
                        message="Context selector returned an invalid result type.",
                        details={"result_type": result.__class__.__name__},
                    )
                ],
                "selection_invalid",
            )
        keys = tuple(result.selected_keys)
        offered_keys = {item.offered.block_key for item in candidates}
        unknown = sorted(set(keys) - offered_keys)
        duplicate = len(keys) != len(set(keys))
        if unknown or duplicate:
            return (
                (),
                [
                    ContextDiagnostic(
                        code="context.selection_invalid",
                        message="Context selector returned unknown or duplicate offered keys.",
                        details={"unknown_keys": unknown, "duplicate_keys": duplicate},
                    )
                ],
                "selection_invalid",
            )
        return keys, [], None

    async def _read_block(
        self,
        item: _CollectedCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
        query: str = "",
    ) -> ContextBlock:
        candidate = item.offered
        if item.direct_entry is not None:
            entry = item.direct_entry
            content = entry.content
            metadata = dict(candidate.metadata)
            if representation == "image_attachment":
                if not self._valid_image_attachment_content(content):
                    raise ValueError(
                        "Direct TaskContext image content is not a non-empty image attachment payload."
                    )
                metadata = self._safe_non_text_metadata(
                    metadata,
                    candidate.source_ref,
                    representation="image_attachment",
                )
                content_chars = 0
            elif representation == "parsed_text":
                if not isinstance(content, str):
                    raise ValueError(
                        "Direct TaskContext parsed document content must be textual."
                    )
                content_chars = len(content)
            else:
                if isinstance(content, bytes | bytearray):
                    raise ValueError(
                        "Direct TaskContext content returned unparsed binary bytes for textual disclosure."
                    )
                content_chars = _content_chars(content)
            return ContextBlock(
                block_id=f"context_block:{uuid.uuid4().hex}",
                block_key=candidate.block_key,
                source_id=candidate.source_id,
                source_revision=candidate.source_revision,
                source_ref=candidate.source_ref,
                binding_id=candidate.binding_id,
                role=candidate.role,
                content=content,
                completeness="complete",
                content_chars=content_chars,
                required=candidate.required,
                refs=(candidate.source_ref,),
                metadata=metadata,
            )
        if item.source_descriptor is None:
            raise RuntimeError("Collected Context candidate has no readable source.")
        source = self.task_context._binding_source(candidate.binding_id)
        if query and isinstance(source, ContextSourceScopedRead):
            raw = await source.async_read_scoped(
                candidate.source_ref,
                query=query,
                max_chars=max_chars,
                representation=representation,
                range_start=0,
            )
        else:
            raw = await source.async_read_exact(
                candidate.source_ref,
                max_chars=max_chars,
                representation=representation,
                range_start=0,
            )
        if not isinstance(raw, ContextSourceRead):
            raise TypeError("ContextSource.async_read_exact must return ContextSourceRead.")
        if (
            raw.source_id != candidate.source_id
            or raw.source_revision != candidate.source_revision
            or raw.source_ref != candidate.source_ref
        ):
            raise ValueError("Context exact read identity changed.")
        if representation != "image_attachment" and isinstance(
            raw.content,
            bytes | bytearray,
        ):
            raise ValueError(
                "Context exact read returned unparsed binary bytes for a textual disclosure."
            )
        if representation == "image_attachment" and not self._valid_image_attachment_content(
            raw.content
        ):
            raise ValueError(
                "Context image exact read did not return a non-empty image attachment payload."
            )
        if representation == "parsed_text":
            raw_representation = str(
                raw.metadata.get("context_representation") or ""
            ).strip().lower()
            if raw_representation != "parsed_text" or not isinstance(
                raw.content,
                str,
            ):
                raise ValueError(
                    "Context document exact read must preserve parsed_text "
                    "provenance and return textual content."
                )
        self._assert_current()
        content_chars = (
            0
            if representation == "image_attachment"
            else _content_chars(raw.content)
        )
        metadata = raw.metadata
        if representation == "image_attachment":
            metadata = self._safe_non_text_metadata(
                {**dict(candidate.metadata), **dict(raw.metadata)},
                candidate.source_ref,
                representation="image_attachment",
            )
        return ContextBlock(
            block_id=f"context_block:{uuid.uuid4().hex}",
            block_key=candidate.block_key,
            source_id=candidate.source_id,
            source_revision=candidate.source_revision,
            source_ref=candidate.source_ref,
            binding_id=candidate.binding_id,
            role=candidate.role,
            content=raw.content,
            completeness=raw.completeness,
            content_chars=content_chars,
            required=candidate.required,
            refs=raw.refs or (candidate.source_ref,),
            metadata=metadata,
        )

    async def _materialize_candidates(
        self,
        items: Sequence[_CollectedCandidate],
        *,
        intent: ContextReadIntent,
        blocks: list[ContextBlock],
        omissions: list[ContextOmission],
        diagnostics: list[ContextDiagnostic],
        remaining_chars: int,
    ) -> int:
        required_overflow = str(
            intent.metadata.get("required_overflow") or "fail"
        ).strip()
        allow_lossy_required = required_overflow == "lossy_digest"
        delivery_mode = str(intent.metadata.get("delivery_mode") or "content").strip()
        if delivery_mode not in {"content", "refs_only"}:
            raise ValueError("Context delivery_mode must be content or refs_only.")
        required_remaining = sum(1 for item in items if item.offered.required)

        for item in items:
            candidate = item.offered
            required_divisor = max(1, required_remaining)
            if candidate.required:
                required_remaining -= 1
            if len(blocks) >= self.budget.max_blocks:
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason="block_budget_exhausted",
                    )
                )
                continue
            representation = self._candidate_representation(candidate)
            if representation == "metadata_only" or (
                delivery_mode == "refs_only" and not candidate.required
            ):
                descriptor_metadata = candidate.metadata
                blocks.append(
                    ContextBlock(
                        block_id=f"context_block:{uuid.uuid4().hex}",
                        block_key=candidate.block_key,
                        source_id=candidate.source_id,
                        source_revision=candidate.source_revision,
                        source_ref=candidate.source_ref,
                        binding_id=candidate.binding_id,
                        role=candidate.role,
                        content=None,
                        completeness="ref_only",
                        content_chars=0,
                        required=candidate.required,
                        refs=(candidate.source_ref,),
                        metadata=descriptor_metadata,
                    )
                )
                self._disclosed.add(self._disclosure_identity(candidate, intent))
                if representation == "metadata_only":
                    diagnostics.append(
                        ContextDiagnostic(
                            code="context.media_content_ref_only",
                            message=(
                                "Non-text Context content was withheld; only its "
                                "canonical source ref is disclosed to this consumer."
                            ),
                            details={
                                "binding_id": candidate.binding_id,
                                "source_ref": candidate.source_ref,
                                "content_kind": self._descriptor_content_kind(
                                    candidate.metadata,
                                    candidate.source_ref,
                                )
                                or "unspecified",
                                "image_attachment_supported": (
                                    self._supports_image_attachments()
                                ),
                            },
                        )
                    )
                continue
            if representation == "image_attachment":
                try:
                    block = await self._read_block(
                        item,
                        max_chars=1,
                        representation="image_attachment",
                        query=intent.query,
                    )
                except ContextStaleError:
                    raise
                except Exception as error:
                    omissions.append(
                        ContextOmission(
                            block_key=candidate.block_key,
                            source_ref=candidate.source_ref,
                            required=candidate.required,
                            reason="source_read_failed",
                            details={
                                "error_type": error.__class__.__name__,
                                "error": str(error),
                            },
                        )
                    )
                    diagnostics.append(
                        ContextDiagnostic(
                            code="context.image_attachment_read_failed",
                            message=(
                                "The selected image could not be prepared as an "
                                "attachment for the capable consumer."
                            ),
                            details={
                                "binding_id": candidate.binding_id,
                                "source_ref": candidate.source_ref,
                                "error_type": error.__class__.__name__,
                                "error": str(error),
                            },
                        )
                    )
                    continue
                blocks.append(block)
                self._disclosed.add(self._disclosure_identity(candidate, intent))
                diagnostics.append(
                    ContextDiagnostic(
                        code="context.image_attachment_delivered",
                        message=(
                            "Image bytes were prepared as a consumer-supported "
                            "attachment; interpretation remains model-owned."
                        ),
                        details={
                            "binding_id": candidate.binding_id,
                            "source_ref": candidate.source_ref,
                        },
                    )
                )
                continue
            if delivery_mode == "refs_only" and not candidate.required:
                descriptor_metadata = (
                    item.source_descriptor.metadata
                    if item.source_descriptor is not None
                    else candidate.metadata
                )
                blocks.append(
                    ContextBlock(
                        block_id=f"context_block:{uuid.uuid4().hex}",
                        block_key=candidate.block_key,
                        source_id=candidate.source_id,
                        source_revision=candidate.source_revision,
                        source_ref=candidate.source_ref,
                        binding_id=candidate.binding_id,
                        role=candidate.role,
                        content=None,
                        completeness="ref_only",
                        content_chars=0,
                        required=False,
                        refs=(candidate.source_ref,),
                        metadata=descriptor_metadata,
                    )
                )
                continue
            requested_block_chars = intent.metadata.get("max_block_chars")
            if requested_block_chars is not None and (
                not isinstance(requested_block_chars, int)
                or isinstance(requested_block_chars, bool)
                or requested_block_chars <= 0
            ):
                raise ValueError("Context max_block_chars must be a positive integer.")
            read_limit = min(
                self.budget.max_block_chars,
                (
                    requested_block_chars
                    if isinstance(requested_block_chars, int)
                    else self.budget.max_block_chars
                ),
                remaining_chars,
            )
            if candidate.required and allow_lossy_required and read_limit > 0:
                read_limit = min(
                    read_limit,
                    max(1, remaining_chars // required_divisor),
                )
            use_lossy_digest = bool(
                candidate.required
                and allow_lossy_required
                and candidate.estimated_chars > read_limit
                and read_limit > 0
            )
            if read_limit <= 0 or (
                candidate.estimated_chars > read_limit
                and (candidate.required or requested_block_chars is None)
            ):
                if use_lossy_digest:
                    try:
                        block = await self._read_block(
                            item,
                            max_chars=read_limit,
                            representation="lossy_digest",
                            query=intent.query,
                        )
                    except ContextStaleError:
                        raise
                    except Exception as error:
                        diagnostics.append(
                            ContextDiagnostic(
                                code="context.required_content_lossy_failed",
                                message="Required Context lossy digest could not be built.",
                                details={
                                    "binding_id": candidate.binding_id,
                                    "source_ref": candidate.source_ref,
                                    "error_type": error.__class__.__name__,
                                    "error": str(error),
                                },
                            )
                        )
                    else:
                        if (
                            block.completeness == "lossy"
                            and 0 < block.content_chars <= read_limit
                            and block.content_chars <= remaining_chars
                        ):
                            blocks.append(block)
                            remaining_chars -= block.content_chars
                            self._disclosed.add(
                                self._disclosure_identity(candidate, intent)
                            )
                            diagnostics.append(
                                ContextDiagnostic(
                                    code="context.required_content_lossy",
                                    message=(
                                        "Required Context content was explicitly delivered as an "
                                        "auditable lossy digest with original refs."
                                    ),
                                    details={
                                        "source_ref": candidate.source_ref,
                                        "estimated_chars": candidate.estimated_chars,
                                        "delivered_chars": block.content_chars,
                                        "refs": list(block.refs),
                                    },
                                )
                            )
                            continue
                reason = (
                    "required_content_incompatible"
                    if candidate.required
                    else "character_budget_exhausted"
                )
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason=reason,
                        details={
                            "estimated_chars": candidate.estimated_chars,
                            "available_chars": read_limit,
                        },
                    )
                )
                if candidate.required:
                    diagnostics.append(
                        ContextDiagnostic(
                            code="context.required_content_incompatible",
                            message=(
                                "Required Context content cannot be delivered completely "
                                "within this consumer budget."
                            ),
                            details={
                                "binding_id": candidate.binding_id,
                                "source_ref": candidate.source_ref,
                                "estimated_chars": candidate.estimated_chars,
                                "available_chars": read_limit,
                            },
                        )
                    )
                continue
            try:
                block = await self._read_block(
                    item,
                    max_chars=read_limit,
                    representation=(
                        "parsed_text" if representation == "parsed_text" else None
                    ),
                    query=intent.query,
                )
            except ContextStaleError:
                raise
            except Exception as error:
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason="source_read_failed",
                        details={
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                        },
                    )
                )
                diagnostics.append(
                    ContextDiagnostic(
                        code="context.source_read_failed",
                        message="A selected Context candidate could not be read.",
                        details={
                            "binding_id": candidate.binding_id,
                            "source_ref": candidate.source_ref,
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                        },
                    )
                )
                continue
            completeness = (
                "truncated" if block.content_chars > read_limit else block.completeness
            )
            if candidate.required and completeness != "complete":
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=True,
                        reason="required_content_incompatible",
                        details={"completeness": completeness},
                    )
                )
                diagnostics.append(
                    ContextDiagnostic(
                        code="context.required_content_incompatible",
                        message="Required Context content was not returned completely.",
                        details={
                            "binding_id": candidate.binding_id,
                            "source_ref": candidate.source_ref,
                            "completeness": completeness,
                        },
                    )
                )
                continue
            if block.content_chars > remaining_chars:
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason="character_budget_exhausted",
                    )
                )
                continue
            blocks.append(block)
            remaining_chars -= block.content_chars
            self._disclosed.add(self._disclosure_identity(candidate, intent))
        return remaining_chars

    async def async_read(self, intent: str | ContextReadIntent) -> ContextPackage:
        self._assert_current()
        resolved_intent = self._coerce_intent(intent)
        for attempt in range(2):
            (
                collected,
                diagnostics,
                source_coverage,
                pending_continuations,
            ) = await self._collect(resolved_intent)
            try:
                self._assert_current()
            except ContextStaleError:
                current = self.task_context.snapshot()
                if current.revision != self._snapshot.revision or attempt > 0:
                    raise
                # A source may establish a lazy index or durable read view while
                # listing candidates. Rebase once and recollect so the package
                # is pinned to the resulting source revisions. Pre-existing
                # staleness and repeated concurrent mutation still fail closed.
                self.refresh()
                continue
            break

        explicit_refs = set(resolved_intent.explicit_refs)
        required_or_explicit: list[_CollectedCandidate] = []
        optional: list[_CollectedCandidate] = []
        omissions: list[ContextOmission] = []

        for item in collected:
            candidate = item.offered
            is_explicit = candidate.source_ref in explicit_refs
            if (
                not candidate.required
                and not is_explicit
                and self._disclosure_identity(candidate, resolved_intent)
                in self._disclosed
            ):
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        reason="already_disclosed",
                    )
                )
                continue
            if candidate.required or is_explicit:
                required_or_explicit.append(item)
            else:
                optional.append(item)

        required_or_explicit.sort(
            key=lambda item: (
                not item.offered.required,
                item.offered.source_ref not in explicit_refs,
                -item.offered.priority,
                item.offered.block_key,
            )
        )
        blocks: list[ContextBlock] = []
        remaining_chars = await self._materialize_candidates(
            required_or_explicit,
            intent=resolved_intent,
            blocks=blocks,
            omissions=omissions,
            diagnostics=diagnostics,
            remaining_chars=self.budget.max_chars,
        )

        optional_keys, selection_diagnostics, selection_failure = await self._select_optional(
            resolved_intent,
            optional,
            available_chars=remaining_chars,
            available_blocks=self.budget.max_blocks - len(blocks),
        )
        diagnostics.extend(selection_diagnostics)
        optional_by_key = {item.offered.block_key: item for item in optional}
        selected_optional = [optional_by_key[key] for key in optional_keys]
        selected_optional_keys = set(optional_keys)
        for item in optional:
            if item.offered.block_key in selected_optional_keys:
                continue
            omissions.append(
                ContextOmission(
                    block_key=item.offered.block_key,
                    source_ref=item.offered.source_ref,
                    reason=selection_failure or "not_selected",
                )
            )
        remaining_chars = await self._materialize_candidates(
            selected_optional,
            intent=resolved_intent,
            blocks=blocks,
            omissions=omissions,
            diagnostics=diagnostics,
            remaining_chars=remaining_chars,
        )

        package = ContextPackage(
            package_id=f"context_package:{uuid.uuid4().hex}",
            task_context_id=self._snapshot.context_id,
            context_revision=self._snapshot.revision,
            consumer_id=self.consumer.consumer_id,
            phase=self.phase,
            source_revisions=self._snapshot.source_revisions,
            source_coverage=source_coverage,
            blocks=tuple(blocks),
            omissions=tuple(omissions),
            diagnostics=tuple(diagnostics),
        )
        cross_source_blocking_diagnostics = {
            "context.semantic_selector_unavailable",
            "context.selection_failed",
            "context.selection_invalid",
        }
        if not any(
            item.code in cross_source_blocking_diagnostics
            for item in diagnostics
        ):
            failed_binding_ids = {
                str(item.details.get("binding_id") or "")
                for item in diagnostics
                if item.code
                in {
                    "context.source_read_failed",
                    "context.required_content_incompatible",
                }
            }
            self._continuations.update(
                {
                    key: continuation
                    for key, continuation in pending_continuations.items()
                    if key[0] not in failed_binding_ids
                }
            )
        self._packages.append(package)
        return package

    async def read(self, intent: str | ContextReadIntent) -> ContextPackage:
        return await self.async_read(intent)


__all__ = ["ContextReader", "ContextStaleError"]
