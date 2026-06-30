import asyncio

import pytest

from agently.core import (
    TaskBoard,
    TaskBoardContext,
    TaskBoardGraph,
    TaskBoardRevision,
    TaskBoardValidator,
    build_task_board_evidence_view,
    coerce_task_board_planning_result,
    resolve_task_board_planning_policy,
    task_board_planning_output_schema,
)
from agently.core.orchestration.TaskBoard import (
    build_task_board_acceptance_index,
    build_task_board_focus_payload,
    build_task_board_handoff_projection,
    task_board_preflight_diagnostics,
)
from agently.core.application.AgentTask.Task import AgentTask
from agently.types.data import TaskBoardCardResult, TaskBoardPatch


def _revision():
    return TaskBoardRevision.create(
        board_id="demo",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "demo-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect facts."},
                    {"id": "final", "objective": "Write final answer.", "depends_on": ["collect"]},
                ],
            }
        ),
    )


def test_task_board_validation_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="Duplicate TaskBoardCard id"):
        TaskBoardValidator().validate(
            {
                "board_id": "duplicate",
                "revision_id": "rev-0",
                "graph": {
                    "graph_id": "duplicate-graph",
                    "cards": [
                        {"id": "a", "objective": "A"},
                        {"id": "a", "objective": "B"},
                    ],
                },
            }
        )


def test_task_board_validation_rejects_missing_dependency():
    with pytest.raises(ValueError, match="depends on missing card"):
        TaskBoardValidator().validate(
            {
                "board_id": "missing",
                "revision_id": "rev-0",
                "graph": {
                    "graph_id": "missing-graph",
                    "cards": [{"id": "a", "objective": "A", "depends_on": ["missing"]}],
                },
            }
        )


def test_task_board_validation_rejects_cycles():
    with pytest.raises(ValueError, match="root card|dependency cycle"):
        TaskBoardValidator().validate(
            {
                "board_id": "cycle",
                "revision_id": "rev-0",
                "graph": {
                    "graph_id": "cycle-graph",
                    "cards": [
                        {"id": "a", "objective": "A", "depends_on": ["b"]},
                        {"id": "b", "objective": "B", "depends_on": ["a"]},
                    ],
                },
            }
        )


def test_task_board_patch_base_revision_mismatch_fails_closed():
    revision = _revision()
    patch = TaskBoardPatch(
        base_revision="rev-stale",
        operations=(
            {
                "op": "record_card_result",
                "result": TaskBoardCardResult(card_id="collect", status="completed").to_dict(),
            },
        ),
    )

    with pytest.raises(ValueError, match="base_revision mismatch"):
        TaskBoardValidator().apply_patch(revision, patch)


def test_task_board_schedule_waits_for_completed_dependencies():
    revision = _revision()
    validator = TaskBoardValidator()

    first_schedule = validator.schedule(revision)
    assert first_schedule.runnable_card_ids == ("collect",)
    assert first_schedule.blocked_card_ids == ("final",)

    next_revision = validator.apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "facts",
                        "file_refs": [{"path": "facts.md", "sha256": "abc"}],
                    },
                },
            ),
        ),
    )
    second_schedule = validator.schedule(next_revision)
    assert next_revision.revision_id == "rev-1"
    assert second_schedule.runnable_card_ids == ("final",)
    assert second_schedule.completed_card_ids == ("collect",)
    assert next_revision.card_results["collect"].file_refs[0]["path"] == "facts.md"


def test_task_board_record_card_results_derives_terminal_board_status():
    revision = _revision()
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {"card_id": "collect", "status": "completed"},
                },
                {
                    "op": "record_card_result",
                    "result": {"card_id": "final", "status": "completed"},
                },
            ),
        ),
    )

    assert completed_revision.status == "completed"


def test_task_board_terminal_status_derivation_handles_all_optional_cards():
    revision = TaskBoardRevision.create(
        board_id="optional-only",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "optional-only-graph",
                "cards": [
                    {"id": "optional_source", "objective": "Try optional source.", "failure_policy": "optional"}
                ],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {"card_id": "optional_source", "status": "failed"},
                },
            ),
        ),
    )

    assert completed_revision.status == "completed"


def test_task_board_required_failed_dependency_blocks_downstream():
    revision = TaskBoardRevision.create(
        board_id="required-failure",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "required-failure-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect required facts."},
                    {"id": "final", "objective": "Write final answer.", "depends_on": ["collect"]},
                ],
            }
        ),
    )
    failed_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "failed",
                        "preview": "source unavailable",
                    },
                },
            ),
        ),
    )

    schedule = TaskBoardValidator().schedule(failed_revision)

    assert failed_revision.status == "failed"
    assert schedule.runnable_card_ids == ()
    assert schedule.blocked_card_ids == ("final",)
    assert not AgentTask._taskboard_revision_completed(failed_revision)


def test_task_board_optional_failed_dependency_unblocks_downstream_with_diagnostics():
    revision = TaskBoardRevision.create(
        board_id="optional-failure",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "optional-failure-graph",
                "cards": [
                    {
                        "id": "style_guidance",
                        "objective": "Read optional writing guidance.",
                        "failure_policy": "optional",
                    },
                    {
                        "id": "final",
                        "objective": "Write final answer.",
                        "depends_on": ["style_guidance"],
                    },
                ],
            }
        ),
    )
    failed_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "style_guidance",
                        "status": "failed",
                        "preview": "guidance lookup timed out",
                    },
                },
            ),
        ),
    )

    schedule = TaskBoardValidator().schedule(failed_revision)

    assert schedule.runnable_card_ids == ("final",)
    assert schedule.blocked_card_ids == ()
    assert schedule.diagnostics[0]["code"] == "taskboard.degraded_dependency_satisfied"
    assert schedule.diagnostics[0]["failure_policy"] == "optional"

    completed_revision = TaskBoardValidator().apply_patch(
        failed_revision,
        TaskBoardPatch(
            base_revision="rev-1",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final",
                        "status": "completed",
                        "preview": "final with missing guidance boundary",
                    },
                },
            ),
        ),
    )
    assert AgentTask._taskboard_revision_completed(completed_revision)


def test_task_board_final_candidate_prefers_structured_deliverable_over_review_leaf():
    revision = TaskBoardRevision.create(
        board_id="final-candidate",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "final-candidate-graph",
                "cards": [
                    {"id": "draft", "objective": "Write the final report."},
                    {"id": "review", "objective": "Review the final report.", "depends_on": ["draft"]},
                ],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": {
                            "answer": "Drafted the report.",
                            "artifact_markdown": "# Actual Report\n\nThis is the complete deliverable.",
                        },
                    },
                },
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "review",
                        "status": "completed",
                        "preview": {
                            "answer": "Review complete. All required sections are present.",
                        },
                    },
                },
            ),
        ),
    )

    task = AgentTask.__new__(AgentTask)

    assert (
        AgentTask._taskboard_candidate_final_result(task, completed_revision)
        == "# Actual Report\n\nThis is the complete deliverable."
    )


def test_task_board_final_candidate_keeps_leaf_answer_as_last_resort():
    revision = TaskBoardRevision.create(
        board_id="final-candidate-fallback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "final-candidate-fallback-graph",
                "cards": [
                    {"id": "draft", "objective": "Prepare notes."},
                    {"id": "final", "objective": "Answer from notes.", "depends_on": ["draft"]},
                ],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": {"answer": "Intermediate notes that are longer than the final."},
                    },
                },
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final",
                        "status": "completed",
                        "preview": {"answer": "Final answer."},
                    },
                },
            ),
        ),
    )

    task = AgentTask.__new__(AgentTask)

    assert AgentTask._taskboard_candidate_final_result(task, completed_revision) == "Final answer."


