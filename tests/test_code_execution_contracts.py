from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
    DockerExecutionResourceProvider,
)

from agently.types.data import (
    CodeExecutionBundle,
    CodeExecutionFile,
    CodeExecutionProviderCapability,
    CodeExecutionRequest,
    CodeExecutionResult,
    CodeExecutionStep,
    CodeExecutionToolchainRequirement,
    resolve_code_execution_workspace_uri,
)


def test_provider_capability_and_result_contracts_are_provider_neutral() -> None:
    capability: CodeExecutionProviderCapability = {
        "languages": ["python"],
        "isolation": {
            "process_contained": True,
            "host_filesystem_restricted": True,
            "privilege_escalation_blocked": True,
            "syscalls_restricted": True,
            "mechanism": "container",
        },
        "workspace_access_modes": ["snapshot"],
        "safety_class": "isolated",
    }
    result: CodeExecutionResult = {
        "ok": True,
        "status": "success",
        "returncode": 0,
        "stdout": "ok\n",
        "stderr": "",
        "outputs": [],
        "log_refs": [],
    }

    assert capability["languages"] == ["python"]
    assert result["status"] == "success"


def test_docker_container_command_enforces_declared_isolation_axes() -> None:
    resource = DockerExecutionResource(
        docker_binary="docker",
        timeout=10,
        runtime_profile={"network_mode": "disabled"},
    )

    args = resource._container_base_args(
        profile={"network_mode": "disabled"},
    )

    assert ["--cap-drop", "ALL"] == args[
        args.index("--cap-drop") : args.index("--cap-drop") + 2
    ]
    assert ["--security-opt", "no-new-privileges"] == args[
        args.index("--security-opt") : args.index("--security-opt") + 2
    ]
    assert "--pids-limit" in args
    assert ["--network", "none"] == args[
        args.index("--network") : args.index("--network") + 2
    ]


@pytest.mark.parametrize(
    ("default_args", "weakened_axis"),
    [
        (["--security-opt", "seccomp=unconfined"], "syscalls_restricted"),
        (["--pid", "host"], "process_contained"),
        (["--userns", "host"], "privilege_escalation_blocked"),
        (["--volume", "/:/host"], "host_filesystem_restricted"),
        (["--privileged=true"], "privilege_escalation_blocked"),
        (["-v=/:/host"], "host_filesystem_restricted"),
    ],
)
def test_docker_probe_detects_split_form_isolation_weakening(
    default_args: list[str],
    weakened_axis: str,
) -> None:
    capabilities = DockerExecutionResourceProvider._isolation_capabilities(
        default_args
    )

    assert capabilities[weakened_axis] is False


def test_workspace_uri_resolves_only_through_provider_supplied_logical_roots() -> None:
    roots = {
        "source": "/workspace/source",
        "build": "/workspace/build",
        "output": "/workspace/output",
        "logs": "/workspace/logs",
    }

    assert (
        resolve_code_execution_workspace_uri(
            "workspace://build/go-cache",
            roots=roots,
        )
        == "/workspace/build/go-cache"
    )
    assert resolve_code_execution_workspace_uri("ordinary", roots=roots) == "ordinary"
    with pytest.raises(ValueError, match="role|root"):
        resolve_code_execution_workspace_uri("workspace://private/value", roots=roots)
    with pytest.raises(ValueError, match="relative|path"):
        resolve_code_execution_workspace_uri("workspace://build/../escape", roots=roots)


def _bundle() -> CodeExecutionBundle:
    return CodeExecutionBundle.create(
        language="python",
        files=[
            CodeExecutionFile(
                path="main.py",
                content=b"print('ok')\n",
                role="source",
            )
        ],
        entrypoint="main.py",
        build_steps=[],
        run_step=CodeExecutionStep(
            argv=("python", "main.py"),
            cwd="source",
            role="run",
        ),
        expected_outputs=["output/result.json"],
        toolchains=[
            CodeExecutionToolchainRequirement(
                tool="python",
                minimum_version="3.10",
            )
        ],
        provenance={"kind": "inline"},
    )


def test_code_execution_bundle_is_immutable_and_has_stable_digest() -> None:
    first = _bundle()
    second = _bundle()

    assert first.files[0].sha256.startswith("sha256:")
    assert first.bundle_digest == second.bundle_digest
    assert first.bundle_digest.startswith("sha256:")
    assert first.expected_outputs == ("output/result.json",)
    with pytest.raises(FrozenInstanceError):
        first.language = "nodejs"  # type: ignore[misc]


@pytest.mark.parametrize(
    "path",
    ["../escape.py", "/absolute.py", ".agently/private.py", "source/../../escape"],
)
def test_code_execution_file_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        CodeExecutionFile(path=path, content=b"", role="source")


def test_bundle_rejects_duplicate_case_colliding_files() -> None:
    with pytest.raises(ValueError, match="duplicate|collision"):
        CodeExecutionBundle.create(
            language="python",
            files=[
                CodeExecutionFile(path="Main.py", content=b"", role="source"),
                CodeExecutionFile(path="main.py", content=b"", role="source"),
            ],
            entrypoint="main.py",
            build_steps=[],
            run_step=CodeExecutionStep(argv=("python", "main.py"), role="run"),
        )


def test_code_execution_step_rejects_shell_string_and_unbounded_arguments() -> None:
    with pytest.raises((TypeError, ValueError), match="argv"):
        CodeExecutionStep(argv="python main.py", role="run")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="argv"):
        CodeExecutionStep(argv=tuple("x" for _ in range(257)), role="run")


def test_code_execution_request_bounds_expected_output_declarations() -> None:
    with pytest.raises(ValueError, match="expected_outputs|expected outputs"):
        CodeExecutionRequest.create(
            language="python",
            source_code="print('ok')\n",
            expected_outputs=[f"output/result-{index}.txt" for index in range(129)],
        )

    with pytest.raises(ValueError, match="output/"):
        CodeExecutionRequest.create(
            language="python",
            source_code="print('ok')\n",
            expected_outputs=["source/result.txt"],
        )

    with pytest.raises(ValueError, match="output/"):
        CodeExecutionRequest.create(
            language="python",
            source_code="print('ok')\n",
            expected_outputs=["Output/result.txt"],
        )

    with pytest.raises(ValueError, match="size limit"):
        CodeExecutionRequest.create(
            language="python",
            source_code="print('ok')\n",
            expected_outputs=[f"output/{'x' * 4096}.txt"],
        )


def test_code_execution_bundle_rejects_outputs_outside_output_root() -> None:
    bundle = _bundle()

    with pytest.raises(ValueError, match="output/"):
        CodeExecutionBundle.create(
            language=bundle.language,
            files=bundle.files,
            entrypoint=bundle.entrypoint,
            build_steps=bundle.build_steps,
            run_step=bundle.run_step,
            expected_outputs=["logs/result.json"],
            toolchains=bundle.toolchains,
        )
