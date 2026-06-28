from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace


def _load_block_carrier_runner() -> ModuleType:
    runner_path = (
        Path(__file__).resolve().parents[1]
        / "spec"
        / "experiments"
        / "agent-task-block-carrier"
        / "round-001"
        / "run_round.py"
    )
    spec = importlib.util.spec_from_file_location("agent_task_block_carrier_round_runner", runner_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_real_samples_runner() -> ModuleType:
    runner_path = (
        Path(__file__).resolve().parents[1]
        / "spec"
        / "experiments"
        / "flat-react-taskboard-real-samples"
        / "flat_react_taskboard_real_samples.py"
    )
    spec = importlib.util.spec_from_file_location("flat_react_taskboard_real_samples", runner_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_block_carrier_summary_records_graph_facts_without_runner_verdict(tmp_path):
    runner = _load_block_carrier_runner()
    run_dir = tmp_path / "round"
    record_dir = run_dir / "records" / "flat"
    record_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "routes": ["flat", "taskboard"],
                "cases": [{"case_id": "stock_risk_outlook"}],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "REPORT.md").write_text(
        "\n".join(
            [
                "# Round",
                "",
                "## Route Boundary Analysis",
                "",
                "AgentExecution implication: legacy runner should not keep this judgment.",
                "",
                "| Case | Complexity | Boundary result | Reason |",
                "| --- | --- | --- | --- |",
                "| `stock_risk_outlook` | `medium` | `comparison_missing` | legacy analysis |",
                "",
                "## Manual Review Queue",
                "",
                "Manual review remains factual.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (record_dir / "stock_risk_outlook.json").write_text(
        json.dumps(
            {
                "route": "flat",
                "case_id": "stock_risk_outlook",
                "judge": {"accepted": True, "quality_level": "strong"},
                "artifact_gate": {"passed": True, "failures": []},
                "framework_terminal_gate": {"status": "completed"},
                "metrics": {
                    "input_chars": 1200,
                    "output_chars": 800,
                    "model_requests": 2,
                    "judge_requests": 1,
                    "tool_calls": 1,
                    "elapsed_seconds": 3.5,
                },
                "framework_meta": {
                    "execution_meta": {
                        "block_carrier": {
                            "work_unit": {"id": "iter-1:flat-step", "origin": "flat_step"},
                            "work_unit_result": {
                                "id": "iter-1:flat-step",
                                "status": "completed",
                                "carrier_meta": {
                                    "execution_block_graph": {
                                        "graph_id": "carrier-graph",
                                        "execution_blocks": [{"id": "agent-step", "kind": "agent_step"}],
                                    },
                                    "block_result": {"semantic_outputs": {"step": "ok"}},
                                },
                            },
                            "output_policy": {
                                "body_transport": "structured_control",
                                "control_format": "json",
                                "body_uses_output": True,
                            },
                        },
                        "blocks": {
                            "execution_block_graph": {
                                "graph_id": "carrier-graph",
                                "execution_blocks": [{"id": "agent-step", "kind": "agent_step"}],
                            },
                            "evidence": {
                                "execution_block_results": [
                                    {"id": "agent-step", "kind": "agent_step", "status": "completed"}
                                ],
                                "plan_block_results": [
                                    {"id": "agent-step", "kind": "agent_step", "status": "completed"}
                                ],
                            },
                            "snapshot": {"status": "completed"},
                        },
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "behavior.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "framework_stream_event",
                        "route": "flat",
                        "case_id": "stock_risk_outlook",
                        "stream_event_index": 10,
                        "stream_item": {
                            "path": "agent_task.action.started",
                            "meta": {
                                "stream_kind": "action_observation",
                                "phase": "started",
                                "action_id": "grep_workspace",
                                "work_unit_id": "iter-1:flat-step",
                            },
                            "value": {
                                "action_id": "grep_workspace",
                                "status": "started",
                                "work_unit_id": "iter-1:flat-step",
                                "origin": "flat_step",
                                "input_summary": {"query": "risk"},
                                "projection_source": "execution_meta.action_logs",
                                "posthoc_projection": True,
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "framework_stream_event",
                        "route": "flat",
                        "case_id": "stock_risk_outlook",
                        "stream_event_index": 11,
                        "stream_item": {
                            "path": "agent_task.action.completed",
                            "meta": {
                                "stream_kind": "action_observation",
                                "phase": "completed",
                                "action_id": "grep_workspace",
                                "work_unit_id": "iter-1:flat-step",
                            },
                            "value": {
                                "action_id": "grep_workspace",
                                "status": "success",
                                "work_unit_id": "iter-1:flat-step",
                                "origin": "flat_step",
                                "output_summary": {"path": "notes.md", "content": "risk evidence"},
                                "source_refs": [{"value": "notes.md"}],
                                "projection_source": "execution_meta.action_logs",
                                "posthoc_projection": True,
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )

    summary = runner.write_block_carrier_summary(run_dir, stage="diagnostic_pair")
    runner.write_block_carrier_summary(run_dir, stage="diagnostic_pair")

    assert summary["schema_version"] == "agent_task_block_carrier_round_facts/v1"
    assert "verdict" not in summary
    assert "failure_classification" not in summary
    assert summary["runner_responsibility"]["runner_classifies_failures"] is False
    assert summary["selected_scope"] == ["flat/stock_risk_outlook"]
    assert len(summary["records"]) == 1
    assert [item["record_present"] for item in summary["full_effect_records"]].count(True) == 1
    facts = summary["records"][0]
    assert facts["observed_origins"] == ["flat_step"]
    carrier = facts["carriers"][0]
    action_observations = facts["action_observations"]
    assert action_observations["event_count"] == 2
    assert action_observations["counts_by_phase"] == {"completed": 1, "started": 1}
    assert action_observations["action_ids"] == ["grep_workspace"]
    assert action_observations["runner_judges_action_usefulness"] is False
    assert action_observations["samples"][0]["input_summary"] == {"query": "risk"}
    assert action_observations["samples"][1]["source_ref_count"] == 1
    assert carrier["block_graph"]["present"] is True
    assert carrier["block_graph"]["graph_id"] == "carrier-graph"
    assert carrier["block_graph"]["execution_block_kinds"] == ["agent_step"]
    assert carrier["block_evidence"]["present"] is True
    assert carrier["block_evidence"]["execution_block_result_kinds"] == ["agent_step"]
    report = (run_dir / "REPORT.md").read_text(encoding="utf-8")
    assert "BlockCarrier Facts" in report
    assert "Action Observation Facts" in report
    assert "grep_workspace" in report
    assert report.count("## BlockCarrier Facts") == 1
    assert "agent_step" in report
    assert "## Route Boundary Analysis" not in report
    assert "AgentExecution implication" not in report
    assert "Manual review remains factual." in report


def test_block_carrier_runner_qwen_model_override(monkeypatch):
    runner = _load_block_carrier_runner()
    monkeypatch.delenv("QWEN_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("REAL_SAMPLE_PROVIDER_STREAM_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("REAL_SAMPLE_RESPONSE_MATERIALIZATION_IDLE_TIMEOUT", raising=False)

    args = SimpleNamespace(
        provider="qwen",
        model="qwen3.6-flash-2026-04-16",
        lmcc_syllabus_path=None,
        lmcc_syllabus_url=None,
        network_proxy=None,
        model_timeout_seconds=None,
        flat_max_iterations=None,
        taskboard_route_timeout_seconds=None,
        taskboard_tick_timeout_seconds=None,
        taskboard_card_timeout_seconds=None,
        taskboard_max_ticks=None,
        taskboard_card_max_steps=None,
        provider_stream_idle_timeout_seconds=12.5,
        response_materialization_idle_timeout_seconds=7.5,
    )
    runner._apply_legacy_cli_environment(args, SimpleNamespace(NETWORK_PROXY_ENV="TEST_NETWORK_PROXY"))

    assert os.environ["QWEN_MODEL"] == "qwen3.6-flash-2026-04-16"
    assert "DEEPSEEK_DEFAULT_MODEL" not in os.environ
    assert os.environ["REAL_SAMPLE_PROVIDER_STREAM_IDLE_TIMEOUT"] == "12.5"
    assert os.environ["REAL_SAMPLE_RESPONSE_MATERIALIZATION_IDLE_TIMEOUT"] == "7.5"


def test_block_carrier_summary_records_runtime_observability_without_verdict(tmp_path):
    runner = _load_block_carrier_runner()
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "routes": ["flat"],
                "cases": [{"case_id": "lmcc_mock_exam"}],
                "provider": {
                    "provider": "qwen",
                    "model": "qwen3.5-plus-2026-04-20",
                    "runtime_observability": {
                        "provider_stream_idle_timeout_seconds": 30.0,
                        "response_materialization_idle_timeout_seconds": 45.0,
                        "framework_no_progress_seconds": 90.0,
                        "strategy_input": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    summary = runner.write_block_carrier_summary(run_dir, stage="diagnostic_pair")

    observability = summary["runtime_observability"]
    assert observability["provider_stream_idle_timeout_seconds"] == 30.0
    assert observability["response_materialization_idle_timeout_seconds"] == 45.0
    assert observability["framework_no_progress_seconds"] == 90.0
    assert observability["strategy_input"] is False
    assert observability["runner_classifies_failures"] is False
    assert "quality" in observability["notes"]


def test_block_carrier_runner_allows_framework_no_progress_as_liveness(monkeypatch):
    runner = _load_block_carrier_runner()
    monkeypatch.setenv("REAL_SAMPLE_FRAMEWORK_NO_PROGRESS_SECONDS", "90")

    args = SimpleNamespace(
        flat_max_iterations=None,
        taskboard_route_timeout_seconds=None,
        taskboard_tick_timeout_seconds=None,
        taskboard_card_timeout_seconds=None,
        taskboard_max_ticks=None,
        taskboard_card_max_steps=None,
    )

    runner._validate_no_strategy_caps(args)


def test_framework_route_metrics_count_model_request_events_from_stream_summary():
    runner = _load_real_samples_runner()
    metrics = runner.Metrics(route="flat", case_id="lmcc_mock_exam")
    stream_summary = {
        "path_counts": {
            "agent_task.iteration.1.progress.plan": 1,
            "agent_task.iteration.1.execution.runtime.progress.action_planning.started": 5,
            "agent_task.iteration.1.progress.verify": 1,
            "agent_task.iteration.1.execution.runtime.progress.action_execution.started": 5,
            "agent_task.iteration.1.progress.completed": 1,
        },
        "model_usage_records": [
            runner.model_usage_record(
                source="framework_stream:agent_task.iteration.1.execution.$status",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                input_chars=100,
                output_chars=50,
            )
        ],
    }

    runner.apply_framework_model_request_metrics(metrics, stream_summary)

    assert metrics.framework_model_request_events == 7
    assert metrics.model_requests == 7
    assert metrics.model_request_count_source == "framework_stream_model_stage_starts"
    as_dict = metrics.to_dict()
    assert as_dict["framework_model_request_events"] == 7
    assert as_dict["model_request_count_source"] == "framework_stream_model_stage_starts"
    assert as_dict["model_usage"]["records"] == 1
    assert as_dict["model_usage"]["provider_usage_records"] == 1
    assert as_dict["model_usage"]["provider"]["prompt_tokens"] == 10
    assert as_dict["model_usage"]["provider"]["completion_tokens"] == 5
    assert as_dict["model_usage"]["provider"]["total_tokens"] == 15
    assert as_dict["model_usage"]["estimated_lengths"]["input_chars"] == 100
    assert as_dict["model_usage"]["estimated_lengths"]["output_chars"] == 50


def test_model_usage_summary_keeps_unknown_tokens_and_estimated_lengths():
    runner = _load_real_samples_runner()
    metrics = runner.Metrics(route="flat", case_id="lmcc_mock_exam")
    metrics.record_model_usage(
        runner.model_usage_record(
            source="direct_model_meta",
            usage=None,
            input_chars=321,
            output_chars=123,
            stage="judge",
            judge=True,
        )
    )

    usage = metrics.to_dict()["model_usage"]

    assert usage["records"] == 1
    assert usage["provider_usage_records"] == 0
    assert usage["missing_provider_usage_records"] == 1
    assert usage["provider"]["prompt_tokens"] is None
    assert usage["provider"]["completion_tokens"] is None
    assert usage["provider"]["total_tokens"] is None
    assert usage["estimated_lengths"]["input_chars"] == 321
    assert usage["estimated_lengths"]["output_chars"] == 123
    assert "NaN" in usage["display_policy"]


def test_real_sample_runner_defaults_do_not_apply_iteration_caps(monkeypatch):
    runner = _load_real_samples_runner()
    monkeypatch.delenv("REAL_SAMPLE_FRAMEWORK_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("REAL_SAMPLE_FLAT_MAX_ITERATIONS", raising=False)

    assert runner._framework_route_max_iterations("flat") is None
    assert runner._framework_route_max_iterations("taskboard") is None
    assert runner._framework_iteration_cap_role(max_iterations=None, taskboard_max_ticks=None) == "none"

    monkeypatch.setenv("REAL_SAMPLE_FRAMEWORK_MAX_ITERATIONS", "7")
    assert runner._framework_route_max_iterations("taskboard") == 7
    assert (
        runner._framework_iteration_cap_role(max_iterations=7, taskboard_max_ticks=None)
        == "explicit_max_iterations"
    )
    assert (
        runner._framework_iteration_cap_role(max_iterations=None, taskboard_max_ticks=4)
        == "explicit_taskboard_max_ticks"
    )


def test_block_carrier_runner_rejects_strategy_caps(monkeypatch):
    runner = _load_block_carrier_runner()
    for env_name in runner.DISALLOWED_STRATEGY_CAP_ENVS:
        monkeypatch.delenv(env_name, raising=False)

    args = SimpleNamespace(
        flat_max_iterations=1,
        taskboard_route_timeout_seconds=None,
        taskboard_tick_timeout_seconds=None,
        taskboard_card_timeout_seconds=None,
        taskboard_max_ticks=None,
        taskboard_card_max_steps=None,
    )

    try:
        runner._validate_no_strategy_caps(args)
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError("hard route-shaping CLI caps must be rejected")

    assert "--flat-max-iterations" in message
    assert "strategy inputs" in message

    args.flat_max_iterations = None
    monkeypatch.setenv("REAL_SAMPLE_FRAMEWORK_MAX_ITERATIONS", "3")

    try:
        runner._validate_no_strategy_caps(args)
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError("hard route-shaping env caps must be rejected")

    assert "REAL_SAMPLE_FRAMEWORK_MAX_ITERATIONS" in message
    assert "watchdog" in message


def test_block_carrier_runner_model_pool_uses_requested_provider_for_bare_models():
    runner = _load_block_carrier_runner()
    args = SimpleNamespace(
        provider="qwen",
        model=None,
        model_pool=[
            "qwen3.6-plus-2026-04-02",
            "qwen3.5-plus-2026-04-20,qwen3.7-plus",
            "glm-5.1",
            "deepseek-v4-flash",
        ],
    )

    candidates = runner._model_pool_candidates(args)

    assert candidates == [
        {"provider": "qwen", "model": "qwen3.6-plus-2026-04-02"},
        {"provider": "qwen", "model": "qwen3.5-plus-2026-04-20"},
        {"provider": "qwen", "model": "qwen3.7-plus"},
        {"provider": "qwen", "model": "glm-5.1"},
        {"provider": "qwen", "model": "deepseek-v4-flash"},
    ]


def test_block_carrier_runner_defaults_bare_models_to_deepseek(monkeypatch):
    runner = _load_block_carrier_runner()
    args = SimpleNamespace(provider=None, model="deepseek-v4-flash", model_pool=None)

    assert runner._model_pool_candidates(args) == [
        {"provider": "deepseek", "model": "deepseek-v4-flash"}
    ]
    assert runner._infer_provider_for_model("custom-hosted-model", None) == "deepseek"

    legacy = SimpleNamespace(NETWORK_PROXY_ENV="NETWORK_PROXY")
    env_names = [
        "REAL_SAMPLE_EXPERIMENT_PROVIDER",
        "DEEPSEEK_DEFAULT_MODEL",
        "QWEN_MODEL",
        "OLLAMA_MODEL",
    ]
    for env_name in env_names:
        monkeypatch.delenv(env_name, raising=False)
    runner._apply_legacy_cli_environment(
        SimpleNamespace(
            provider=None,
            model="custom-hosted-model",
            lmcc_syllabus_path=None,
            lmcc_syllabus_url=None,
            network_proxy=None,
            model_timeout_seconds=None,
            provider_stream_idle_timeout_seconds=None,
            response_materialization_idle_timeout_seconds=None,
            flat_max_iterations=None,
            taskboard_route_timeout_seconds=None,
            taskboard_tick_timeout_seconds=None,
            taskboard_card_timeout_seconds=None,
            taskboard_max_ticks=None,
            taskboard_card_max_steps=None,
        ),
        legacy,
    )

    assert os.environ["DEEPSEEK_DEFAULT_MODEL"] == "custom-hosted-model"
    assert "QWEN_MODEL" not in os.environ


def test_block_carrier_runner_records_model_pool_resource_exhaustion_fact(tmp_path):
    runner = _load_block_carrier_runner()
    run_dir = tmp_path / "run"
    record_dir = run_dir / "records" / "flat"
    record_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"provider": {"provider": "qwen", "model": "qwen3.6-plus-2026-04-02"}}),
        encoding="utf-8",
    )
    (record_dir / "case.json").write_text(
        json.dumps(
            {
                "judge": {
                    "accepted": False,
                    "quality_level": "fail",
                    "reason": "Status Code: 403; AllocationQuota.FreeTierOnly: quota exhausted.",
                }
            }
        ),
        encoding="utf-8",
    )

    evidence = runner._run_dir_resource_exhaustion_evidence(run_dir)

    assert evidence
    assert evidence[0]["path"].endswith("case.json.judge.reason")
    assert "AllocationQuota.FreeTierOnly" in evidence[0]["preview"]


def test_block_carrier_runner_omits_provider_request_payload_from_attempt_preview():
    runner = _load_block_carrier_runner()
    preview = runner._compact_attempt_text(
        "Status Code: 403\n"
        "Detail: AllocationQuota.FreeTierOnly\n"
        "Request Data: {'messages': [{'role': 'user', 'content': 'large private prompt'}]}"
    )

    assert "AllocationQuota.FreeTierOnly" in preview
    assert "[provider payload omitted]" in preview
    assert "large private prompt" not in preview
    assert "messages" not in preview


def test_real_sample_runner_omits_provider_request_payload_from_stream_preview():
    runner = _load_real_samples_runner()
    item = SimpleNamespace(
        path="error",
        value={
            "message": (
                "Status Code: 400\n"
                "Detail: invalid input\n"
                "Request Data: {'messages': [{'role': 'user', 'content': 'large private prompt'}]}"
            )
        },
        delta=None,
        is_complete=True,
        event_type="done",
        source="agent_execution",
        route="agent_task",
        stage_id="",
        task_id="task-1",
        action_id="",
        graph_id="",
        wildcard_path="error",
        indexes=[],
        meta={},
    )

    serialized = runner.serialize_framework_stream_item(item)

    assert serialized["value"]["redacted"] is True
    assert "[provider payload omitted]" in serialized["value"]["preview"]
    assert "large private prompt" not in serialized["value"]["preview"]
    assert "messages" not in serialized["value"]["preview"]


def test_hidden_source_audit_records_fact_without_overriding_judge(tmp_path):
    runner = _load_block_carrier_runner()
    legacy = runner._load_legacy_runner()
    run_dir = tmp_path / "run"
    artifact = run_dir / "artifacts" / "flat" / "lmcc_mock_exam" / "final.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Final\n\nCites an official page, but not the hidden audit URL.\n", encoding="utf-8")

    record = {
        "judge": {
            "accepted": True,
            "quality_level": "excellent",
            "dimension_scores": {"factual_grounding": "excellent", "evidence_discipline": "excellent"},
            "material_gaps": [],
            "reason": "Semantic judge accepted the result.",
        }
    }
    case = SimpleNamespace(
        hidden_audit_refs={
            "official_syllabus_url": "https://lmcc.ccf.org.cn/101/1010/10261.html",
            "hidden_from_execution_prompt": True,
        }
    )
    final_outputs = {
        "deliverable_refs": [
            {
                "path": "artifacts/flat/lmcc_mock_exam/final.md",
                "role": "final_deliverable",
            }
        ]
    }

    updated = legacy.apply_hidden_source_audit(record, case, run_dir, final_outputs)

    assert updated["source_audit"]["schema_version"] == "hidden_source_audit/v2"
    assert updated["source_audit"]["quality_gate"] is False
    assert updated["source_audit"]["required_source_refs"] == [
        "https://lmcc.ccf.org.cn/101/1010/10261.html"
    ]
    assert updated["source_audit"]["exact_source_refs"] == []
    assert updated["source_audit"]["missing_exact_refs"] == [
        "https://lmcc.ccf.org.cn/101/1010/10261.html"
    ]
    assert updated["judge"] == record["judge"]


def test_lmcc_framework_criteria_preserve_source_ref_contract():
    runner = _load_block_carrier_runner()
    legacy = runner._load_legacy_runner()

    cases = legacy.experiment_cases("https://github.com/microsoft/SkillOpt")
    cases = legacy.apply_lmcc_official_site_hint(cases, "lmcc.ccf.org.cn")
    lmcc = cases["lmcc_mock_exam"]
    criteria = legacy.framework_success_criteria(lmcc)

    assert lmcc.output_contract["source_refs_required"] is True
    assert "official source references" in lmcc.output_contract["sections"]
    assert any("source URLs" in item and "evidence refs" in item for item in criteria)