def test_task_board_evidence_view_uses_bounded_hot_preview_and_cold_refs():
    revision = _revision()
    cold_ref = {
        "path": "artifacts/collect.json",
        "sha256": "abc",
        "bytes": 1200,
        "preview": "ref preview must not enter hot path",
        "content": "full content must not enter hot path",
    }
    next_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "x" * 1200,
                        "artifact_refs": [cold_ref],
                        "file_refs": [cold_ref],
                        "diagnostics": [{"kind": "probe", "content": "diagnostic body"}],
                    },
                },
            ),
        ),
    )

    view = build_task_board_evidence_view(next_revision, preview_chars=100).to_dict()

    collect = view["cards"][0]
    assert collect["card_id"] == "collect"
    assert collect["preview"]["content"] == "x" * 100
    assert collect["preview"]["truncated"] is True
    assert collect["preview"]["original_chars"] == 1200
    assert collect["has_cold_refs"] is True
    assert collect["artifact_refs"][0]["path"] == "artifacts/collect.json"
    assert "preview" not in collect["artifact_refs"][0]
    assert "content" not in collect["artifact_refs"][0]
    assert "content" not in collect["diagnostics"]["items"][0]
    assert view["truncated"] is True
    assert view["status_counts"]["completed"] == 1
    assert view["status_counts"]["pending"] == 1


def test_task_board_evidence_view_preserves_action_result_ledger_items():
    from agently.core.application.AgentTask.EvidenceLedger import evidence_ledger_view, validate_evidence_use

    revision = _revision()
    action_ledger = AgentTask._evidence_ledger_from_execution_meta(
        {
            "status": "completed",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "market_quotes",
                        "status": "partial_success",
                        "action_call_id": "call-quotes",
                        "raw": {
                            "kwargs": {"symbols": ["NVDA", "AMD", "AVGO"]},
                            "data": {
                                "quotes": [
                                    {"symbol": "NVDA", "last": "194.97"},
                                    {"symbol": "AMD", "last": "539.49"},
                                ]
                            },
                        },
                    }
                ],
                "route_logs": {},
            },
        }
    )
    next_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": {"answer": "Collected quote facts."},
                        "metadata": {"evidence_ledger": action_ledger},
                    },
                },
            ),
        ),
    )

    view = build_task_board_evidence_view(next_revision).to_dict()
    board_ledger = evidence_ledger_view(view)
    action_items = [
        item
        for item in board_ledger["items"]
        if item.get("kind") == "agent_task.action.result"
        and item.get("action_id") == "market_quotes"
    ]

    assert len(action_items) == 1
    assert action_items[0]["body_state"] == "bounded"
    assert "NVDA" in str(action_items[0].get("body") or action_items[0].get("preview"))

    guard = validate_evidence_use(
        [
            {
                "claim": "TaskBoard quote facts were collected.",
                "evidence_ids": ["action_result_market_quotes"],
                "support_type": "content",
            }
        ],
        board_ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == [action_items[0]["id"]]


def test_task_board_evidence_view_rejects_unknown_card_scope():
    with pytest.raises(ValueError, match="unknown card ids"):
        build_task_board_evidence_view(_revision(), card_ids=["missing"])


def test_taskboard_agent_card_status_blocks_on_invalid_evidence_use():
    status = AgentTask._taskboard_card_status(
        {"status": "completed", "answer": "unsupported claim"},
        {"status": "completed"},
        evidence_use_guard={
            "schema_version": "evidence_use_guard/v1",
            "valid": False,
            "blocking_count": 1,
            "diagnostics": [{"code": "evidence_ledger.invalid_evidence_id", "blocking": True}],
        },
    )

    assert status == "blocked"


def test_taskboard_card_evidence_repair_rebinds_unique_action_result_labels():
    from agently.core.application.AgentTask.EvidenceLedger import collect_evidence_use, validate_evidence_use

    ledger = AgentTask._evidence_ledger_from_execution_meta(
        {
            "status": "completed",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "market_quotes",
                        "status": "partial_success",
                        "action_call_id": "call-quotes",
                        "raw": {
                            "data": {
                                "quotes": [
                                    {"symbol": "NVDA", "last": "196.8464"},
                                    {"symbol": "AMD", "last": "543.17"},
                                    {"symbol": "AVGO", "last": "377.26"},
                                ],
                                "history_status": "stooq_csv_failed_404",
                            },
                        },
                    }
                ],
                "route_logs": {},
            },
        }
    )
    card_output = {
        "status": "completed",
        "answer": "Quote snapshot gathered.",
        "evidence_use": [
            {
                "claim": "NVDA last sale price $196.8464.",
                "evidence_ids": ["Action result NVDA"],
                "support_type": "content",
            },
            {
                "claim": "One-year pricing history from Stooq CSV is not available due to HTTP 404.",
                "evidence_ids": ["Stooq CSV failure"],
                "support_type": "unavailability",
            },
        ],
    }
    guard = validate_evidence_use(collect_evidence_use(card_output), ledger)
    repaired_output, repaired_guard, diagnostic = AgentTask._repair_taskboard_card_evidence_use(
        card_output,
        guard,
        ledger,
    )

    action_id = next(
        item["id"]
        for item in ledger["items"]
        if item.get("kind") == "agent_task.action.result"
        and item.get("action_id") == "market_quotes"
    )
    assert diagnostic is not None
    assert repaired_guard["valid"] is True
    assert [item["evidence_ids"] for item in repaired_output["evidence_use"]] == [[action_id], [action_id]]


def test_taskboard_card_evidence_repair_prefers_direct_artifact_ref_alias():
    from agently.core.application.AgentTask.EvidenceLedger import (
        collect_evidence_use,
        evidence_ledger_view,
        validate_evidence_use,
    )

    artifact_id = "act_art_guidance"
    artifact_ref_id = "artifact_ref:taskboard:skill_guidance:attempt:1:act_art_guidance:artifact_ref:5"
    action_result_id = "agent_task_action_result:read_skill_guidance:call-guidance"
    ledger = evidence_ledger_view(
        {
            "evidence_items": [
                {
                    "id": artifact_ref_id,
                    "kind": "artifact_ref",
                    "status": "ok",
                    "body_state": "bounded",
                    "artifact_id": artifact_id,
                },
                {
                    "id": action_result_id,
                    "kind": "agent_task.action.result",
                    "status": "ok",
                    "body_state": "bounded",
                    "action_id": "read_skill_guidance",
                    "aliases": ["read_skill_guidance", artifact_id],
                },
            ]
        }
    )
    card_output = {
        "status": "completed",
        "evidence_use": [
            {
                "claim": "Skill guidance was read and is available as an artifact ref.",
                "evidence_ids": [artifact_id],
                "support_type": "content",
            }
        ],
    }
    guard = validate_evidence_use(collect_evidence_use(card_output), ledger)
    repaired_output, repaired_guard, diagnostic = AgentTask._repair_taskboard_card_evidence_use(
        card_output,
        guard,
        ledger,
    )

    assert diagnostic is not None
    assert repaired_guard["valid"] is True
    assert repaired_output["evidence_use"][0]["evidence_ids"] == [artifact_ref_id]


