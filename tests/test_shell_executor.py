from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agently.builtins.actions import Cmd
from agently.builtins.plugins.ExecutionResourceProvider.Shell import BashExecutor, PowerShellExecutor, Shell


BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="Native Bash is not installed")


@pytest.mark.asyncio
@requires_bash
async def test_bash_interprets_script_without_using_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("New executor must not delegate to legacy Cmd")

    monkeypatch.setattr(Cmd, "run", forbidden)
    folder = tmp_path / "directory with spaces"
    folder.mkdir()
    result = await BashExecutor(binary=str(BASH)).run(
        "value='中文 value'\nprintf '%s\\n' \"$value\" | tee 'output file.txt'\n"
        "printf 'next\\n' >> 'output file.txt'\nprintf 'diagnostic' >&2\nexit 7",
        workdir=folder,
        timeout=5,
    )
    assert result.returncode == 7
    assert result.stdout == "中文 value\n"
    assert result.stderr == "diagnostic"
    assert (folder / "output file.txt").read_text() == "中文 value\nnext\n"


@pytest.mark.asyncio
async def test_cmd_delegates_exact_argv_to_new_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = BashExecutor.run_argv
    calls = []

    async def observed(self, argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return await actual(self, argv, **kwargs)

    monkeypatch.setattr(BashExecutor, "run_argv", observed)
    args = ["", "a b", "|", ">", "$(touch unexpected)", "$HOME", "a'\"b"]
    argv = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *args]
    cmd = Cmd(allowed_cmd_prefixes=[sys.executable], allowed_workdir_roots=[tmp_path])
    result = await cmd.run(argv)
    assert result["ok"] is True
    assert json.loads(result["stdout"]) == args
    assert len(calls) == 1
    assert calls[0][0] == argv
    assert calls[0][1]["workdir"] == tmp_path
    assert not (tmp_path / "unexpected").exists()


@pytest.mark.asyncio
async def test_argv_does_not_require_bash(tmp_path: Path) -> None:
    result = await BashExecutor(binary=str(tmp_path / "missing-bash")).run_argv(
        [sys.executable, "-c", "print('argv')"], workdir=tmp_path, timeout=5
    )
    assert result.stdout == "argv\n"


@pytest.mark.asyncio
@requires_bash
async def test_bash_calls_keep_environment_and_cwd_private(tmp_path: Path) -> None:
    executor = BashExecutor(binary=str(BASH))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    async def run(root: Path, value: str):
        return await executor.run(
            "printf '%s\\n' \"$SHELL_TEST_VALUE\"; pwd",
            workdir=root,
            timeout=5,
            env={**os.environ, "SHELL_TEST_VALUE": value},
        )

    a, b = await asyncio.gather(run(first, "a"), run(second, "b"))
    assert a.stdout.splitlines() == ["a", str(first.resolve())]
    assert b.stdout.splitlines() == ["b", str(second.resolve())]


