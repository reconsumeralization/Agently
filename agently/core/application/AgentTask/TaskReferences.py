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

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from agently.utils import DataFormatter


_BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def encode_base62(value: int) -> str:
    if value < 0:
        raise ValueError("Base62 values cannot be negative.")
    if value == 0:
        return _BASE62_ALPHABET[0]
    encoded = ""
    current = value
    while current:
        current, remainder = divmod(current, len(_BASE62_ALPHABET))
        encoded = _BASE62_ALPHABET[remainder] + encoded
    return encoded


def decode_base62(value: str) -> int:
    decoded = 0
    for character in value:
        decoded = decoded * len(_BASE62_ALPHABET) + _BASE62_ALPHABET.index(character)
    return decoded


TASK_REFERENCE_CATALOG_SCHEMA_VERSION = "agent_task_reference_catalog/v1"
REFERENCE_TOKEN_PATTERN = re.compile(r"\[\[ref:(ref_[0-9A-Za-z]+)\]\]")
_IDENTITY_FIELDS = frozenset({"cite_as", "evidence_id", "reference_id", "binding_id"})
_IDENTITY_PREFIXES = ("evd_", "ref_", "bnd_")
_SNAPSHOT_IDENTITY_FIELDS = (
    "snapshot_id",
    "content_version_id",
    "sha256",
    "content_fingerprint",
    "digest",
    "etag",
)
_LOSSY_PROJECTION_MARKERS = (
    "[...body truncated for evidence ledger view...]",
    "[truncated for verifier prompt]",
    "[truncated middle for verifier prompt]",
)