def test_taskboard_card_evidence_repair_uses_unique_workspace_readback_for_numeric_ids():
    from agently.core.application.AgentTask.EvidenceLedger import (
        collect_evidence_use,
        evidence_ledger_view,
        validate_evidence_use,
    )

    readback_id = "workspace_artifact_readback:card:working/taskboard/recent_news/final.md"
    ledger = evidence_ledger_view(
        {
            "evidence_items": [
                {
                    "id": readback_id,
                    "kind": "workspace_artifact.readback",
                    "status": "ok",
                    "body_state": "truncated",
                    "path": "working/taskboard/recent_news/final.md",
                }
            ]
        }
    )
    card_output = {
        "status": "completed",
        "evidence_use": [
            {
                "claim": "NVIDIA introduced Halos for Robotics.",
                "evidence_ids": ["0"],
                "support_type": "content",
            },
            {
                "claim": "AMD received a late-June analyst target upgrade.",
                "evidence_ids": ["1"],
                "support_type": "content",
            },
        ],
    }
    guard = validate_evidence_use(collect_evidence_use(card_output), ledger)
    repaired_output, repaired_guard, diagnostic = AgentTask._repair_taskboard_card_evidence_use(
        card_output,
        guard,
        ledger,
    )

    assert diagnostic is not None
    assert repaired_guard["valid"] is True
    assert [item["evidence_ids"] for item in repaired_output["evidence_use"]] == [[readback_id], [readback_id]]


def test_taskboard_card_evidence_repair_uses_unique_action_result_body_snippets():
    from agently.core.application.AgentTask.EvidenceLedger import (
        collect_evidence_use,
        evidence_ledger_view,
        validate_evidence_use,
    )

    market_quote_id = "agent_task_action_result:market_quotes:call-quotes"
    ledger = evidence_ledger_view(
        {
            "evidence_items": [
                {
                    "id": market_quote_id,
                    "kind": "agent_task.action.result",
                    "status": "ok",
                    "body_state": "bounded",
                    "action_id": "market_quotes",
                    "action_call_id": "call-quotes",
                    "body": (
                        '{"companies": [{"symbol": "NVDA", "last_sale_price": "$197.88", '
                        '"net_change": "+2.91", "percentage_change": "+1.49%", '
                        '"fallback_reason": "CSV request failed 404"}, '
                        '{"symbol": "AMD", "last_sale_price": "$557.48", '
                        '"net_change": "+17.99", "percentage_change": "+3.33%", '
                        '"fallback_reason": "CSV request failed 404"}]}'
                    ),
                }
            ]
        }
    )
    card_output = {
        "status": "completed",
        "evidence_use": [
            {
                "claim": "NVDA last price $197.88 with +1.49% change.",
                "evidence_ids": ["NVDA last_sale_price $197.88, net_change +2.91, percentage_change +1.49%"],
                "support_type": "content",
            },
            {
                "claim": "AMD quote used Nasdaq fallback after Stooq returned 404.",
                "evidence_ids": ["AMD fallback_reason: CSV request failed 404"],
                "support_type": "content",
            },
        ],
    }
    guard = validate_evidence_use(collect_evidence_use(card_output), ledger)
    repaired_output, repaired_guard, diagnostic = AgentTask._repair_taskboard_card_evidence_use(
        card_output,
        guard,
        ledger,
    )

    assert diagnostic is not None
    assert repaired_guard["valid"] is True
    assert [item["evidence_ids"] for item in repaired_output["evidence_use"]] == [[market_quote_id], [market_quote_id]]


def test_taskboard_card_evidence_repair_uses_unique_search_result_titles():
    from agently.core.application.AgentTask.EvidenceLedger import (
        collect_evidence_use,
        evidence_ledger_view,
        validate_evidence_use,
    )

    nvda_ref_id = "agent_task_action_result:web_search:call-nvda"
    amd_ref_id = "agent_task_action_result:web_search:call-amd"
    avgo_ref_id = "agent_task_action_result:web_search:call-avgo"
    ledger = evidence_ledger_view(
        {
            "evidence_items": [
                {
                    "id": nvda_ref_id,
                    "kind": "agent_task.action.result",
                    "status": "ok",
                    "body_state": "bounded",
                    "action_id": "web_search",
                    "action_call_id": "call-nvda",
                    "body": (
                        "[{\"title\": \"Prediction: This Will Be Nvidia's Stock Price at the End of 2026\", "
                        '"href": "https://finance.yahoo.com/markets/stocks/articles/prediction-nvidias-stock-price-end-065500179.html"}]'
                    ),
                },
                {
                    "id": amd_ref_id,
                    "kind": "agent_task.action.result",
                    "status": "ok",
                    "body_state": "bounded",
                    "action_id": "web_search",
                    "action_call_id": "call-amd",
                    "body": (
                        "[{\"title\": \"AMD Stock Is Crushing Nvidia's in 2026. Will That Continue?\", "
                        '"href": "https://finance.yahoo.com/markets/stocks/articles/amd-stock-crushing-nvidias-2026-180500062.html"}]'
                    ),
                },
                {
                    "id": avgo_ref_id,
                    "kind": "agent_task.action.result",
                    "status": "ok",
                    "body_state": "bounded",
                    "action_id": "web_search",
                    "action_call_id": "call-avgo",
                    "body": (
                        '[{"title": "Could This New Chip Be a Game Changer for Broadcom Stock?", '
                        '"href": "https://finance.yahoo.com/markets/stocks/articles/could-chip-game-changer-broadcom-135000175.html"}]'
                    ),
                },
            ]
        }
    )
    card_output = {
        "status": "completed",
        "evidence_use": [
            {
                "claim": "AMD stock rose sharply in 2026.",
                "evidence_ids": ["Search result: AMD Stock Is Crushing Nvidia's in 2026. Will That Continue?"],
                "support_type": "content",
            },
            {
                "claim": "NVIDIA has a 2026 price prediction article.",
                "evidence_ids": ["Search result: Prediction: This Will Be Nvidia's Stock Price at the End of 2026"],
                "support_type": "content",
            },
            {
                "claim": "Broadcom has a recent custom chip article.",
                "evidence_ids": ["Search result: Could This New Chip Be a Game Changer for Broadcom Stock?"],
                "support_type": "content",
            },
        ],
    }
    guard = validate_evidence_use(collect_evidence_use(card_output), ledger)
    repaired_output, repaired_guard, diagnostic = AgentTask._repair_taskboard_card_evidence_use(
        card_output,
        guard,
        ledger,
    )

    assert diagnostic is not None
    assert repaired_guard["valid"] is True
    assert [item["evidence_ids"] for item in repaired_output["evidence_use"]] == [
        [amd_ref_id],
        [nvda_ref_id],
        [avgo_ref_id],
    ]


def test_evidence_binding_repair_uses_deterministic_unique_ref_alias():
    repaired = AgentTask._deterministic_evidence_binding_repair(
        {
            "normalized_evidence_use": [
                {
                    "claim": "The report uses the final file.",
                    "evidence_ids": ["final.md"],
                    "support_type": "content",
                }
            ],
            "available_evidence_refs": [
                {
                    "id": "workspace_artifact.final_readback",
                    "path": "final.md",
                    "body_state": "bounded",
                    "status": "ok",
                }
            ],
            "diagnostics": [
                {
                    "code": "evidence_ledger.invalid_evidence_id",
                    "blocking": True,
                    "index": 0,
                    "claim": "The report uses the final file.",
                    "evidence_id": "final.md",
                    "support_type": "content",
                }
            ],
        }
    )

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "The report uses the final file.",
            "evidence_ids": ["workspace_artifact.final_readback"],
            "support_type": "content",
        }
    ]


