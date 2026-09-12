"""Protocol simulations: these tests do not claim native Windows isolation."""
from __future__ import annotations

import asyncio
import json
import shutil
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agently.builtins.plugins.ExecutionResourceProvider._windows_sandbox import WindowsSandbox


def resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options) -> WindowsSandbox:
    monkeypatch.setattr(shutil, "which", lambda name: "wsb.exe")
    config = {"environment": "offline", "root": str(tmp_path / "workspace"), "read_only": False,
              "read_paths": {"skill": str(tmp_path / "skill & 中文")}, "timeout": 3,
              "max_output_bytes": 31, "env": {}}
    config.update(options)
    return WindowsSandbox(config)


@pytest.mark.parametrize("environment", ["offline", "online"])
@pytest.mark.parametrize("read_only", [False, True])
def test_xml_exact_authorized_mounts(tmp_path: Path, monkeypatch, environment, read_only):
    owner = resource(tmp_path, monkeypatch, environment=environment, read_only=read_only)
    xml = ET.fromstring(owner._xml(tmp_path / "control", tmp_path / "output"))
    assert xml.findtext("Networking") == ("Disable" if environment == "offline" else "Enable")
    for name in ("vGPU", "ClipboardRedirection", "AudioInput", "VideoInput", "PrinterRedirection"):
        assert xml.findtext(name) == "Disable"
    mounts = xml.findall("MappedFolders/MappedFolder")
    assert len(mounts) == 4
    assert [(item.findtext("SandboxFolder"), item.findtext("ReadOnly")) for item in mounts] == [
        (r"C:\workspace", str(read_only).lower()), (r"C:\agently-control", "true"),
        (r"C:\agently-output", "false"), (r"C:\skills\skill", "true"),
    ]
    assert mounts[-1].findtext("HostFolder") == str(tmp_path / "skill & 中文")


def test_missing_sandbox_does_not_find_host_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: calls.append(name))
    with pytest.raises(RuntimeError, match="No host fallback"):
        WindowsSandbox({})
    assert calls == ["wsb.exe"]


@pytest.mark.parametrize("kind", ["symlink", "directory", "oversized", "missing"])
def test_untrusted_readback(tmp_path, kind):
    path = tmp_path / "stdout"
    if kind == "symlink":
        (tmp_path / "secret").write_text("not an output")
        path.symlink_to(tmp_path / "secret")
    elif kind == "directory":
        path.mkdir()
    elif kind == "oversized":
        path.write_bytes(b"x" * 40)
    with pytest.raises(RuntimeError):
        WindowsSandbox._read(tmp_path, "stdout", 31)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["success", "nonzero", "missing", "malformed", "start_error", "exec_error", "timeout", "cancel"])
async def test_call_cleanup_and_delivery(tmp_path, monkeypatch, kind):
    owner = resource(tmp_path, monkeypatch, timeout=0.1 if kind == "timeout" else 3)
    calls = []
    entered = asyncio.Event()
    folders = {}
    observed = []

    async def cli(*args, timeout):
        calls.append(args)
        if args[0] == "start":
            xml = ET.fromstring(args[-1])
            for item in xml.findall("MappedFolders/MappedFolder"):
                source, target = item.findtext("HostFolder"), item.findtext("SandboxFolder")
                assert source is not None and target is not None
                folders[target] = Path(source)
            if kind == "start_error":
                raise RuntimeError("start error")
        elif args[0] == "exec":
            control, output = folders[r"C:\agently-control"], folders[r"C:\agently-output"]
            observed.append(json.loads((control / "request.json").read_text()))
            entered.set()
            if kind in {"cancel", "timeout"}:
                await asyncio.Event().wait()
            if kind == "exec_error":
                raise RuntimeError("exec error")
            if kind != "missing":
                result = {"returncode": 7 if kind == "nonzero" else 0, "timed_out": False,
                          "stdout_truncated": False, "stderr_truncated": False}
                if kind == "malformed":
                    result["returncode"] = True  # bool is not an exit code.
                (output / "result.json").write_text(json.dumps(result))
            (output / "stdout").write_text("中文", encoding="utf-8")
            (output / "stderr").write_text("")
        elif args[0] == "stop":
            assert folders[r"C:\agently-output"].exists()

    monkeypatch.setattr(owner, "_cli", cli)
    task = asyncio.create_task(owner.run(["powershell.exe", "literal;$(untrusted)"], r"C:\workspace"))
    if kind == "cancel":
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif kind in {"missing", "malformed", "start_error", "exec_error"}:
        with pytest.raises(RuntimeError):
            await task
    else:
        value = await task
        assert value["timed_out"] == (kind == "timeout")
        assert value["returncode"] == (124 if kind == "timeout" else 7 if kind == "nonzero" else 0)
        assert value["ok"] == (kind == "success")
    assert calls[0][0] == "start" and calls[-1][0] == "stop"
    ids = [call[call.index("--id") + 1] for call in calls]
    assert len(set(ids)) == 1
    assert not folders[r"C:\agently-output"].parent.exists()
    if observed:
        assert observed[0]["arguments"] == "literal;$(untrusted)"
        assert "literal" not in calls[1][-1]  # command never interpolated in the CLI controller.


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_one_stop(tmp_path, monkeypatch):
    owner = resource(tmp_path, monkeypatch)
    executing, stopping, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def cli(*args, timeout):
        calls.append(args[0])
        if args[0] == "exec":
            executing.set()
            await asyncio.Event().wait()
        if args[0] == "stop":
            stopping.set()
            await finish.wait()

    monkeypatch.setattr(owner, "_cli", cli)
    task = asyncio.create_task(owner.run(["powershell.exe"], r"C:\workspace"))
    await asyncio.wait_for(executing.wait(), 5)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["start", "exec", "stop"]


@pytest.mark.asyncio
async def test_failed_stop_retains_only_owned_directory(tmp_path, monkeypatch):
    owner = resource(tmp_path, monkeypatch)
    owned = tmp_path / "owned-sandbox"
    owned.mkdir()
    sentinel = tmp_path / "unrelated"
    sentinel.write_text("preserve")
    monkeypatch.setattr("agently.builtins.plugins.ExecutionResourceProvider._windows_sandbox.tempfile.mkdtemp",
                        lambda **kwargs: str(owned))
    calls = []

    async def cli(*args, timeout):
        calls.append(args)
        if args[0] == "stop":
            raise RuntimeError("simulated stop failure")

    monkeypatch.setattr(owner, "_cli", cli)
    with pytest.warns(RuntimeWarning, match="cleanup was not confirmed; retained"):
        with pytest.raises(RuntimeError, match="stop failure"):
            await owner.run(["powershell.exe"], r"C:\workspace")
    assert owned.is_dir() and (owned / "control" / "request.json").is_file()
    assert sentinel.read_text() == "preserve"
    assert [call[0] for call in calls] == ["start", "exec", "stop"]
    assert len({call[call.index("--id") + 1] for call in calls}) == 1


def test_regular_windows_reparse_file_is_not_read(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(Path, "lstat", lambda self: SimpleNamespace(
        st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))
    with pytest.raises(RuntimeError, match="non-reparse"):
        WindowsSandbox._read(tmp_path, "stdout", 31)
