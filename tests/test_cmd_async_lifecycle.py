from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agently import Agently
from agently.builtins.actions import Cmd


def runner(root: Path, **kwargs) -> Cmd:
    return Cmd(allowed_cmd_prefixes=[sys.executable], allowed_workdir_roots=[root], **kwargs)


@pytest.mark.asyncio
async def test_cmd_wait_does_not_block_event_loop(tmp_path: Path) -> None:
    tick = asyncio.Event()
    timer = asyncio.get_running_loop().call_later(0.02, tick.set)
    try:
        result = await runner(tmp_path).run([sys.executable, "-c", "import time; time.sleep(0.15)"])
        assert tick.is_set(), "The loop must run while the command is still pending"
        assert result["returncode"] == 0
    finally:
        timer.cancel()


@pytest.mark.asyncio
async def test_cmd_preserves_nonzero_text_newlines_and_empty_argument(tmp_path: Path) -> None:
    result = await runner(tmp_path).run(
        [
            sys.executable,
            "-c",
            "import sys; assert sys.argv[1] == ''; sys.stdout.buffer.write(b'a\\r\\nb\\rc'); "
            "sys.stderr.write('error'); sys.exit(7)",
            "",
        ]
    )
    assert result["ok"] is False
    assert result["returncode"] == 7
    assert result["stdout"] == "a\nb\nc"
    assert result["stderr"] == "error"


@pytest.mark.asyncio
async def test_cmd_drains_both_streams_and_preserves_full_artifacts(tmp_path: Path) -> None:
    result = await runner(tmp_path, max_output_chars=8, output_artifact_dir=tmp_path / "output").run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('a' * 200000); sys.stderr.write('b' * 200000)",
        ]
    )
    assert result["ok"] is True
    assert result["stdout"] == "a" * 8
    assert result["stderr"] == "b" * 8
    assert result["stdout_truncated"] and result["stderr_truncated"]
    artifacts = {item["stream"]: item for item in result["output_artifacts"]}
    assert Path(artifacts["stdout"]["path"]).read_text() == "a" * 200000
    assert Path(artifacts["stderr"]["path"]).read_text() == "b" * 200000


@pytest.mark.asyncio
async def test_cmd_timeout_keeps_partial_output(tmp_path: Path) -> None:
    result = await runner(tmp_path, timeout=1).run(
        [
            sys.executable,
            "-c",
            "import sys,time; print('before timeout', flush=True); "
            "print('diagnostic', file=sys.stderr, flush=True); time.sleep(10)",
        ]
    )
    assert result["ok"] is False
    assert result["status"] == "timed_out"
    assert result["reason"] == "command_timeout"
    assert result["timeout_seconds"] == 1
    assert result["stdout"] == "before timeout\n"
    assert result["stderr"] == "diagnostic\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("during_spawn", [False, True])
@pytest.mark.parametrize("through_action", [False, True])
async def test_cmd_cancellation_settles_owned_process_even_when_repeated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    during_spawn: bool,
    through_action: bool,
) -> None:
    original = asyncio.create_subprocess_exec
    created = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        created.set()
        if during_spawn:
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    argv = [sys.executable, "-c", "import time; time.sleep(10)"]
    if through_action:
        agent = Agently.create_agent().use_task_workspace(tmp_path, mode="read_write")
        agent.use_record_store(tmp_path / "records")
        agent.enable_shell(
            root=tmp_path,
            commands=[sys.executable],
            sandbox="trusted_local",
            action_id="cancel_command",
        )
        task = asyncio.create_task(agent.action.async_execute_action("cancel_command", {"cmd": argv}))
    else:
        task = asyncio.create_task(runner(tmp_path).run(argv))
    try:
        await asyncio.wait_for(created.wait(), 3)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert processes[0].returncode is not None
    finally:
        release.set()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.communicate()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="Cmd only promises process-group cleanup on POSIX")
@pytest.mark.parametrize("parent_exits", [False, True])
async def test_cmd_timeout_closes_descendant_held_pipes(tmp_path: Path, parent_exits: bool) -> None:
    # The child inherits both pipes. Parent exit alone must not make cleanup skip
    # the owned process group and wait forever for this child's pipe EOF.
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
        "print('spawned', flush=True); " + ("sys.exit(0)" if parent_exits else "time.sleep(10)")
    )
    result = await asyncio.wait_for(runner(tmp_path, timeout=1).run([sys.executable, "-c", script]), 4)
    assert result["status"] == "timed_out"
    assert result["stdout"] == "spawned\n"


@pytest.mark.asyncio
async def test_cmd_rejected_command_never_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args, **kwargs):
        raise AssertionError("Rejected command must not create a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    cmd = runner(tmp_path)
    assert (await cmd.run(["not-allowed"]))["reason"] == "cmd_not_allowed"
    assert (await cmd.run([sys.executable, "-V"], workdir=tmp_path.parent))["reason"] == "workdir_not_allowed"


@pytest.mark.asyncio
async def test_cmd_spawn_failure_propagates(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing-executable")
    cmd = Cmd(allowed_cmd_prefixes=[missing], allowed_workdir_roots=[tmp_path])
    with pytest.raises(FileNotFoundError):
        await cmd.run([missing])


@pytest.mark.asyncio
async def test_cmd_passes_explicit_environment(tmp_path: Path) -> None:
    result = await runner(tmp_path, env={**os.environ, "CMD_LIFECYCLE_TEST_VALUE": "observed"}).run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['CMD_LIFECYCLE_TEST_VALUE'])",
        ]
    )
    assert result["stdout"] == "observed\n"
