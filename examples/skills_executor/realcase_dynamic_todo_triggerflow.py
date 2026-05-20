"""Diagnostic realcase: can DeepSeek + Agently-Skills write a DAG executor?

Run:
    PYTHONPATH=. python examples/skills_executor/realcase_dynamic_todo_triggerflow.py

Environment:
    DEEPSEEK_API_KEY must be available in the shell or a .env file.
    Optional:
      DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
      DEEPSEEK_DEFAULT_MODEL=deepseek-chat
      AGENTLY_SKILLS_REPO=../Agently-Skills

Expected key output from a real DeepSeek run after this diagnostic passes:
    [SKILLS_EXECUTOR_PLAN]
    selected_skills=['agently-playbook', 'agently-request', 'agently-triggerflow']
    [MODEL_GENERATED_ARTIFACT]
    generated_file=.example_runtime/skills_executor/dynamic_todo/generated_dynamic_todo_executor.py
    [EVALUATION]
    diagnostic_result=pass

This example intentionally does not hard-code TriggerFlow API details in the
model prompt and does not run repair rounds. It tests whether the new Skills
Executor plus Agently-Skills guidance is enough for DeepSeek to generate a
usable dynamic TriggerFlow Todo-DAG executor.

Host responsibilities:
    1. install Agently-Skills into the framework-side Skills Executor;
    2. expose relevant skills with agent.use_skills(..., mode="model_decision");
    3. ask DeepSeek for a complete runnable Python module;
    4. evaluate the generated module against runtime/API diagnostics;
    5. run the generated module only after safety and API checks pass.

Flow:
    Agently-Skills
      |
      v
    Skills Executor -> SkillCards + bounded SKILL.md guidance
      |
      v
    DeepSeek generates Todo DAG + TriggerFlow executor source
      |
      v
    host evaluator reports pass/fail without prompt repair
"""

from __future__ import annotations

import ast
import os
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.types.data import SkillExecutionPlan


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / ".example_runtime" / "skills_executor" / "dynamic_todo"
GENERATED_PATH = RUNTIME_ROOT / "generated_dynamic_todo_executor.py"
SKILL_IDS = ["agently-playbook", "agently-request", "agently-triggerflow"]
PROBLEM = (
    "Use Agently and TriggerFlow to solve a complex planning problem: generate "
    "a Todo List with non-trivial task dependencies for the first production "
    "release of a multi-tenant customer-support AI assistant, then dynamically "
    "plan and execute the Todo List asynchronously with the best dependency-aware "
    "schedule."
)


@dataclass
class Evaluation:
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None

    @property
    def passed(self) -> bool:
        return all(self.checks.values()) and not self.errors and self.returncode == 0


def configure_deepseek():
    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Missing DEEPSEEK_API_KEY. Put it in your shell or .env before running this example.")
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat"),
            "model_type": "chat",
            "auth": api_key,
            "request_options": {"temperature": 0.0},
        },
    )
    Agently.set_settings("debug", False)