def test_evidence_binding_repair_uses_unique_content_readback_for_invalid_id():
    repaired = AgentTask._deterministic_evidence_binding_repair(
        {
            "normalized_evidence_use": [
                {
                    "claim": "The heading correction in final.md is present.",
                    "evidence_ids": ["step_corrected_content_evidence"],
                    "support_type": "content",
                }
            ],
            "available_evidence_refs": [
                {
                    "id": "workspace_artifact_readback:agent_task.iteration.7.workspace_artifact:final.md",
                    "kind": "workspace_artifact.targeted_readback",
                    "path": "final.md",
                    "body_state": "bounded",
                    "status": "ok",
                },
                {
                    "id": "workspace_artifact_acceptance_locator:agent_task.iteration.7.workspace_artifact:final.md:heading",
                    "kind": "workspace_artifact.acceptance_locator",
                    "path": "final.md",
                    "body_state": "ref_only",
                    "status": "ok",
                },
            ],
            "diagnostics": [
                {
                    "code": "evidence_ledger.invalid_evidence_id",
                    "blocking": True,
                    "index": 0,
                    "claim": "The heading correction in final.md is present.",
                    "evidence_id": "step_corrected_content_evidence",
                    "support_type": "content",
                }
            ],
        }
    )

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "The heading correction in final.md is present.",
            "evidence_ids": ["workspace_artifact_readback:agent_task.iteration.7.workspace_artifact:final.md"],
            "support_type": "content",
        }
    ]


def test_evidence_binding_repair_replaces_ref_only_content_with_action_result_readback():
    action_result_id = "agent_task_action_result:market_quotes:call-quotes"
    repaired = AgentTask._deterministic_evidence_binding_repair(
        {
            "normalized_evidence_use": [
                {
                    "claim": "The quote lookup returned NVDA and AMD prices.",
                    "evidence_ids": ["call-quotes"],
                    "support_type": "content",
                }
            ],
            "available_evidence_refs": [
                {
                    "id": "action_evidence:market_quotes:call-quotes",
                    "kind": "action_evidence",
                    "action_id": "market_quotes",
                    "action_call_id": "call-quotes",
                    "body_state": "ref_only",
                    "status": "ok",
                    "aliases": ["call-quotes", "action_result_market_quotes"],
                },
                {
                    "id": action_result_id,
                    "kind": "agent_task.action.result",
                    "action_id": "market_quotes",
                    "action_call_id": "call-quotes",
                    "body_state": "bounded",
                    "status": "ok",
                    "aliases": ["call-quotes", "action_result_market_quotes"],
                    "body": "{\"quotes\": [{\"symbol\": \"NVDA\"}, {\"symbol\": \"AMD\"}]}",
                },
            ],
            "diagnostics": [
                {
                    "code": "evidence_ledger.ref_only_item_used_as_content_support",
                    "blocking": True,
                    "index": 0,
                    "claim": "The quote lookup returned NVDA and AMD prices.",
                    "evidence_id": "call-quotes",
                    "support_type": "content",
                }
            ],
        }
    )

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "The quote lookup returned NVDA and AMD prices.",
            "evidence_ids": [action_result_id],
            "support_type": "content",
        }
    ]


def test_evidence_binding_repair_maps_workspace_readback_section_label_to_latest_readback():
    latest_readback_id = (
        "workspace_artifact_readback:agent_task.taskboard.card.final-verification-repair-3.continue.workspace_artifact:final.md"
    )
    guard = {
        "normalized_evidence_use": [
            {
                "claim": "The final brief includes the non-investment-advice statement.",
                "evidence_ids": [
                    "workspace_artifact_readback:final-verification-repair-3.continue:final.md:non-investment-advice-section"
                ],
                "support_type": "content",
            }
        ],
        "available_evidence_refs": [
            {
                "id": "workspace_artifact_readback:agent_task.taskboard.card.final-verification-repair.workspace_artifact:final.md",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "truncated",
                "path": "final.md",
            },
            {
                "id": latest_readback_id,
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "truncated",
                "path": "final.md",
            },
        ],
        "diagnostics": [
            {
                "code": "evidence_ledger.invalid_evidence_id",
                "blocking": True,
                "index": 0,
                "claim": "The final brief includes the non-investment-advice statement.",
                "evidence_id": (
                    "workspace_artifact_readback:final-verification-repair-3.continue:final.md:non-investment-advice-section"
                ),
                "support_type": "content",
            }
        ],
    }
    ledger = {
        "items": [
            {
                "id": "workspace_artifact_readback:agent_task.taskboard.card.final-verification-repair.workspace_artifact:final.md",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "truncated",
                "path": "final.md",
                "body": "## Non-Investment-Advice Statement\nThis brief is for informational purposes only.",
            },
            {
                "id": latest_readback_id,
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "truncated",
                "path": "final.md",
                "body": "## Non-Investment-Advice Statement\nThis brief is for informational purposes only.",
            },
        ]
    }

    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == [
        {
            "claim_index": 0,
            "claim": "The final brief includes the non-investment-advice statement.",
            "evidence_ids": [latest_readback_id],
            "support_type": "content",
        }
    ]


def test_evidence_binding_repair_maps_artifact_locator_claim_to_targeted_readback():
    targeted_readback_id = (
        "workspace_artifact_targeted_readback:final.md:acceptance_locator_search:Non-Investment-Advice_Statement"
    )
    source_locator_id = (
        "workspace_artifact_acceptance_locator:agent_task.taskboard.card.final-verification-repair.workspace_artifact"
        ":final.md:non-investment-advice:Non-Investment-Advice_Statement"
    )
    guard = {
        "normalized_evidence_use": [
            {
                "claim": "Non-investment-advice statement",
                "evidence_ids": ["artifact_locator:16"],
                "support_type": "content",
            }
        ],
        "available_evidence_refs": [
            {
                "id": source_locator_id,
                "kind": "workspace_artifact.acceptance_locator",
                "status": "ok",
                "body_state": "ref_only",
                "path": "final.md",
                "criterion_id": "non-investment-advice",
                "heading": "Non-Investment-Advice Statement",
            },
            {
                "id": targeted_readback_id,
                "kind": "workspace_artifact.targeted_readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "final.md",
            },
        ],
        "diagnostics": [
            {
                "code": "evidence_ledger.invalid_evidence_id",
                "blocking": True,
                "index": 0,
                "claim": "Non-investment-advice statement",
                "evidence_id": "artifact_locator:16",
                "support_type": "content",
            }
        ],
    }
    ledger = {
        "items": [
            {
                "id": source_locator_id,
                "kind": "workspace_artifact.acceptance_locator",
                "status": "ok",
                "body_state": "ref_only",
                "path": "final.md",
                "criterion_id": "non-investment-advice",
                "heading": "Non-Investment-Advice Statement",
            },
            {
                "id": targeted_readback_id,
                "kind": "workspace_artifact.targeted_readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "final.md",
                "aliases": [source_locator_id, "Non-Investment-Advice Statement", "non-investment-advice-statement"],
                "body": "## Non-Investment-Advice Statement\nThis brief is for informational purposes only.",
            },
        ]
    }

    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == [
        {
            "claim_index": 0,
            "claim": "Non-investment-advice statement",
            "evidence_ids": [targeted_readback_id],
            "support_type": "content",
        }
    ]


def test_evidence_binding_repair_maps_file_row_label_to_matching_read_file_result():
    read_file_id = "agent_task_action_result:read_file:read-quotes-summary"
    guard = {
        "normalized_evidence_use": [
            {
                "claim": "AMD last sale price $565.70 as of Jun 30, 2026 11:23 AM ET",
                "evidence_ids": ["quotes_summary.md table row for AMD"],
                "support_type": "content",
            }
        ],
        "available_evidence_refs": [
            {
                "id": "agent_task_action_result:market_quotes:quotes",
                "kind": "agent_task.action.result",
                "action_id": "market_quotes",
                "status": "ok",
                "body_state": "bounded",
            },
            {
                "id": read_file_id,
                "kind": "agent_task.action.result",
                "action_id": "read_file",
                "path": "quotes_summary.md",
                "aliases": ["quotes_summary.md"],
                "status": "ok",
                "body_state": "bounded",
            },
        ],
        "diagnostics": [
            {
                "code": "evidence_ledger.invalid_evidence_id",
                "blocking": True,
                "index": 0,
                "claim": "AMD last sale price $565.70 as of Jun 30, 2026 11:23 AM ET",
                "evidence_id": "quotes_summary.md table row for AMD",
                "support_type": "content",
            }
        ],
    }
    ledger = {
        "items": [
            {
                "id": "agent_task_action_result:market_quotes:quotes",
                "kind": "agent_task.action.result",
                "action_id": "market_quotes",
                "status": "ok",
                "body_state": "bounded",
                "body": "{\"ticker\": \"AMD\", \"last_sale_price\": \"$565.70\"}",
            },
            {
                "id": read_file_id,
                "kind": "agent_task.action.result",
                "action_id": "read_file",
                "path": "quotes_summary.md",
                "aliases": ["quotes_summary.md"],
                "status": "ok",
                "body_state": "bounded",
                "body": "| AMD | $565.70 | +26.21 | +4.86% | Jun 30, 2026 11:23 AM ET |",
            },
        ]
    }

    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == [
        {
            "claim_index": 0,
            "claim": "AMD last sale price $565.70 as of Jun 30, 2026 11:23 AM ET",
            "evidence_ids": [read_file_id],
            "support_type": "content",
        }
    ]