class TaskReferenceCatalog:
    """Private task-scoped identity and host-join owner.

    One monotonically increasing sequence is shared by evidence, reference,
    and binding records. The catalog is in memory until an existing recovery
    or retention seam explicitly activates a RecordStore-backed lease.
    """

    def __init__(self, task_id: str):
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise ValueError("Task reference identity requires a task_id.")
        self.task_id = normalized_task_id
        self._high_water = 0
        self._evidence: dict[str, dict[str, Any]] = {}
        self._references: dict[str, dict[str, Any]] = {}
        self._bindings: dict[str, dict[str, Any]] = {}
        self._fingerprints: dict[str, str] = {}
        self._leased_capacity = 0
        self._task_workspace_leases: list[dict[str, int]] = []

    @property
    def high_water(self) -> int:
        return self._high_water

    def add_evidence(self, item: Mapping[str, Any]) -> dict[str, Any]:
        target = dict(DataFormatter.sanitize(item))
        supplied_evidence_id = str(target.get("evidence_id") or "").strip()
        supplied_reference_id = str(target.get("reference_id") or "").strip()
        if supplied_evidence_id or supplied_reference_id:
            return self._resolve_supplied_identity(target, supplied_evidence_id, supplied_reference_id)

        fingerprint = self._evidence_fingerprint(target)
        existing_id = self._fingerprints.get(fingerprint)
        if existing_id:
            record = self._evidence[existing_id]
            return self._project_canonical_item(record, target_override=target)
        canonical_record = self._canonical_projection_target(target)
        if canonical_record is not None:
            return self._project_canonical_item(canonical_record, target_override=target)

        evidence_id = self._allocate("evd")
        reference_id = self._allocate("ref")
        source_role = self._source_role(target)
        evidence_record = {
            "evidence_id": evidence_id,
            "reference_id": reference_id,
            "task_id": self.task_id,
            "fingerprint": fingerprint,
            "source_role": source_role,
            "target": target,
        }
        reference_record = {
            "reference_id": reference_id,
            "evidence_id": evidence_id,
            "task_id": self.task_id,
            "source_role": source_role,
            "kind": str(target.get("kind") or ""),
            "status": str(target.get("status") or "ok"),
            "body_state": str(target.get("body_state") or "ref_only"),
        }
        self._evidence[evidence_id] = evidence_record
        self._references[reference_id] = reference_record
        self._fingerprints[fingerprint] = evidence_id
        return self._project_canonical_item(evidence_record)

    def _canonical_projection_target(self, target: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Rejoin a lossy view to its immutable canonical evidence object.

        Ledger and verifier projections intentionally remove integrity metadata
        and compact bodies. Those representation changes must not allocate a new
        task reference. A real snapshot discriminator change still represents a
        different object and therefore receives a new identity.
        """
        canonical_id = str(target.get("id") or "").strip()
        if not canonical_id:
            return None
        kind = str(target.get("kind") or "").strip()
        candidates: list[Mapping[str, Any]] = []
        for record in self._evidence.values():
            existing_target = record.get("target")
            if not isinstance(existing_target, Mapping):
                continue
            if str(existing_target.get("id") or "").strip() != canonical_id:
                continue
            if kind and str(existing_target.get("kind") or "").strip() not in {"", kind}:
                continue
            candidates.append(record)
        if not candidates:
            return None

        snapshot_identity = self._snapshot_identity(target)
        if snapshot_identity:
            for record in candidates:
                existing_target = record.get("target")
                if not isinstance(existing_target, Mapping):
                    continue
                existing_snapshot = self._snapshot_identity(existing_target)
                comparable_fields = snapshot_identity.keys() & existing_snapshot.keys()
                if comparable_fields and all(
                    snapshot_identity[field] == existing_snapshot[field]
                    for field in comparable_fields
                ):
                    return record
            return None

        if self._is_lossy_projection(target) and len(candidates) == 1:
            return candidates[0]
        return None

    @staticmethod
    def _snapshot_identity(target: Mapping[str, Any]) -> dict[str, str]:
        identity: dict[str, str] = {}
        for field in _SNAPSHOT_IDENTITY_FIELDS:
            value = str(target.get(field) or "").strip()
            if value:
                identity[field] = value
        provenance = target.get("provenance")
        if isinstance(provenance, Mapping):
            for field in _SNAPSHOT_IDENTITY_FIELDS:
                if field in identity:
                    continue
                value = str(provenance.get(field) or "").strip()
                if value:
                    identity[field] = value
        return identity

    @staticmethod
    def _is_lossy_projection(target: Mapping[str, Any]) -> bool:
        if str(target.get("cite_as") or "").strip():
            return True
        if target.get("body_truncated_for_view") is True or target.get("body_not_rendered") is True:
            return True
        for field in ("body", "content", "text", "snippet", "preview"):
            value = target.get(field)
            if isinstance(value, str) and any(marker in value for marker in _LOSSY_PROJECTION_MARKERS):
                return True
        return False

    def offer_reference(self, evidence_id: str, *, required_role: str | None = None) -> dict[str, Any]:
        normalized_evidence_id = str(evidence_id or "").strip()
        try:
            evidence = self._evidence[normalized_evidence_id]
        except KeyError as error:
            raise ValueError("Task evidence identity is unknown or stale.") from error
        reference_id = str(evidence["reference_id"])
        reference = self._references.get(reference_id)
        if reference is None or reference.get("evidence_id") != normalized_evidence_id:
            raise ValueError("Task reference target is stale.")
        self._require_role(reference, required_role)
        return {
            "reference_id": reference_id,
            "kind": str(reference.get("kind") or ""),
            "status": str(reference.get("status") or ""),
            "body_state": str(reference.get("body_state") or ""),
            "source_role": str(reference.get("source_role") or ""),
        }

    def offered_references(
        self,
        *,
        eligible_roles: Sequence[str] = ("action", "source", "task_workspace_readback"),
    ) -> dict[str, dict[str, Any]]:
        roles = {str(role).strip() for role in eligible_roles if str(role).strip()}
        offered: dict[str, dict[str, Any]] = {}
        for reference_id, reference in self._references.items():
            if str(reference.get("source_role") or "") not in roles:
                continue
            evidence_id = str(reference.get("evidence_id") or "")
            if evidence_id not in self._evidence:
                raise ValueError("Task reference target is stale.")
            offered[reference_id] = self.offer_reference(evidence_id)
        return offered

    def source_cards(self, reference_ids: Sequence[str]) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for reference_id in reference_ids:
            resolved = self.resolve(str(reference_id))
            target = resolved.get("target")
            if not isinstance(target, Mapping):
                raise ValueError("Task reference target is stale.")
            card: dict[str, Any] = {
                "reference_id": str(reference_id),
                "kind": str(resolved.get("kind") or ""),
                "source_role": str(resolved.get("source_role") or ""),
            }
            for field in ("label", "title", "path", "source_url", "canonical_url", "url"):
                value = str(target.get(field) or "").strip()
                if value:
                    card[field] = value[:1000]
            cards.append(dict(DataFormatter.sanitize(card)))
        return cards

    @staticmethod
    def _context_source_body(target: Mapping[str, Any]) -> str:
        """Return the canonical textual body that may enter TaskContext.

        Pointer-only evidence deliberately stays out of this projection.  The
        reference catalog remains the identity owner; this method only exposes
        immutable, body-bearing evidence to the TaskContext retrieval port.
        """
        for field in ("body", "content", "text", "snippet", "preview"):
            value = target.get(field)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, Mapping) or (
                isinstance(value, Sequence)
                and not isinstance(value, str | bytes | bytearray)
            ):
                return json.dumps(
                    DataFormatter.sanitize(value),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        return ""

    def context_source_records(
        self,
        *,
        eligible_roles: Sequence[str] = (
            "action",
            "source",
            "task_workspace_readback",
        ),
    ) -> tuple[dict[str, Any], ...]:
        """Project body-bearing canonical evidence for TaskContext indexing."""
        roles = {str(role).strip() for role in eligible_roles if str(role).strip()}
        records: list[dict[str, Any]] = []
        for reference_id, reference in self._references.items():
            source_role = str(reference.get("source_role") or "")
            if source_role not in roles:
                continue
            evidence_id = str(reference.get("evidence_id") or "")
            evidence = self._evidence.get(evidence_id)
            target = evidence.get("target") if isinstance(evidence, Mapping) else None
            if not isinstance(target, Mapping):
                raise ValueError("Task reference target is stale.")
            body = self._context_source_body(target)
            if not body:
                continue
            records.append(
                dict(
                    DataFormatter.sanitize(
                        {
                            "reference_id": reference_id,
                            "evidence_id": evidence_id,
                            "source_role": source_role,
                            "kind": reference.get("kind"),
                            "status": reference.get("status"),
                            "body_state": reference.get("body_state"),
                            "body": body,
                            "target": {
                                field: target.get(field)
                                for field in (
                                    "id",
                                    "label",
                                    "title",
                                    "path",
                                    "action_id",
                                    "action_call_id",
                                    "selection_key",
                                    "source_id",
                                    "source_revision",
                                    "source_ref",
                                    "query",
                                    "range_start",
                                    "provenance",
                                )
                                if target.get(field) not in (None, "", [], {})
                            },
                        }
                    )
                )
            )
        return tuple(records)

    def context_source_revision(self) -> str:
        """Digest only evidence that is actually readable through TaskContext."""
        digest = hashlib.sha256()
        for record in self.context_source_records():
            digest.update(str(record.get("reference_id") or "").encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(record.get("evidence_id") or "").encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(record.get("body") or "").encode("utf-8"))
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def bind(
        self,
        subject: str,
        offered_reference_ids: Sequence[str],
        *,
        required_role: str | None = None,
    ) -> dict[str, Any]:
        normalized_subject = str(subject or "").strip()
        if not normalized_subject:
            raise ValueError("Task reference binding requires a subject.")
        reference_ids = [str(value or "").strip() for value in offered_reference_ids]
        if not reference_ids or any(not value for value in reference_ids):
            raise ValueError("Task reference binding requires offered reference ids.")
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("Task reference binding contains duplicate reference ids.")
        for reference_id in reference_ids:
            if not reference_id.startswith("ref_"):
                if reference_id.startswith(_IDENTITY_PREFIXES):
                    raise ValueError(
                        "Task binding cannot accept copied canonical identities in place of reference ids."
                    )
                raise ValueError("Task binding accepts only offered reference ids.")
            reference = self._references.get(reference_id)
            if reference is None:
                raise ValueError("Task binding contains a reference that was not offered by this task.")
            if str(reference.get("task_id") or "") != self.task_id:
                raise ValueError("Task binding contains a cross-task reference.")
            evidence_id = str(reference.get("evidence_id") or "")
            if evidence_id not in self._evidence:
                raise ValueError("Task binding contains a stale reference target.")
            self._require_role(reference, required_role)
        binding_id = self._allocate("bnd")
        record = {
            "binding_id": binding_id,
            "task_id": self.task_id,
            "subject": normalized_subject,
            "reference_ids": reference_ids,
            "required_role": str(required_role or ""),
        }
        self._bindings[binding_id] = record
        return dict(DataFormatter.sanitize(record))

    def resolve(self, reference_id: str, *, task_id: str | None = None) -> dict[str, Any]:
        if task_id is not None and str(task_id) != self.task_id:
            raise ValueError("Task reference cannot be resolved across task scopes.")
        normalized_reference_id = str(reference_id or "").strip()
        reference = self._references.get(normalized_reference_id)
        if reference is None:
            raise ValueError("Task reference identity is unknown or stale.")
        if str(reference.get("task_id") or "") != self.task_id:
            raise ValueError("Task reference belongs to a different task scope.")
        evidence_id = str(reference.get("evidence_id") or "")
        evidence = self._evidence.get(evidence_id)
        if evidence is None:
            raise ValueError("Task reference target is stale.")
        return dict(
            DataFormatter.sanitize(
                {
                    **reference,
                    "target": evidence.get("target", {}),
                }
            )
        )

    def snapshot(self) -> dict[str, Any]:
        persistent_evidence = {
            evidence_id: {
                **record,
                "target": self._persistent_target(cast(Mapping[str, Any], record.get("target", {}))),
            }
            for evidence_id, record in self._evidence.items()
        }
        return dict(
            DataFormatter.sanitize(
                {
                    "schema_version": TASK_REFERENCE_CATALOG_SCHEMA_VERSION,
                    "task_id": self.task_id,
                    "high_water": str(self._high_water),
                    "leased_capacity": str(self._leased_capacity),
                    "task_workspace_leases": self._task_workspace_leases,
                    "evidence": persistent_evidence,
                    "references": self._references,
                    "bindings": self._bindings,
                    "fingerprints": self._fingerprints,
                }
            )
        )

    @classmethod
    def from_snapshot(cls, task_id: str, value: Mapping[str, Any]) -> "TaskReferenceCatalog":
        if value.get("schema_version") != TASK_REFERENCE_CATALOG_SCHEMA_VERSION:
            raise ValueError("Task reference catalog snapshot schema is unsupported.")
        if str(value.get("task_id") or "") != str(task_id):
            raise ValueError("Task reference catalog snapshot belongs to a different task.")
        high_water = cls._decimal_counter(value.get("high_water"), label="high water")
        leased_capacity = cls._decimal_counter(value.get("leased_capacity", "0"), label="leased capacity")
        catalog = cls(task_id)
        catalog._high_water = high_water
        catalog._leased_capacity = leased_capacity
        catalog._evidence = cls._record_mapping(value.get("evidence"), "evidence")
        catalog._references = cls._record_mapping(value.get("references"), "references")
        catalog._bindings = cls._record_mapping(value.get("bindings"), "bindings")
        fingerprints = value.get("fingerprints")
        if not isinstance(fingerprints, Mapping):
            raise ValueError("Task reference catalog fingerprint index is invalid.")
        catalog._fingerprints = {str(key): str(item) for key, item in fingerprints.items()}
        leases = value.get("task_workspace_leases", [])
        if not isinstance(leases, Sequence) or isinstance(leases, str | bytes | bytearray):
            raise ValueError("Task reference catalog lease list is invalid.")
        catalog._task_workspace_leases = [
            {"start": int(cast(Mapping[str, Any], lease)["start"]), "end": int(cast(Mapping[str, Any], lease)["end"])}
            for lease in leases
            if isinstance(lease, Mapping)
        ]
        catalog._validate_snapshot_graph()
        return catalog

    def _allocate(self, prefix: str) -> str:
        self._high_water += 1
        return f"{prefix}_{encode_base62(self._high_water)}"

    def _resolve_supplied_identity(
        self,
        target: Mapping[str, Any],
        evidence_id: str,
        reference_id: str,
    ) -> dict[str, Any]:
        if not evidence_id or not reference_id:
            raise ValueError("Task evidence and reference identities must be carried together.")
        evidence = self._evidence.get(evidence_id)
        reference = self._references.get(reference_id)
        if evidence is None or reference is None:
            raise ValueError("Task evidence contains an unknown or stale identity.")
        if evidence.get("reference_id") != reference_id or reference.get("evidence_id") != evidence_id:
            raise ValueError("Task evidence/reference identity join is invalid.")
        original_target = evidence.get("target")
        if not isinstance(original_target, Mapping):
            raise ValueError("Task evidence identity has a stale target.")
        for field in (
            "id",
            "kind",
            "execution_block_id",
            "block_id",
            "source_id",
            "source_revision",
            "source_ref",
            "binding_id",
            "query",
            "range_start",
            "action_call_id",
            "action_id",
            "path",
            "record_id",
            "source_url",
            "url",
        ):
            supplied = target.get(field)
            original = original_target.get(field)
            if supplied not in (None, "", [], {}) and original not in (None, "", [], {}) and supplied != original:
                raise ValueError(
                    "Task evidence identity cannot be retargeted after allocation "
                    f"(field={field})."
                )
        supplied_status = str(target.get("status") or "").strip()
        original_status = str(original_target.get("status") or "").strip()
        if supplied_status and original_status and supplied_status != original_status:
            raise ValueError(
                "Task evidence identity cannot be retargeted after allocation "
                "(field=status)."
            )
        supplied_body_state = str(target.get("body_state") or "").strip()
        original_body_state = str(original_target.get("body_state") or "").strip()
        if (
            supplied_body_state
            and original_body_state
            and supplied_body_state != original_body_state
        ):
            visibility_rank = {
                "ref_only": 0,
                "truncated": 1,
                "bounded": 2,
                "full": 3,
            }
            lossy_projection = self._is_lossy_projection(target)
            supplied_rank = visibility_rank.get(supplied_body_state, -1)
            original_rank = visibility_rank.get(original_body_state, -1)
            if (
                not lossy_projection
                or supplied_rank < 0
                or original_rank < 0
                or supplied_rank > original_rank
            ):
                raise ValueError(
                    "Task evidence identity cannot be retargeted after allocation "
                    "(field=body_state)."
                )
        return self._project_canonical_item(evidence, target_override=target)

    @staticmethod
    def _project_canonical_item(
        record: Mapping[str, Any],
        *,
        target_override: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = target_override if target_override is not None else record.get("target")
        output = dict(target) if isinstance(target, Mapping) else {}
        output["evidence_id"] = str(record.get("evidence_id") or "")
        output["reference_id"] = str(record.get("reference_id") or "")
        return dict(DataFormatter.sanitize(output))

    @staticmethod
    def _persistent_target(target: Mapping[str, Any]) -> dict[str, Any]:
        fields = (
            "id",
            "kind",
            "status",
            "raw_status",
            "body_state",
            "execution_block_id",
            "block_id",
            "source_id",
            "source_revision",
            "source_ref",
            "binding_id",
            "query",
            "range_start",
            "action_id",
            "action_call_id",
            "path",
            "record_id",
            "selection_key",
            "artifact_id",
            "source_url",
            "selected_url",
            "requested_url",
            "canonical_url",
            "url",
            "href",
            "label",
            "title",
            "locator_id",
            "content_version_id",
            "role",
            "source",
        )
        return {
            field: DataFormatter.sanitize(target.get(field))
            for field in fields
            if target.get(field) not in (None, "", [], {})
        }

    @staticmethod
    def _source_role(item: Mapping[str, Any]) -> str:
        kind = str(item.get("kind") or "").strip().lower()
        if str(item.get("action_call_id") or "").strip() or "action.result" in kind or kind == "action_evidence":
            return "action"
        if (
            kind == "taskboard_action_artifact.readback"
            and str(item.get("owner") or "").strip().lower() == "action_artifact"
        ):
            # This is a body snapshot of an Action-owned result, not a
            # TaskWorkspace deliverable being transported toward promotion.
            # Keep the readback as a distinct evidence object while retaining
            # the source role of the operation that produced the body.
            return "action"
        declared_role = str(item.get("role") or "").strip().lower()
        source = str(item.get("source") or "").strip().lower()
        if (
            "delivery" in kind
            or "candidate" in kind
            or "acceptance" in kind
            or "artifact.readback" in kind
            or "artifact.targeted_readback" in kind
            or "verifier_readback" in kind
            or (
                kind in {"taskboard_ref", "taskboard_evidence_ref"}
                and declared_role == "task_workspace_artifact"
                and (
                    (
                        source.startswith("agent_task.taskboard.card.")
                        and source.endswith(".task_workspace_artifact")
                    )
                    or source.startswith("agent_task.task_workspace_artifact.")
                )
            )
        ):
            return "transport"
        if "readback" in kind:
            return "task_workspace_readback"
        return "source"

    @staticmethod
    def _require_role(reference: Mapping[str, Any], required_role: str | None) -> None:
        normalized_role = str(required_role or "").strip()
        if normalized_role and str(reference.get("source_role") or "") != normalized_role:
            raise ValueError(f"Task reference is not eligible for required role '{normalized_role}'.")

    @staticmethod
    def _evidence_fingerprint(item: Mapping[str, Any]) -> str:
        canonical = {
            str(key): value for key, value in DataFormatter.sanitize(item).items() if str(key) not in _IDENTITY_FIELDS
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _decimal_counter(value: Any, *, label: str) -> int:
        if not isinstance(value, str) or not value.isdecimal() or (len(value) > 1 and value.startswith("0")):
            raise ValueError(f"Task reference catalog {label} is invalid.")
        return int(value)

    @staticmethod
    def _record_mapping(value: Any, label: str) -> dict[str, dict[str, Any]]:
        if not isinstance(value, Mapping):
            raise ValueError(f"Task reference catalog {label} records are invalid.")
        output: dict[str, dict[str, Any]] = {}
        for key, item in value.items():
            if not isinstance(item, Mapping):
                raise ValueError(f"Task reference catalog {label} record is invalid.")
            output[str(key)] = dict(DataFormatter.sanitize(item))
        return output

    def _validate_snapshot_graph(self) -> None:
        seen_sequences: set[int] = set()
        for prefix, records in (("evd", self._evidence), ("ref", self._references), ("bnd", self._bindings)):
            for entity_id, record in records.items():
                expected_field = {"evd": "evidence_id", "ref": "reference_id", "bnd": "binding_id"}[prefix]
                if record.get(expected_field) != entity_id or record.get("task_id") != self.task_id:
                    raise ValueError("Task reference catalog contains a cross-task or mismatched record.")
                actual_prefix, separator, encoded = entity_id.partition("_")
                if not separator or actual_prefix != prefix:
                    raise ValueError("Task reference catalog contains an invalid entity id.")
                try:
                    sequence = decode_base62(encoded)
                except ValueError as error:
                    raise ValueError("Task reference catalog contains an invalid entity id.") from error
                if sequence <= 0 or sequence > self._high_water or sequence in seen_sequences:
                    raise ValueError("Task reference catalog identity sequence is invalid or duplicated.")
                seen_sequences.add(sequence)
        for evidence_id, evidence in self._evidence.items():
            reference_id = str(evidence.get("reference_id") or "")
            reference = self._references.get(reference_id)
            if reference is None or reference.get("evidence_id") != evidence_id:
                raise ValueError("Task reference catalog contains a stale reference target.")
            fingerprint = str(evidence.get("fingerprint") or "")
            if not fingerprint or self._fingerprints.get(fingerprint) != evidence_id:
                raise ValueError("Task reference catalog fingerprint join is invalid.")
        for binding in self._bindings.values():
            reference_ids = binding.get("reference_ids")
            if not isinstance(reference_ids, Sequence) or isinstance(reference_ids, str | bytes | bytearray):
                raise ValueError("Task reference catalog binding references are invalid.")
            if any(str(reference_id) not in self._references for reference_id in reference_ids):
                raise ValueError("Task reference catalog binding contains a stale reference target.")


def parse_reference_tokens(text: str) -> list[str]:
    return REFERENCE_TOKEN_PATTERN.findall(str(text or ""))


def validate_reference_tokens(text: str, offered: Mapping[str, Any]) -> dict[str, Any]:
    raw_text = str(text or "")
    reference_ids = parse_reference_tokens(raw_text)
    if "[[ref:" in REFERENCE_TOKEN_PATTERN.sub("", raw_text):
        raise ValueError("Artifact contains a malformed reference token.")
    unique_reference_ids = list(dict.fromkeys(reference_ids))
    for reference_id in unique_reference_ids:
        record = offered.get(reference_id)
        if not isinstance(record, Mapping) or record.get("reference_id") != reference_id:
            raise ValueError("Artifact reference token was not present in the offered source map.")
        if str(record.get("source_role") or "") == "transport":
            raise ValueError("Artifact reference token is not eligible as a grounding source role.")
    return {"reference_ids": unique_reference_ids}


__all__ = [
    "TASK_REFERENCE_CATALOG_SCHEMA_VERSION",
    "TaskReferenceCatalog",
    "parse_reference_tokens",
    "validate_reference_tokens",
]