def install_agently_skills():
    configured = os.getenv("AGENTLY_SKILLS_REPO")
    skills_repo = Path(configured).expanduser().resolve() if configured else (ROOT / ".." / "Agently-Skills").resolve()
    if not skills_repo.exists():
        raise RuntimeError(f"Agently-Skills repo is required for this realcase example: { skills_repo }")

    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    Agently.settings.set("skills.registry.root", str(RUNTIME_ROOT / "registry"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)
    Agently.settings.set("skills.prompt.max_guidance_chars_per_skill", 9000)

    for skill_id in SKILL_IDS:
        Agently.skills.install(skills_repo / "skills" / skill_id, trust_level="local", update=True)


def create_generator_agent():
    agent = Agently.create_agent()
    agent.set_agent_prompt(
        "system",
        (
            "You are an Agently implementation agent. Use the disclosed skills "
            "as the source of framework guidance. Generate runnable code rather "
            "than advice."
        ),
    )
    return agent


def generate_artifact() -> tuple[SkillExecutionPlan, dict[str, Any]]:
    agent = create_generator_agent()
    skills_plan = agent.resolve_skill_plan(PROBLEM, skills=SKILL_IDS, mode="model_decision", scope="request")
    agent.use_skills(SKILL_IDS, mode="model_decision", scope="request")

    artifact = (
        agent.input({"problem": PROBLEM})
        .instruct(
            [
                "Produce a complete runnable Python module for the problem.",
                "The module should include the model-generated Todo List and the dynamic executor.",
                "The module should finish quickly on a local machine.",
                "Return source code only in executor_code, without markdown fences.",
            ]
        )
        .output(
            {
                "skill_usage": (str, "How the disclosed skills shaped the generated implementation.", True),
                "todo_plan_summary": (str, "Short summary of the Todo List and dependency strategy.", True),
                "executor_code": (str, "Complete runnable Python source code.", True),
            }
        )
        .start(max_retries=1, raise_ensure_failure=False)
    )
    return skills_plan, artifact


def strip_code_fence(code: str) -> str:
    text = code.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return textwrap.dedent(text).strip() + "\n"


def write_generated_source(code: str) -> Path:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    GENERATED_PATH.write_text(code, encoding="utf-8")
    return GENERATED_PATH


def evaluate_source(code: str) -> Evaluation:
    evaluation = Evaluation()
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        evaluation.errors.append(f"syntax_error: { error }")
        return evaluation

    imported_modules: list[str] = []
    imports_agently_triggerflow = False
    imports_wrong_triggerflow_package = False
    calls_when = False
    calls_multi_dependency_join = False
    calls_emit_nowait = False
    creates_execution = False
    closes_execution = False
    uses_triggerflow_decorator = False
    suspicious_import = False
    unsafe_call = False

    denied_import_roots = {"os", "sys", "subprocess", "pathlib", "shutil", "socket", "requests", "httpx"}
    denied_call_names = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint"}
    denied_attr_names = {"system", "popen", "Popen", "call", "check_call", "check_output", "unlink", "rmtree"}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "when":
                    uses_triggerflow_decorator = True
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.append(alias.name)
                root = alias.name.split(".")[0]
                suspicious_import = suspicious_import or root in denied_import_roots
                imports_wrong_triggerflow_package = imports_wrong_triggerflow_package or root in {
                    "triggerflow",
                    "agently_triggerflow",
                }
        if isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")
            root = (node.module or "").split(".")[0]
            suspicious_import = suspicious_import or root in denied_import_roots
            imports_wrong_triggerflow_package = imports_wrong_triggerflow_package or root in {
                "triggerflow",
                "agently_triggerflow",
            }
            if node.module == "agently":
                imports_agently_triggerflow = any(alias.name == "TriggerFlow" for alias in node.names)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in denied_call_names:
                unsafe_call = True
            if isinstance(node.func, ast.Attribute):
                unsafe_call = unsafe_call or node.func.attr in denied_attr_names
                calls_when = calls_when or node.func.attr == "when"
                calls_emit_nowait = calls_emit_nowait or node.func.attr in {"emit_nowait", "async_emit_nowait"}
                creates_execution = creates_execution or node.func.attr in {"create_execution", "async_start_execution"}
                closes_execution = closes_execution or node.func.attr in {"close", "async_close"}
                if node.func.attr == "when":
                    calls_multi_dependency_join = calls_multi_dependency_join or any(
                        keyword.arg == "mode"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "and"
                        for keyword in node.keywords
                    )

    evaluation.checks = {
        "safe_to_run": not suspicious_import and not unsafe_call,
        "imports_agently_triggerflow": imports_agently_triggerflow,
        "does_not_import_wrong_triggerflow_package": not imports_wrong_triggerflow_package,
        "uses_triggerflow_when": calls_when,
        "uses_multi_dependency_join": calls_multi_dependency_join,
        "uses_nowait_emit": calls_emit_nowait,
        "creates_execution": creates_execution,
        "closes_execution": closes_execution,
        "does_not_use_when_as_decorator": not uses_triggerflow_decorator,
    }
    if suspicious_import:
        evaluation.errors.append(f"suspicious_imports: { imported_modules }")
    if unsafe_call:
        evaluation.errors.append("unsafe_call_detected")
    return evaluation


def run_generated_module(path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        ["python", str(path)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def generate_evaluate_and_maybe_run() -> tuple[SkillExecutionPlan, dict[str, Any], Path, Evaluation]:
    skills_plan, artifact = generate_artifact()
    code = strip_code_fence(str(artifact.get("executor_code") or ""))
    generated_path = write_generated_source(code)
    evaluation = evaluate_source(code)

    if evaluation.errors or not evaluation.checks.get("safe_to_run", False):
        return skills_plan, artifact, generated_path, evaluation

    try:
        completed = run_generated_module(generated_path)
    except subprocess.TimeoutExpired as error:
        evaluation.errors.append("runtime_timeout")
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        evaluation.stdout = stdout.decode() if isinstance(stdout, bytes) else str(stdout)
        evaluation.stderr = stderr.decode() if isinstance(stderr, bytes) else str(stderr)
        return skills_plan, artifact, generated_path, evaluation
    evaluation.stdout = completed.stdout
    evaluation.stderr = completed.stderr
    evaluation.returncode = completed.returncode
    if completed.returncode != 0:
        evaluation.errors.append("runtime_nonzero_exit")
    return skills_plan, artifact, generated_path, evaluation


def print_evaluation(evaluation: Evaluation):
    print("[EVALUATION]")
    print(f"diagnostic_result={ 'pass' if evaluation.passed else 'fail' }")
    for name, passed in evaluation.checks.items():
        print(f"{ name }={ passed }")
    if evaluation.errors:
        print(f"errors={ evaluation.errors }")
    if evaluation.stdout.strip():
        print("\n[MODEL_GENERATED_EXECUTOR_STDOUT]")
        print(evaluation.stdout.strip())
    if evaluation.stderr.strip():
        print("\n[MODEL_GENERATED_EXECUTOR_STDERR]")
        print(evaluation.stderr.strip())


def main():
    configure_deepseek()
    install_agently_skills()
    skills_plan, artifact, generated_path, evaluation = generate_evaluate_and_maybe_run()

    print("[SKILLS_EXECUTOR_PLAN]")
    print(f"selected_skills={ [item.get('skill_id') for item in skills_plan.get('selected_skills', [])] }")
    print(f"plan_status={ skills_plan.get('status') }")

    print("\n[MODEL_GENERATED_ARTIFACT]")
    print(f"generated_file={ generated_path.relative_to(ROOT) }")
    print(f"skill_usage={ artifact.get('skill_usage') }")
    print(f"todo_plan_summary={ artifact.get('todo_plan_summary') }")
    print_evaluation(evaluation)

    if not evaluation.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