def test_evidence_binding_repair_maps_file_section_unavailability_to_content_readback():
    read_file_id = "agent_task_action_result:read_file:read-quotes-summary"
    guard = {
        "normalized_evidence_use": [
            {
                "claim": "High, low, open, volume, and one-year historical data are not available (Stooq CSV 404 error)",
                "evidence_ids": ["quotes_summary.md 'Missing Data' section"],
                "support_type": "unavailability",
            }
        ],
        "available_evidence_refs": [
            {
                "id": read_file_id,
                "kind": "agent_task.action.result",
                "action_id": "read_file",
                "path": "quotes_summary.md",
                "aliases": ["quotes_summary.md"],
                "status": "ok",
                "body_state": "bounded",
            }
        ],
        "diagnostics": [
            {
                "code": "evidence_ledger.invalid_evidence_id",
                "blocking": True,
                "index": 0,
                "claim": "High, low, open, volume, and one-year historical data are not available (Stooq CSV 404 error)",
                "evidence_id": "quotes_summary.md 'Missing Data' section",
                "support_type": "unavailability",
            }
        ],
    }
    ledger = {
        "items": [
            {
                "id": read_file_id,
                "kind": "agent_task.action.result",
                "action_id": "read_file",
                "path": "quotes_summary.md",
                "aliases": ["quotes_summary.md"],
                "status": "ok",
                "body_state": "bounded",
                "body": (
                    "## Missing Data\n"
                    "High, low, open, volume, and one-year historical data are not available "
                    "because the Stooq CSV endpoint returned a 404 error."
                ),
            }
        ]
    }

    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == [
        {
            "claim_index": 0,
            "claim": "High, low, open, volume, and one-year historical data are not available (Stooq CSV 404 error)",
            "evidence_ids": [read_file_id],
            "support_type": "content",
        }
    ]


def test_evidence_binding_repair_refuses_cross_file_body_match_for_file_locator():
    # A file-locator reference names report.md, but the only ref whose body happens to
    # contain the claim text is a *different* file (data.csv). Body-text fallback must
    # not cross files: with no path/anchor agreement the locator stays unbound rather
    # than silently binding the claim to the wrong file's evidence.
    other_file_id = "agent_task_action_result:read_file:read-data-csv"
    guard = {
        "normalized_evidence_use": [
            {
                "claim": "project-a throughput is 42 units",
                "evidence_ids": ["report.md table row for project-a"],
                "support_type": "content",
            }
        ],
        "available_evidence_refs": [
            {
                "id": other_file_id,
                "kind": "agent_task.action.result",
                "action_id": "read_file",
                "path": "data.csv",
                "aliases": ["data.csv"],
                "status": "ok",
                "body_state": "bounded",
            }
        ],
        "diagnostics": [
            {
                "code": "evidence_ledger.invalid_evidence_id",
                "blocking": True,
                "index": 0,
                "claim": "project-a throughput is 42 units",
                "evidence_id": "report.md table row for project-a",
                "support_type": "content",
            }
        ],
    }
    ledger = {
        "items": [
            {
                "id": other_file_id,
                "kind": "agent_task.action.result",
                "action_id": "read_file",
                "path": "data.csv",
                "aliases": ["data.csv"],
                "status": "ok",
                "body_state": "bounded",
                # body coincidentally contains the claim text, but this is data.csv.
                "body": "project-a throughput is 42 units (raw export row)",
            }
        ]
    }

    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == []


def test_evidence_binding_repair_attempt_gate_only_limits_model_repair():
    write_result_id = "agent_task_action_result:write_file:act_call_write"
    guard = {
        "normalized_evidence_use": [
            {
                "claim": "final.md content was written through write_file.",
                "evidence_ids": ["agent_task_action_result:write_file:act_call_read"],
                "support_type": "content",
            }
        ],
        "available_evidence_refs": [
            {
                "id": "agent_task_action_result:read_file:act_call_read",
                "kind": "agent_task.action.result",
                "action_id": "read_file",
                "action_call_id": "act_call_read",
                "path": "final.md",
                "body_state": "truncated",
                "status": "ok",
                "aliases": ["read_file", "act_call_read", "final.md"],
            },
            {
                "id": write_result_id,
                "kind": "agent_task.action.result",
                "action_id": "write_file",
                "action_call_id": "act_call_write",
                "path": "final.md",
                "body_state": "bounded",
                "status": "ok",
                "aliases": ["write_file", "act_call_write", "final.md"],
            },
        ],
        "blocking_count": 1,
        "diagnostics": [
            {
                "code": "evidence_ledger.invalid_evidence_id",
                "blocking": True,
                "index": 0,
                "claim": "final.md content was written through write_file.",
                "evidence_id": "agent_task_action_result:write_file:act_call_read",
                "support_type": "content",
            }
        ],
    }
    task = AgentTask.__new__(AgentTask)
    task.diagnostics = {"evidence_binding_repair_attempt_count": 2}

    assert task._can_attempt_model_evidence_binding_repair() is False
    assert task._should_attempt_evidence_binding_repair(guard) is True
    assert AgentTask._deterministic_evidence_binding_repair(guard) == [
        {
            "claim_index": 0,
            "claim": "final.md content was written through write_file.",
            "evidence_ids": [write_result_id],
            "support_type": "content",
        }
    ]


def test_task_board_acceptance_index_derives_from_criteria_cards_verifier_and_locators():
    revision = TaskBoardRevision.create(
        board_id="acceptance-index",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "acceptance-index-graph",
                "cards": [
                    {
                        "id": "draft",
                        "objective": "Draft the report.",
                        "metadata": {"acceptance_criteria": ["Report includes source citations."]},
                    },
                    {
                        "id": "publish",
                        "objective": "Publish the final report.",
                        "depends_on": ["draft"],
                        "metadata": {"acceptance_criteria": ["Report includes source citations."]},
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision=revision.revision_id,
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": "Drafted with source citations.",
                        "metadata": {
                            "evidence_ledger": {
                                "items": [
                                    {
                                        "id": "locator:report:citation",
                                        "kind": "workspace_artifact.acceptance_locator",
                                        "status": "ok",
                                        "body_state": "ref_only",
                                        "claim": "Report includes source citations.",
                                        "criterion": "Report includes source citations.",
                                        "artifact_ref": {"path": "report.md"},
                                    }
                                ]
                            }
                        },
                    },
                },
            ),
        ),
    )
    evidence_view = build_task_board_evidence_view(revision).to_dict()

    index = build_task_board_acceptance_index(
        revision,
        success_criteria=["Report includes source citations."],
        verification={
            "criterion_checks": [
                {
                    "criterion": "Report includes source citations.",
                    "satisfied": True,
                    "evidence_ids": ["locator:report:citation"],
                    "locator_ids": ["locator:report:citation"],
                    "reason": "Verifier accepted the targeted readback.",
                }
            ]
        },
        evidence_view=evidence_view,
    )

    assert index["schema_version"] == "task_board_acceptance_index/v1"
    item = index["items"][0]
    assert item["criterion"] == "Report includes source citations."
    assert item["status"] == "satisfied"
    assert item["source"] == "verifier"
    assert item["linked_card_ids"] == ["draft", "publish"]
    assert item["linked_evidence_ids"] == ["locator:report:citation"]
    assert item["linked_locator_ids"] == ["locator:report:citation"]
    assert index["status_counts"]["satisfied"] == 1