@pytest.mark.asyncio
@requires_bash
async def test_bash_timeout_keeps_partial_output(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        await BashExecutor(binary=str(BASH)).run(
            "printf 'started'; printf 'diagnostic' >&2; sleep 10", workdir=tmp_path, timeout=0.1
        )
    assert caught.value.stdout == b"started"
    assert caught.value.stderr == b"diagnostic"


@pytest.mark.asyncio
@requires_bash
async def test_bash_keeps_native_pipeline_exit_semantics(tmp_path: Path) -> None:
    executor = BashExecutor(binary=str(BASH))
    normal = await executor.run("false | true", workdir=tmp_path, timeout=5)
    explicit = await executor.run("set -o pipefail; false | true", workdir=tmp_path, timeout=5)
    assert normal.returncode == 0
    assert explicit.returncode != 0


@pytest.mark.asyncio
@pytest.mark.parametrize("executor", [BashExecutor, PowerShellExecutor])
@pytest.mark.parametrize("command", ["", " \n", "a\0b", "a" * 65537])
async def test_invalid_source_rejected_before_spawn(tmp_path: Path, monkeypatch, executor, command) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid source must not launch an interpreter")

    monkeypatch.setattr(Shell, "run_argv", forbidden)
    with pytest.raises(ValueError):
        await executor().run(command, workdir=tmp_path, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("executor", [BashExecutor, PowerShellExecutor])
async def test_missing_interpreter_never_falls_back(tmp_path: Path, executor) -> None:
    with pytest.raises(FileNotFoundError):
        await executor(binary=str(tmp_path / "not-installed")).run("echo test", workdir=tmp_path, timeout=5)


@pytest.mark.asyncio
async def test_powershell_transports_exact_source_not_bash_syntax(tmp_path: Path, monkeypatch) -> None:
    calls = []

    async def capture(self, argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(Shell, "run_argv", capture)
    command = '$x = "中文"\nWrite-Output $x\nGet-Content "C:\\space dir\\a.txt"'
    await PowerShellExecutor(binary="pwsh").run(command, workdir=tmp_path, timeout=5)
    argv, options = calls[0]
    assert argv[:-1] == ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand"]
    wire_source = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "$agently_ast = [scriptblock]::Create('" + command.replace("'", "''") + "').Ast" in wire_source
    assert '"`nif (-not `$?) { exit 1 }`n"' in wire_source
    assert "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)" in wire_source
    assert options["encoding"] == "utf-8"
    assert options["workdir"] == tmp_path
    # This is transport evidence only, not Windows execution or isolation proof.


@pytest.mark.asyncio
async def test_explicit_encoding_does_not_change_legacy_argv_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("locale.getpreferredencoding", lambda _=False: "latin-1")
    argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0xc3, 0xa9]))"]
    legacy = await Shell().run_argv(argv, workdir=tmp_path, timeout=5)
    utf8 = await Shell().run_argv(argv, workdir=tmp_path, timeout=5, encoding="utf-8")
    assert legacy.stdout == "\u00c3\u00a9"
    assert utf8.stdout == "\u00e9"


@pytest.mark.asyncio
async def test_powershell_transport_quotes_are_literal_and_env_unmodified(tmp_path: Path, monkeypatch) -> None:
    calls = []

    async def capture(self, argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(Shell, "run_argv", capture)
    command = "param($x = 'it''s 中文')\nWrite-Output $x; # '); throw 'not executed by wrapper"
    env = {"TASK_TEST_KEY": "unchanged"}
    await PowerShellExecutor(binary="pwsh").run(command, workdir=tmp_path, timeout=5, env=env)
    argv, options = calls[0]
    wire = base64.b64decode(argv[-1]).decode("utf-16-le")
    literal = wire.split("$agently_ast = [scriptblock]::Create('", 1)[1].split("').Ast", 1)[0]
    assert literal.replace("''", "'") == command
    assert options["env"] == env == {"TASK_TEST_KEY": "unchanged"}


@pytest.mark.asyncio
async def test_powershell_limits_final_wire_size_including_escaping(tmp_path: Path, monkeypatch) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("Oversized transport must fail before spawn")

    monkeypatch.setattr(Shell, "run_argv", forbidden)
    with pytest.raises(ValueError, match="UTF-16LE"):
        await PowerShellExecutor().run("'" * 5000, workdir=tmp_path, timeout=5)


@pytest.mark.asyncio
async def test_invalid_output_encoding_rejected_before_spawn(tmp_path: Path, monkeypatch) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid encoding must not execute the command")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(LookupError):
        await Shell().run_argv(["not-executed"], workdir=tmp_path, timeout=5, encoding="invalid-codec")


@pytest.mark.asyncio
async def test_powershell_encoded_size_limit_checked_before_spawn(tmp_path: Path, monkeypatch) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("Oversized encoded source must not launch PowerShell")

    monkeypatch.setattr(Shell, "run_argv", forbidden)
    with pytest.raises(ValueError, match="UTF-16LE"):
        await PowerShellExecutor().run("a" * 8193, workdir=tmp_path, timeout=5)
