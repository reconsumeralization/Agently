import importlib.util
import os
import sys
from pathlib import Path

import pytest
from dotenv import find_dotenv, load_dotenv

from agently import Agently


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = ROOT / "examples" / "skills_executor" / "06_executable_education_course_pack.py"
pytestmark = pytest.mark.skip(
    reason=(
        "examples/skills_executor rewrite is owned separately; this benchmark "
        "still targets the retired staged skill.yaml executor."
    )
)


def _load_benchmark_module():
    spec = importlib.util.spec_from_file_location("executable_education_course_pack", BENCHMARK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dependency_installer_skill_contract_runs_through_skills_executor(tmp_path):
    benchmark = _load_benchmark_module()
    Agently.settings.set("skills.registry.root", str(tmp_path / "registry"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    execution = benchmark._run_dependency_install_skill(tmp_path / "dependency-skill")

    assert execution.status == "success"
    assert execution.skill_logs[0]["kind"] == "action"
    assert execution.skill_logs[0]["action_id"] == "ensure_python_packages"
    assert execution.action_logs[0]["status"] == "success"
    output = execution.output["ensure_artifact_writer_dependencies"]
    assert output["available_after"]["docx"] is True
    assert output["available_after"]["openpyxl"] is True
    assert output["available_after"]["pptx"] is True
    assert output["available_after"]["reportlab"] is True
    assert output["available_after"]["pypdf"] is True


@pytest.mark.skills_real_execution
def test_deepseek_executable_education_course_pack(tmp_path):
    if os.getenv("AGENTLY_RUN_SKILLS_REAL_EXECUTION") != "1":
        pytest.skip("Set AGENTLY_RUN_SKILLS_REAL_EXECUTION=1 to run real execution Skills benchmark.")

    load_dotenv(find_dotenv(usecwd=True))
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("Missing DEEPSEEK_API_KEY.")

    benchmark = _load_benchmark_module()
    report = benchmark.run_real_execution_benchmark(
        output_root=tmp_path / "outputs",
        registry_root=tmp_path / "registry",
    )

    assert report["dependency_execution"]["status"] == "success"
    assert report["artifact_validation"]["passed"] is True
    assert report["model_judge"]["passes"] is True
    assert report["real_execution_result"] == "pass"