def test_task_board_acceptance_index_does_not_enter_evidence_envelope():
    revision = _revision()
    evidence_view = build_task_board_evidence_view(revision).to_dict()
    index = build_task_board_acceptance_index(
        revision,
        success_criteria=["The final answer is accepted."],
        evidence_view=evidence_view,
    )

    assert index["schema_version"] == "task_board_acceptance_index/v1"
    assert "acceptance_index" not in evidence_view
    assert all(item.get("kind") != "task_board_acceptance_index" for item in evidence_view["evidence_items"])
    assert all(item.get("schema_version") != "task_board_acceptance_index/v1" for item in evidence_view["evidence_items"])


def test_task_board_handoff_projection_is_bounded_and_ref_only():
    revision = _revision()
    revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision=revision.revision_id,
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "x" * 2000,
                        "artifact_refs": [
                            {
                                "path": "artifacts/full.json",
                                "sha256": "abc",
                                "content": "must stay cold",
                                "preview": "also too hot",
                            }
                        ],
                    },
                },
            ),
        ),
    )
    evidence_view = build_task_board_evidence_view(revision).to_dict()
    acceptance_index = build_task_board_acceptance_index(
        revision,
        success_criteria=["The final answer is accepted."],
        evidence_view=evidence_view,
    )

    handoff = build_task_board_handoff_projection(
        task_id="handoff-task",
        execution_strategy="taskboard",
        effective_execution_strategy="taskboard",
        stage="tick",
        tick_index=1,
        revision=revision,
        schedule=TaskBoard(revision, handler=lambda _context: None).schedule(),
        evidence_view=evidence_view,
        acceptance_index=acceptance_index,
        checkpoint_refs=[{"id": "checkpoint-1", "content": "must stay cold"}],
    )

    assert handoff["schema_version"] == "task_board_handoff_projection/v1"
    assert handoff["task_id"] == "handoff-task"
    assert handoff["completed_card_ids"] == ["collect"]
    assert handoff["runnable_card_ids"] == ["final"]
    assert handoff["active_card_ids"] == ["final"]
    assert handoff["acceptance_index_summary"]["total_items"] == 1
    assert "content" not in handoff["artifact_refs"][0]
    assert "preview" not in handoff["artifact_refs"][0]
    assert "content" not in handoff["checkpoint_refs"][0]
    assert "x" * 200 not in str(handoff)


def test_taskboard_preflight_cards_require_mounted_capabilities():
    revision = TaskBoardRevision.create(
        board_id="preflight",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "preflight-graph",
                "cards": [
                    {
                        "id": "browser_health",
                        "objective": "Check browser readiness.",
                        "allowed_execution_shape": "actions",
                        "metadata": {
                            "preflight_kind": "resource_health",
                            "requires_capability_ids": ["browser"],
                        },
                    },
                    {
                        "id": "workspace_readback",
                        "objective": "Check existing artifact.",
                        "allowed_execution_shape": "readback",
                        "metadata": {
                            "preflight_kind": "readback",
                            "requires_workspace_refs": ["artifact:report"],
                        },
                    },
                ],
            }
        ),
    )

    missing = task_board_preflight_diagnostics(
        revision,
        mounted_capabilities=[{"id": "filesystem"}],
        workspace_refs=[{"id": "artifact:report"}],
    )
    assert missing == [
        {
            "code": "taskboard.preflight.unmounted_capability",
            "card_id": "browser_health",
            "missing_capability_ids": ["browser"],
            "status": "blocked",
        }
    ]

    available = task_board_preflight_diagnostics(
        revision,
        mounted_capabilities=[{"id": "browser"}, {"id": "filesystem"}],
        workspace_refs=[{"id": "artifact:report"}],
    )
    assert available == []


def test_taskboard_focus_payload_uses_acceptance_index_without_keyword_routing():
    revision = _revision()
    acceptance_index = build_task_board_acceptance_index(
        revision,
        success_criteria=["The final answer is accepted."],
    )
    payload = build_task_board_focus_payload(
        revision,
        acceptance_index=acceptance_index,
        schedule=TaskBoard(revision, handler=lambda _context: None).schedule(),
        preflight_diagnostics=[],
    )

    assert payload["schema_version"] == "task_board_focus_payload/v1"
    assert payload["selected_acceptance_item_ids"] == [acceptance_index["items"][0]["id"]]
    assert payload["runnable_card_ids"] == ["collect"]
    assert payload["blocked_card_ids"] == ["final"]
    assert payload["metadata"]["selection_policy"] == "status_dependency_capability_projection"
    assert "keyword" not in str(payload).lower()
    assert "regex" not in str(payload).lower()


def test_task_board_effort_policy_does_not_define_hard_budgets_or_action_options():
    policy = resolve_task_board_planning_policy("high")
    payload = policy.to_prompt_payload()
    forbidden_keys = {
        "allowed_actions",
        "action_options",
        "max_cards",
        "max_model_requests",
        "max_steps",
        "required_actions",
        "step_count",
    }

    def walk_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from walk_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk_keys(item)

    assert policy.effort_profile.name == "high"
    assert forbidden_keys.isdisjoint({str(key) for key in walk_keys(payload)})
    assert "not a target count" in policy.action_block_meaning
    assert "not an allowlist" in policy.action_block_meaning
    assert any("existing TaskBoard card results" in item for item in policy.evidence_reuse_guidance)
    assert any("Re-gather evidence only" in item for item in policy.evidence_reuse_guidance)
    assert any("localized defect" in item for item in policy.repair_orchestration_guidance)
    assert any("one terminal control card" in item for item in policy.control_card_guidance)
    assert any("allowed_execution_shape='readback'" in item for item in policy.control_card_guidance)
    assert any("synthesis, verification, and next-step decision" in item for item in payload["control_card_guidance"])


def test_task_board_planning_result_builds_valid_revision():
    result = coerce_task_board_planning_result(
        {
            "board_goal": "Prepare a support refund decision.",
            "cards": [
                {
                    "id": "collect",
                    "action_block": "Collect ticket and invoice evidence.",
                    "objective": "Gather customer and billing facts.",
                    "depends_on": [],
                    "evidence_to_use": ["ticket_id", "invoice_id"],
                    "done_when": "Ticket and invoice evidence are available.",
                    "failure_policy": "degradable",
                },
                {
                    "id": "decide",
                    "action_block": "Compare facts against refund policy.",
                    "objective": "Decide whether refund approval is justified.",
                    "depends_on": ["collect"],
                    "done_when": "Decision has evidence-backed reason.",
                    "allowed_execution_shape": "model",
                },
            ],
            "reflection_points": ["Check that billing status matches the ticket claim."],
            "completion_gate": "Final decision cites collected evidence.",
            "why_this_effort_shape": "Balanced evidence and decision separation.",
        },
        board_id="refund",
        effort="medium",
    )

    assert result.revision.board_id == "refund"
    assert result.revision.graph.graph_id == "refund.graph"
    assert [card.id for card in result.revision.graph.cards] == ["collect", "decide"]
    assert result.revision.graph.cards[0].input_refs == ("ticket_id", "invoice_id")
    assert result.revision.graph.cards[0].evidence_contract["action_block"] == "Collect ticket and invoice evidence."
    assert result.revision.graph.cards[0].failure_policy == "degradable"
    assert result.revision.graph.cards[1].depends_on == ("collect",)
    assert result.revision.graph.cards[1].failure_policy == "required"
    assert result.revision.graph.cards[1].allowed_execution_shape == "model"
    assert result.revision.metadata["completion_gate"] == "Final decision cites collected evidence."
    assert result.planning_policy.effort_profile.name == "medium"


