# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import hashlib
import uuid

from .LifecycleState import TerminalCarrier, TerminalCarrierInventory
from .TaskShared import *


class AgentTaskTerminalVerificationMixin(AgentTaskMixinBase):
    """Materialize and resolve the one current terminal carrier inventory."""

    async def _allocate_terminal_carrier_id(self) -> str:
        return f"car_{uuid.uuid4().hex}"

    @staticmethod
    def _terminal_carrier_reuse_key(
        *,
        kind: str,
        path: str,
        content_version_id: str,
    ) -> tuple[str, str, str]:
        return (str(kind), str(path), str(content_version_id))

    async def _replace_terminal_carriers(
        self,
        *,
        execution_result: Any,
        execution_evidence_summary: Mapping[str, Any],
        source_work_result_id: str,
    ) -> TerminalCarrierInventory:
        work_result_id = str(source_work_result_id or "").strip()
        if not work_result_id:
            raise ValueError("Terminal carrier materialization requires source_work_result_id.")
        inline_text = self._candidate_final_result_from_execution_result(execution_result).strip()
        diagnostics: list[dict[str, Any]] = []
        raw_carriers: list[dict[str, Any]] = []
        current_file_refs = self._trusted_terminal_file_refs(execution_result)
        cumulative_file_refs = self._trusted_task_workspace_artifact_refs_from_summary(
            execution_evidence_summary
        )
        text_file_refs = [
            ref
            for ref in current_file_refs
            if str(ref.get("content_kind") or "text") in {"", "text"}
        ]
        if not text_file_refs:
            text_file_refs = [
                ref
                for ref in cumulative_file_refs
                if str(ref.get("content_kind") or "text") in {"", "text"}
            ]
        required_deliverables = self._required_task_workspace_deliverables()
        candidate_paths: list[str] = []
        for required_path in required_deliverables:
            path = self._task_workspace_artifact_display_path(required_path)
            if path and path not in candidate_paths:
                candidate_paths.append(path)
        # A declared deliverable path is the terminal TaskWorkspace carrier owner.
        # Upstream/working artifacts remain cold evidence and must not compete
        # with that path in the semantic terminal audit. If no deliverable was
        # declared, current trusted refs remain the structural fallback.
        if not candidate_paths:
            for ref in text_file_refs:
                path = self._task_workspace_artifact_display_path(ref.get("path"))
                if path and path not in candidate_paths:
                    candidate_paths.append(path)

        required_paths = {
            self._task_workspace_artifact_display_path(path)
            for path in required_deliverables
            if self._task_workspace_artifact_display_path(path)
        }
        for path in candidate_paths:
            try:
                promoted = await self.task_workspace._promote_file_identity(
                    path,
                    role="terminal_carrier",
                )
                content_version_id = str(promoted.get("content_version_id") or "").strip()
                digest = str(promoted.get("sha256") or "").strip()
                size = int(promoted.get("bytes") or promoted.get("size") or 0)
                if not content_version_id or not digest:
                    raise ValueError("TaskWorkspace did not return a versioned content identity.")
                readback = await self.task_workspace.read_file(path, max_bytes=max(1, size + 1))
                text = readback.get("content")
                if not isinstance(text, str) or bool(readback.get("truncated")):
                    raise ValueError("Terminal carrier readback was not complete text.")
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
                    raise ValueError("Terminal carrier readback digest does not match its content version.")
                raw_carriers.append(
                    {
                        "kind": "task_workspace_artifact",
                        "required": path in required_paths or not required_paths,
                        "text": text,
                        "path": path,
                        "content_version_id": content_version_id,
                        "content_digest": digest,
                        "source_work_result_id": work_result_id,
                        "status": "materialized",
                    }
                )
            except Exception as error:
                diagnostics.append(
                    {
                        "code": "agent_task.terminal_carrier.readback_failed",
                        "path": path,
                        "message": _compact_agent_task_error_message(
                            error,
                            fallback=error.__class__.__name__,
                        ),
                    }
                )

        inline_is_pointer = (
            not inline_text
            or self._looks_like_task_workspace_artifact_placeholder(inline_text)
            or inline_text in candidate_paths
        )
        if inline_text and not inline_is_pointer:
            digest = hashlib.sha256(inline_text.encode("utf-8")).hexdigest()
            raw_carriers.append(
                {
                    "kind": "inline_final_result",
                    "required": True,
                    "text": inline_text,
                    "path": "",
                    "content_version_id": f"inline:{digest}",
                    "content_digest": digest,
                    "source_work_result_id": work_result_id,
                    "status": "materialized",
                }
            )

        current_inventory = self._lifecycle_state.carrier_inventory
        reusable_ids = {
            self._terminal_carrier_reuse_key(
                kind=carrier.kind,
                path=carrier.path,
                content_version_id=carrier.content_version_id,
            ): carrier.carrier_id
            for carrier in current_inventory.carriers
        }
        carrier_values: list[dict[str, Any]] = []
        next_inline_values: dict[str, str] = {}
        for raw_carrier in raw_carriers:
            reuse_key = self._terminal_carrier_reuse_key(
                kind=str(raw_carrier.get("kind") or ""),
                path=str(raw_carrier.get("path") or ""),
                content_version_id=str(raw_carrier.get("content_version_id") or ""),
            )
            carrier_id = reusable_ids.get(reuse_key)
            if not carrier_id:
                carrier_id = await self._allocate_terminal_carrier_id()
            carrier_value = {
                key: value
                for key, value in raw_carrier.items()
                if key != "text"
            }
            carrier_value["carrier_id"] = carrier_id
            carrier_values.append(carrier_value)
            if raw_carrier.get("kind") == "inline_final_result":
                next_inline_values[str(raw_carrier["content_version_id"])] = str(
                    raw_carrier.get("text") or ""
                )

        inventory = self._lifecycle_state.replace_carriers(
            carrier_values,
            expected_version=self._lifecycle_state.state_version,
        )
        self._terminal_inline_values = next_inline_values
        self._terminal_materialization_diagnostics = diagnostics
        return inventory

    async def _current_terminal_candidate(self) -> dict[str, Any]:
        inventory = self._lifecycle_state.carrier_inventory
        carriers: list[dict[str, Any]] = []
        diagnostics = list(self._terminal_materialization_diagnostics)
        for carrier in inventory.carriers:
            text = ""
            if carrier.kind == "task_workspace_artifact":
                promoted = await self.task_workspace._promote_file_identity(
                    carrier.path,
                    role="terminal_carrier_readback",
                )
                current_version = str(promoted.get("content_version_id") or "").strip()
                current_digest = str(promoted.get("sha256") or "").strip()
                if (
                    current_version != carrier.content_version_id
                    or current_digest != carrier.content_digest
                ):
                    raise ValueError(
                        f"Terminal carrier {carrier.carrier_id} is stale for TaskWorkspace path {carrier.path}."
                    )
                size = int(promoted.get("bytes") or promoted.get("size") or 0)
                readback = await self.task_workspace.read_file(
                    carrier.path,
                    max_bytes=max(1, size + 1),
                )
                text = str(readback.get("content") or "")
                if bool(readback.get("truncated")):
                    raise ValueError(
                        f"Terminal carrier {carrier.carrier_id} could not be read completely."
                    )
            else:
                text = self._terminal_inline_values.get(carrier.content_version_id, "")
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != carrier.content_digest:
                    raise ValueError(
                        f"Inline terminal carrier {carrier.carrier_id} is missing or stale."
                    )
            carriers.append(
                {
                    **carrier.to_dict(),
                    "text": text,
                    "diagnostics": [],
                }
            )
        if not carriers:
            return {
                "kind": "",
                "carrier_id": "",
                "text": "",
                "path": "",
                "content_version_id": "",
                "diagnostics": diagnostics,
                "inventory_version": inventory.inventory_version,
                "state_version": inventory.state_version,
                "carriers": [],
            }
        primary = dict(carriers[0])
        return {
            **primary,
            "diagnostics": diagnostics,
            "inventory_version": inventory.inventory_version,
            "state_version": inventory.state_version,
            "carriers": carriers,
        }

    def _terminal_carrier_for_repair_contract(
        self,
        repair_contract: Mapping[str, Any],
    ) -> TerminalCarrier | None:
        raw_requirements = repair_contract.get("requirements")
        requirements = (
            [item for item in raw_requirements if isinstance(item, Mapping)]
            if isinstance(raw_requirements, Sequence)
            and not isinstance(raw_requirements, str | bytes | bytearray)
            else []
        )
        carrier_ids = {
            str(item.get("carrier_id") or "").strip()
            for item in requirements
            if str(item.get("carrier_id") or "").strip()
        }
        if len(carrier_ids) != 1:
            return None
        carrier_id = next(iter(carrier_ids))
        carrier = next(
            (
                item
                for item in self._lifecycle_state.carrier_inventory.carriers
                if item.carrier_id == carrier_id
            ),
            None,
        )
        if carrier is None:
            return None
        versions = {
            str(item.get("content_version_id") or "").strip()
            for item in requirements
            if str(item.get("content_version_id") or "").strip()
        }
        if versions and versions != {carrier.content_version_id}:
            return None
        return carrier

    async def _terminal_inventory_task_workspace_refs(self) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        inventory = self._lifecycle_state.carrier_inventory
        for carrier in inventory.carriers:
            if carrier.kind != "task_workspace_artifact":
                continue
            promoted = await self.task_workspace._promote_file_identity(
                carrier.path,
                role="terminal_carrier",
            )
            if (
                str(promoted.get("content_version_id") or "")
                != carrier.content_version_id
                or str(promoted.get("sha256") or "") != carrier.content_digest
            ):
                raise ValueError(
                    f"Terminal carrier {carrier.carrier_id} changed before result projection."
                )
            refs.append(
                {
                    **dict(DataFormatter.sanitize(promoted)),
                    "role": "task_workspace_artifact",
                    "source": "agent_task.terminal_carrier_inventory",
                    "carrier_id": carrier.carrier_id,
                }
            )
        return refs

    async def _missing_required_task_workspace_deliverables(self) -> list[str]:
        missing: list[str] = []
        for path in self._required_task_workspace_deliverables():
            try:
                read_result = await self.task_workspace.read_file(path, max_bytes=1)
            except Exception:
                missing.append(path)
                continue
            try:
                size = int(read_result.get("bytes") or 0)
            except (TypeError, ValueError):
                size = 0
            if size <= 0:
                missing.append(path)
        return missing

    def _guard_missing_required_deliverables(
        self,
        verification: dict[str, Any],
        missing_deliverables: Sequence[str],
    ) -> None:
        missing = self._normalize_string_list(missing_deliverables)
        if not missing:
            return
        message = "Missing required TaskWorkspace deliverable(s): " + ", ".join(missing)
        verification["is_complete"] = False
        verification["final_result_required"] = True
        verification["missing_criteria"] = self._merge_string_lists(
            verification.get("missing_criteria"),
            [message],
        )
        verification["acceptance_delta"] = self._merge_string_lists(
            verification.get("acceptance_delta"),
            [message],
        )
        verification["guard_reasons"] = self._merge_string_lists(
            verification.get("guard_reasons"),
            ["required_task_workspace_deliverable_missing"],
        )
        if not str(verification.get("failure_analysis") or "").strip():
            verification["failure_analysis"] = message
        if not str(verification.get("replan_instruction") or "").strip():
            verification["replan_instruction"] = (
                "Write and read back the required TaskWorkspace deliverable before accepting completion."
            )

    def _apply_terminal_guard_issues(
        self,
        verification: dict[str, Any],
        issues: Sequence[Mapping[str, Any]],
    ) -> None:
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            code = str(issue.get("code") or "terminal_guard_failed").strip()
            reason = str(issue.get("reason") or code).strip()
            verification["is_complete"] = False
            if issue.get("requires_block") is True:
                verification["requires_block"] = True
            verification["reason"] = reason
            verification["failure_analysis"] = reason
            verification["missing_criteria"] = self._merge_string_lists(
                verification.get("missing_criteria"),
                issue.get("missing_criteria") or [reason],
            )
            verification["acceptance_delta"] = self._merge_string_lists(
                verification.get("acceptance_delta"),
                issue.get("acceptance_delta") or [reason],
            )
            verification["guard_reasons"] = self._merge_string_lists(
                verification.get("guard_reasons"),
                [code],
            )

    @staticmethod
    def _terminal_repair_contract(verification: Mapping[str, Any]) -> dict[str, Any]:
        convergence = verification.get("terminal_convergence")
        if isinstance(convergence, Mapping):
            convergence_contract = convergence.get("repair_contract")
            if isinstance(convergence_contract, Mapping) and convergence_contract:
                return dict(DataFormatter.sanitize(convergence_contract))
        for field in (
            "criterion_repair_contract",
            "material_claim_repair_contract",
        ):
            contract = verification.get(field)
            if isinstance(contract, Mapping) and contract:
                return dict(DataFormatter.sanitize(contract))
        return {}

    @classmethod
    def _terminal_rejected_carrier_ids(
        cls,
        verification: Mapping[str, Any],
        *,
        offered_carrier_ids: set[str],
    ) -> list[str]:
        rejected: list[str] = []

        def add(value: Any) -> None:
            carrier_id = str(value or "").strip()
            if carrier_id in offered_carrier_ids and carrier_id not in rejected:
                rejected.append(carrier_id)

        audit = verification.get("material_claim_audit")
        if isinstance(audit, Mapping):
            for carrier_id in audit.get("failed_carrier_ids") or []:
                add(carrier_id)
        checks = verification.get("material_claim_checks")
        if isinstance(checks, Sequence) and not isinstance(
            checks,
            str | bytes | bytearray,
        ):
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                if str(check.get("state") or "").strip() not in {
                    "supported",
                    "reasonable_derived",
                }:
                    add(check.get("carrier_id"))
        for carrier_id in verification.get("rejected_carrier_ids") or []:
            add(carrier_id)
        return rejected

    async def _run_terminal_verification(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        execution_result: Any,
        execution_meta: dict[str, Any],
        context_pack: "TaskContextView",
        missing_deliverables: Sequence[str] | None = None,
        terminal_guard_issues: Sequence[Mapping[str, Any]] = (),
        preferred_final_result: Any = None,
        terminal_refs: Sequence[Mapping[str, Any]] | None = None,
        preserve_final_result: bool = False,
    ) -> dict[str, Any]:
        """Run the one terminal verifier and return one host-owned transition."""

        raw_verification = await self._request_verification(
            iteration_index,
            plan=plan,
            execution_result=execution_result,
            execution_meta=execution_meta,
            context_pack=context_pack,
        )
        cumulative_evidence_summary = self._cumulative_execution_evidence_summary(
            execution_meta
        )
        candidate = await self._current_terminal_candidate()
        if not candidate.get("carriers"):
            await self._replace_terminal_carriers(
                execution_result=execution_result,
                execution_evidence_summary=cumulative_evidence_summary,
                source_work_result_id=str(
                    execution_meta.get("execution_id")
                    or f"iteration:{iteration_index}"
                ),
            )
            candidate = await self._current_terminal_candidate()
        if isinstance(raw_verification, Mapping):
            verification = dict(raw_verification)
        else:
            verification = {
                "is_complete": False,
                "requires_block": False,
                "reason": str(raw_verification),
                "missing_criteria": list(self.success_criteria),
                "final_result_required": False,
                "final_result": "",
            }
        if verification.get("strict_terminal_gates_applied") is not True:
            # `_request_verification` owns the semantic criterion/material-claim
            # audit. Instance-level test/application overrides may return its
            # pre-normalized compatibility shape, so apply only host-owned
            # lifecycle/capability/status normalization here. Do not run a
            # second semantic audit or invent terminal carrier judgments.
            verification = self._normalize_verification(
                verification,
                execution_evidence_summary=cumulative_evidence_summary,
                candidate_final_result=self._candidate_final_result_from_execution_result(
                    execution_result
                ),
                terminal_candidate=None,
            )
        effective_missing_deliverables = missing_deliverables
        if effective_missing_deliverables is None and bool(
            verification.get("is_complete")
        ):
            effective_missing_deliverables = (
                await self._missing_required_task_workspace_deliverables()
            )
        if effective_missing_deliverables:
            self._guard_missing_required_deliverables(
                verification,
                effective_missing_deliverables,
            )
        self._apply_terminal_guard_issues(verification, terminal_guard_issues)

        inventory = self._lifecycle_state.carrier_inventory
        offered_carriers = list(inventory.carriers)
        offered_carrier_ids = {carrier.carrier_id for carrier in offered_carriers}
        required_carrier_ids = {
            carrier.carrier_id for carrier in offered_carriers if carrier.required
        }
        rejected_carrier_ids = self._terminal_rejected_carrier_ids(
            verification,
            offered_carrier_ids=offered_carrier_ids,
        )
        rejected_required_ids = sorted(
            required_carrier_ids.intersection(rejected_carrier_ids)
        )
        if bool(verification.get("is_complete")) and rejected_required_ids:
            message = (
                "Required terminal carrier(s) were rejected by the current material-claim audit: "
                + ", ".join(rejected_required_ids)
            )
            verification["is_complete"] = False
            verification["requires_block"] = False
            verification["reason"] = message
            verification["missing_criteria"] = self._merge_string_lists(
                verification.get("missing_criteria"),
                [message],
            )
            verification["acceptance_delta"] = self._merge_string_lists(
                verification.get("acceptance_delta"),
                [message],
            )
            verification["guard_reasons"] = self._merge_string_lists(
                verification.get("guard_reasons"),
                ["required_terminal_carrier_rejected"],
            )

        repair_contract = self._terminal_repair_contract(verification)
        if bool(verification.get("is_complete")):
            transition = "accepted"
        elif bool(verification.get("requires_block")):
            transition = "blocked"
        elif (
            repair_contract.get("gate_kind") == "output_contract"
            and repair_contract.get("issue_code")
            == "terminal_verifier_output_invalid"
        ):
            transition = "verification_retry"
        elif repair_contract or verification.get("missing_criteria"):
            transition = "repair"
        else:
            transition = "continue"

        accepted_carrier_ids = (
            [carrier.carrier_id for carrier in offered_carriers]
            if transition == "accepted"
            else []
        )
        supplied_terminal_refs = (
            list(terminal_refs)
            if terminal_refs is not None
            else self._trusted_terminal_refs(execution_result, verification)
        )
        inventory_task_workspace_refs = await self._terminal_inventory_task_workspace_refs()
        effective_terminal_refs = self._trusted_terminal_refs(
            supplied_terminal_refs,
            inventory_task_workspace_refs,
        )
        terminal_file_refs = self._trusted_terminal_file_refs(effective_terminal_refs)
        final_result_value = (
            preferred_final_result
            if preferred_final_result is not None
            else verification.get("final_result", "")
        )
        if final_result_value in (None, "", [], {}):
            inline_carrier = next(
                (
                    item
                    for item in candidate.get("carriers") or []
                    if isinstance(item, Mapping)
                    and item.get("kind") == "inline_final_result"
                ),
                None,
            )
            if isinstance(inline_carrier, Mapping):
                final_result_value = inline_carrier.get("text", "")
        preserve_value = preserve_final_result or str(
            plan.get("deliverable_mode") or ""
        ) == "inline_final"
        terminal_result = {
            "carrier_ids": [carrier.carrier_id for carrier in offered_carriers],
            "required_carrier_ids": [
                carrier.carrier_id for carrier in offered_carriers if carrier.required
            ],
            "task_workspace_paths": [
                carrier.path
                for carrier in offered_carriers
                if carrier.kind == "task_workspace_artifact"
            ],
            "final_result": self._compact_terminal_final_result(
                final_result_value,
                trusted_file_refs=terminal_file_refs,
                preserve_value=preserve_value,
            ),
            "terminal_refs": DataFormatter.sanitize(effective_terminal_refs),
            "final_file_refs": DataFormatter.sanitize(terminal_file_refs),
        }
        terminal_convergence = verification.get("terminal_convergence")
        issue = (
            dict(DataFormatter.sanitize(terminal_convergence.get("issue")))
            if isinstance(terminal_convergence, Mapping)
            and isinstance(terminal_convergence.get("issue"), Mapping)
            else {
                "gate_kind": repair_contract.get("gate_kind"),
                "issue_code": repair_contract.get("issue_code"),
                "contract_subject": repair_contract.get("contract_subject"),
            }
            if repair_contract
            else {}
        )
        decided_inventory = self._lifecycle_state.record_terminal_transition(
            transition,
            expected_version=self._lifecycle_state.state_version,
            accepted_carrier_ids=accepted_carrier_ids,
            rejected_carrier_ids=rejected_carrier_ids,
            issue=issue,
            repair_contract=repair_contract,
        )
        return {
            "transition": transition,
            "verification": DataFormatter.sanitize(verification),
            "issue": issue,
            "repair_contract": repair_contract,
            "accepted_carrier_ids": accepted_carrier_ids,
            "rejected_carrier_ids": rejected_carrier_ids,
            "terminal_result": terminal_result,
            "state_version": decided_inventory.state_version,
            "carrier_inventory_version": decided_inventory.inventory_version,
        }


__all__: list[str] = []