def test_task_board_planning_canonicalizes_optional_card_id_hints():
    schema = task_board_planning_output_schema()
    assert schema["cards"][0]["id"][2] is False

    result = coerce_task_board_planning_result(
        {
            "board_goal": "Prepare a report.",
            "cards": [
                {
                    "id": "Collect Evidence",
                    "action_block": "Collect source evidence.",
                    "objective": "Gather evidence.",
                    "depends_on": [],
                    "done_when": "Evidence is available.",
                },
                {
                    "id": "Synthesize Report!",
                    "action_block": "Synthesize the report.",
                    "objective": "Write report.",
                    "depends_on": ["collect evidence"],
                    "done_when": "Report draft exists.",
                },
                {
                    "action_block": "Review the output.",
                    "objective": "Review output.",
                    "depends_on": ["Synthesize Report!"],
                    "done_when": "Review is complete.",
                },
            ],
            "completion_gate": "Report is reviewed.",
            "why_this_effort_shape": "Evidence, synthesis, and review are separated.",
        },
        board_id="canonical-card-ids",
    )

    cards = result.revision.graph.cards
    assert [card.id for card in cards] == [
        "collect_evidence",
        "synthesize_report",
        "card_3_review_output",
    ]
    assert cards[1].depends_on == ("collect_evidence",)
    assert cards[2].depends_on == ("synthesize_report",)
    assert cards[0].metadata["planning_id_hint"] == "Collect Evidence"
    assert any(
        diagnostic.get("code") == "taskboard.planning_card_id_canonicalized"
        for diagnostic in result.diagnostics
    )


def test_task_board_planning_preserves_scoped_retrieval_plan():
    result = coerce_task_board_planning_result(
        {
            "board_goal": "Answer from retained notes.",
            "cards": [
                {
                    "id": "collect",
                    "action_block": "Search retained Workspace records.",
                    "objective": "Find the Atlas renewal evidence without broad reads.",
                    "depends_on": [],
                    "done_when": "Atlas evidence snippet is available.",
                    "allowed_execution_shape": "actions",
                    "scoped_retrieval": {
                        "query_groups": [
                            {
                                "query": "Atlas",
                                "expected_role": "evidence_snippet",
                                "search_surface": "workspace_index",
                                "filters": {"collection": "retained-notes"},
                                "max_results": 3,
                            }
                        ]
                    },
                }
            ],
            "completion_gate": "Atlas evidence is found.",
            "why_this_effort_shape": "Single evidence card.",
        },
        board_id="scoped-retrieval-board",
    )

    card = result.revision.graph.card_by_id()["collect"]
    assert card.metadata["scoped_retrieval"]["query_groups"][0]["query"] == "Atlas"
    assert card.evidence_contract["scoped_retrieval"]["query_groups"][0]["filters"] == {
        "collection": "retained-notes",
    }


def test_task_board_planning_result_rejects_effort_as_hard_control_keys():
    with pytest.raises(ValueError, match="forbidden effort-control key: max_cards"):
        coerce_task_board_planning_result(
            {
                "board_goal": "Invalid board.",
                "max_cards": 2,
                "cards": [{"id": "only", "objective": "Run.", "depends_on": []}],
                "completion_gate": "Done.",
                "why_this_effort_shape": "Invalid hard control.",
            },
            board_id="invalid",
        )


def test_task_board_planning_fails_closed_on_ambiguous_dependency_hint():
    with pytest.raises(ValueError, match="ambiguous because multiple cards used that id hint"):
        coerce_task_board_planning_result(
            {
                "board_goal": "Invalid ambiguous dependency board.",
                "cards": [
                    {
                        "id": "dup",
                        "action_block": "First.",
                        "objective": "First card.",
                        "depends_on": [],
                        "done_when": "First done.",
                    },
                    {
                        "id": "dup",
                        "action_block": "Second.",
                        "objective": "Second card.",
                        "depends_on": [],
                        "done_when": "Second done.",
                    },
                    {
                        "id": "final",
                        "action_block": "Finalize.",
                        "objective": "Write final.",
                        "depends_on": ["dup"],
                        "done_when": "Final exists.",
                    },
                ],
                "completion_gate": "Done.",
                "why_this_effort_shape": "Invalid ambiguous dependency.",
            },
            board_id="ambiguous-dependency",
        )


def test_task_board_planning_result_still_fails_closed_on_invalid_dependencies():
    with pytest.raises(ValueError, match="depends on missing card"):
        coerce_task_board_planning_result(
            {
                "board_goal": "Invalid dependency board.",
                "cards": [
                    {
                        "id": "final",
                        "action_block": "Finalize.",
                        "objective": "Write final.",
                        "depends_on": ["missing"],
                        "done_when": "Final exists.",
                    }
                ],
                "completion_gate": "Done.",
                "why_this_effort_shape": "Invalid dependency.",
            },
            board_id="missing-dependency",
        )


@pytest.mark.asyncio
async def test_task_board_tick_runs_through_triggerflow_and_advances_revision():
    contexts: list[TaskBoardContext] = []

    async def handler(context: TaskBoardContext):
        contexts.append(context)
        assert context.model == "model-key"
        assert context.workspace == "workspace-ref"
        assert context.effort == "high"
        assert context.planning_policy is not None
        assert context.planning_policy.effort_profile.name == "high"
        return {
            "status": "completed",
            "preview": f"done:{ context.card.id }",
            "artifact_refs": [{"card_id": context.card.id, "kind": "text"}],
        }

    board = TaskBoard(
        _revision(),
        handler=handler,
        model="model-key",
        workspace="workspace-ref",
        effort="high",
    )

    first_tick = await board.async_run_tick(timeout=1)
    assert first_tick.previous_revision.revision_id == "rev-0"
    assert first_tick.revision.revision_id == "rev-1"
    assert first_tick.schedule.runnable_card_ids == ("collect",)
    assert first_tick.revision.card_results["collect"].preview == "done:collect"
    assert first_tick.triggerflow_snapshot["revision"]["revision_id"] == "rev-1"

    second_tick = await board.async_run_tick(timeout=1)
    assert second_tick.revision.revision_id == "rev-2"
    assert second_tick.schedule.runnable_card_ids == ("final",)
    assert contexts[-1].dependency_results["collect"].preview == "done:collect"


@pytest.mark.asyncio
async def test_task_board_explicit_simple_task_still_uses_task_board_process():
    async def handler(context: TaskBoardContext):
        return f"simple:{ context.card.objective }"

    board = TaskBoard(
        TaskBoardRevision.create(
            board_id="simple",
            graph={"graph_id": "simple-graph", "cards": [{"id": "answer", "objective": "Answer directly."}]},
        ),
        handler=handler,
    )
    tick = await board.async_run_tick(timeout=1)

    assert tick.schedule.runnable_card_ids == ("answer",)
    assert tick.revision.revision_id == "rev-1"
    assert tick.revision.card_results["answer"].preview == "simple:Answer directly."


@pytest.mark.asyncio
async def test_task_board_tick_fans_out_independent_cards_by_default():
    active = 0
    max_active = 0

    async def handler(context: TaskBoardContext):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"status": "completed", "preview": f"done:{ context.card.id }"}

    board = TaskBoard(
        TaskBoardRevision.create(
            board_id="default-fanout",
            graph={
                "graph_id": "default-fanout-graph",
                "cards": [
                    {"id": "a", "objective": "Run A."},
                    {"id": "b", "objective": "Run B."},
                    {"id": "c", "objective": "Run C."},
                ],
            },
        ),
        handler=handler,
    )

    tick = await board.async_run_tick(timeout=1)

    assert max_active == 3
    assert set(tick.revision.card_results) == {"a", "b", "c"}
    assert tick.revision.card_results["a"].preview == "done:a"
    assert tick.revision.card_results["b"].preview == "done:b"
    assert tick.revision.card_results["c"].preview == "done:c"
    assert tick.triggerflow_snapshot["runtime_topology"]["fanout"] == "signal_net_dynamic_overlay"
    assert tick.triggerflow_snapshot["runtime_topology"]["card_requested_event"].startswith("task_board.card.requested.")
    assert tick.triggerflow_snapshot["runtime_topology"]["card_run_binding_id"].startswith("task_board.tick.run_card.")


@pytest.mark.asyncio
async def test_task_board_tick_does_not_cancel_independent_card_on_required_failure():
    seen: list[str] = []

    async def handler(context: TaskBoardContext):
        seen.append(context.card.id)
        if context.card.id == "first":
            return {"status": "failed", "preview": "network timeout"}
        return {"status": "completed", "preview": "should not run in this tick"}

    board = TaskBoard(
        TaskBoardRevision.create(
            board_id="failure-stop",
            graph={
                "graph_id": "failure-stop-graph",
                "cards": [
                    {"id": "first", "objective": "Try fragile evidence."},
                    {"id": "second", "objective": "Independent follow-up."},
                ],
            },
        ),
        handler=handler,
    )

    tick = await board.async_run_tick(timeout=1, concurrency=1)

    assert set(seen) == {"first", "second"}
    assert tick.revision.revision_id == "rev-1"
    assert tick.revision.card_results["first"].status == "failed"
    assert tick.revision.card_results["second"].status == "completed"
    assert tick.card_results["first"].preview == "network timeout"


def test_task_board_tick_finalize_incomplete_snapshot_preserves_collected_results():
    revision = TaskBoardRevision.create(
        board_id="incomplete-snapshot",
        graph={
            "graph_id": "incomplete-snapshot-graph",
            "cards": [
                {"id": "a", "objective": "Run A."},
                {"id": "b", "objective": "Run B."},
            ],
        },
    )
    board = TaskBoard(revision, handler=lambda _context: {"status": "completed"})

    result = board._finalize_tick_snapshot(
        revision,
        {
            "status": "failed",
            "pending_tasks_cancelled": 1,
            "schedule": {
                "revision_id": "rev-0",
                "runnable_card_ids": ["a", "b"],
                "blocked_card_ids": [],
                "completed_card_ids": [],
                "diagnostics": [],
            },
            "collected_card_results": {
                "a": {
                    "card_id": "a",
                    "status": "completed",
                    "preview": "A finished before close failure.",
                }
            },
        },
    )

    assert result.revision.revision_id == "rev-1"
    assert result.revision.card_results["a"].status == "completed"
    assert result.revision.card_results["a"].preview == "A finished before close failure."
    assert result.revision.card_results["b"].status == "failed"
    assert result.revision.card_results["b"].metadata["interrupted"] is True
    assert result.revision.card_results["b"].diagnostics[0]["code"] == "taskboard.tick.card_interrupted"
    assert result.revision.diagnostics[-1]["code"] == "taskboard.tick.incomplete_snapshot"
    assert result.revision.diagnostics[-1]["interrupted_card_ids"] == ["b"]
    assert result.triggerflow_snapshot["status"] == "failed"


@pytest.mark.asyncio
async def test_task_board_tick_resume_retries_missing_cards_without_repeating_completed():
    previous_revision = TaskBoardRevision.create(
        board_id="resume-fanout",
        graph={
            "graph_id": "resume-fanout-graph",
            "cards": [
                {"id": "a", "objective": "Run A."},
                {"id": "b", "objective": "Run B."},
                {"id": "c", "objective": "Run C."},
            ],
        },
    )
    original_calls: list[str] = []
    restored_calls: list[str] = []
    second_running = asyncio.Event()
    release_original = asyncio.Event()

    async def original_handler(context: TaskBoardContext):
        original_calls.append(context.card.id)
        if len(original_calls) == 2:
            second_running.set()
            await release_original.wait()
        return {"status": "completed", "preview": f"original:{ context.card.id }"}

    async def restored_handler(context: TaskBoardContext):
        restored_calls.append(context.card.id)
        return {"status": "completed", "preview": f"restored:{ context.card.id }"}

    original_board = TaskBoard(previous_revision, handler=original_handler)
    original_tick = await original_board.async_start_tick(concurrency=1)
    await asyncio.wait_for(second_running.wait(), timeout=1)
    saved_state = original_tick.save()
    completed_before_save = original_calls[0]
    expected_restored_cards = {"a", "b", "c"} - {completed_before_save}

    release_original.set()
    await original_tick.async_close(timeout=1)

    restored_board = TaskBoard(previous_revision, handler=restored_handler)
    restored_tick = restored_board.create_tick_execution(concurrency=2)
    restored_tick.load(saved_state)
    restored_signal_net = restored_tick.save()["signal_net"]

    await restored_tick.async_resume_pending()
    result = await restored_tick.async_close(timeout=1)

    assert set(restored_calls) == expected_restored_cards
    assert completed_before_save not in restored_calls
    assert result.revision.revision_id == "rev-1"
    assert result.revision.card_results[completed_before_save].preview == f"original:{ completed_before_save }"
    for card_id in expected_restored_cards:
        assert result.revision.card_results[card_id].preview == f"restored:{ card_id }"
    assert any(
        attempt["trigger_event"] == result.triggerflow_snapshot["runtime_topology"]["card_requested_event"]
        and attempt["status"] == "interrupted"
        for attempt in restored_signal_net["signal_attempts"]
    )


@pytest.mark.asyncio
async def test_task_board_tick_continues_after_optional_failure():
    seen: list[str] = []

    async def handler(context: TaskBoardContext):
        seen.append(context.card.id)
        if context.card.id == "optional":
            return {"status": "failed", "preview": "optional lookup timeout"}
        return {"status": "completed", "preview": "independent work completed"}

    board = TaskBoard(
        TaskBoardRevision.create(
            board_id="optional-failure-continues",
            graph={
                "graph_id": "optional-failure-continues-graph",
                "cards": [
                    {"id": "optional", "objective": "Try optional evidence.", "failure_policy": "optional"},
                    {"id": "second", "objective": "Independent follow-up."},
                ],
            },
        ),
        handler=handler,
    )

    tick = await board.async_run_tick(timeout=1, concurrency=1)

    assert set(seen) == {"optional", "second"}
    assert tick.revision.card_results["optional"].status == "failed"
    assert tick.revision.card_results["second"].status == "completed"


@pytest.mark.asyncio
async def test_task_board_handler_cannot_mutate_frozen_revision_directly():
    def handler(context: TaskBoardContext):
        with pytest.raises(Exception):
            setattr(context.revision, "revision_id", "mutated")
        return {"status": "completed", "preview": "ok"}

    board = TaskBoard(
        TaskBoardRevision.create(
            board_id="immutable",
            graph={"graph_id": "immutable-graph", "cards": [{"id": "card", "objective": "Run."}]},
        ),
        handler=handler,
    )
    tick = await board.async_run_tick(timeout=1)

    assert tick.previous_revision.revision_id == "rev-0"
    assert tick.revision.revision_id == "rev-1"
