"""One-call Windows Sandbox transport; never substitutes a host interpreter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from agently.types.data.shell import ShellResult

from ._bounded_process import run_bounded_process


def sandbox_paths(config: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """One projection shared by the Action contract, risk facts and transport."""
    if config["environment"] == "host":
        return str(config["root"]), dict(config["read_paths"])
    if os.name == "nt":
        return r"C:\workspace", {name: "C:\\skills\\" + name for name in config["read_paths"]}
    return "/workspace", {name: "/skills/" + name for name in config["read_paths"]}


class WindowsSandbox:
    """Internal ShellResource backend. Requires the installed Windows wsb CLI."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.binary = shutil.which("wsb.exe")
        if self.binary is None:
            raise RuntimeError(
                "Windows Sandbox CLI (wsb.exe, Windows 11 24H2+) is required for isolated Shell. "
                "Enable it explicitly in Windows; CrossOver does not provide this capability. No host fallback was used."
            )

    def _xml(self, control: Path, output: Path) -> str:
        root = ET.Element("Configuration")
        for name in ("vGPU", "AudioInput", "VideoInput", "PrinterRedirection", "ClipboardRedirection"):
            ET.SubElement(root, name).text = "Disable"
        ET.SubElement(root, "Networking").text = "Disable" if self.config["environment"] == "offline" else "Enable"
        mappings = ET.SubElement(root, "MappedFolders")
        paths = [(self.config["root"], r"C:\workspace", self.config["read_only"]),
                 (str(control), r"C:\agently-control", True), (str(output), r"C:\agently-output", False)]
        paths.extend((path, "C:\\skills\\" + name, True) for name, path in self.config["read_paths"].items())
        for source, target, read_only in paths:
            mapping = ET.SubElement(mappings, "MappedFolder")
            ET.SubElement(mapping, "HostFolder").text = str(source)
            ET.SubElement(mapping, "SandboxFolder").text = target
            ET.SubElement(mapping, "ReadOnly").text = "true" if read_only else "false"
        xml = ET.tostring(root, encoding="unicode")
        if len(xml.encode("utf-16-le")) > 24000:
            raise ValueError("Windows Sandbox mount configuration exceeds the bounded CLI command size")
        return xml

    @staticmethod
    def _read(directory: Path, name: str, limit: int, *, optional: bool = False) -> bytes:
        # The owned Sandbox has already stopped. Do not follow its reparse
        # points/symlinks or accept directories/devices as result files.
        path = directory / name
        try:
            status = path.lstat()
        except FileNotFoundError:
            if optional:
                return b""
            raise RuntimeError(f"Windows Sandbox did not deliver {name}") from None
        if not stat.S_ISREG(status.st_mode) or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError("Windows Sandbox output is not a regular non-reparse file")
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise RuntimeError("Windows Sandbox result exceeds its output contract")
        return value

    async def _cli(self, *args: str, timeout: float) -> None:
        assert self.binary is not None
        result = await run_bounded_process([self.binary, *args], timeout=timeout, max_output_bytes=4096)
        if result.timed_out or result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Windows Sandbox {args[0]} failed (exit={result.returncode}): {detail}")

    async def run(self, argv: list[str], cwd: str) -> ShellResult:
        # The id exists before launch, so even cancellation during startup has
        # one exact cleanup target. No global stop or process-name termination.
        sandbox_id = str(uuid.uuid4())
        directory = Path(tempfile.mkdtemp(prefix="agently-shell-"))
        control, output = directory / "control", directory / "output"
        control.mkdir()
        output.mkdir()
        launched = False
        stopped = False
        timed_out = False
        timeout = float(self.config["timeout"])
        try:
            shutil.copyfile(Path(__file__).with_name("_windows_shell.ps1"), control / "run.ps1")
            request = {
                "binary": argv[0], "arguments": subprocess.list2cmdline(argv[1:]),
                "cwd": cwd, "output": r"C:\agently-output", "env": self.config["env"],
                "limit": self.config["max_output_bytes"], "milliseconds": min(2147483647, max(1, int(timeout * 1000))),
            }
            (control / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            xml = self._xml(control, output)

            async def execute() -> None:
                nonlocal launched
                launched = True
                await self._cli("start", "--id", sandbox_id, "--config", xml, timeout=timeout + 5)
                await self._cli(
                    "exec", "--id", sandbox_id, "-r", "System", "-c",
                    r'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\agently-control\run.ps1 -RequestPath C:\agently-control\request.json',
                    timeout=timeout + 5,
                )

            try:
                await asyncio.wait_for(execute(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
            finally:
                if launched:
                    cleanup = asyncio.create_task(self._cli("stop", "--id", sandbox_id, timeout=45))
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                    cleanup.result()  # Failed cleanup is never a successful call.
                    stopped = True
                    if cancelled:
                        raise asyncio.CancelledError
            limit = int(self.config["max_output_bytes"])
            raw = self._read(output, "result.json", 4096, optional=timed_out)
            if raw:
                result = json.loads(raw)
                if not isinstance(result, dict) or type(result.get("returncode")) is not int or any(
                    type(result.get(key)) is not bool for key in ("timed_out", "stdout_truncated", "stderr_truncated")
                ):
                    raise RuntimeError("Invalid Windows Sandbox completion record")
                timed_out = timed_out or result["timed_out"]
            else:
                result = {"returncode": 124, "stdout_truncated": True, "stderr_truncated": True}
            return {
                "ok": result["returncode"] == 0 and not timed_out,
                "returncode": 124 if timed_out else result["returncode"], "timed_out": timed_out,
                "stdout": self._read(output, "stdout", limit, optional=timed_out).decode("utf-8", errors="replace"),
                "stderr": self._read(output, "stderr", limit, optional=timed_out).decode("utf-8", errors="replace"),
                "stdout_truncated": result["stdout_truncated"], "stderr_truncated": result["stderr_truncated"],
            }
        finally:
            if stopped or not launched:
                shutil.rmtree(directory)
            else:
                # Do not remove a live mapped directory after failed cleanup.
                # Surface its exact identity so the Host can diagnose/recover.
                import warnings
                warnings.warn(f"Shell Sandbox {sandbox_id} cleanup was not confirmed; retained {directory}", RuntimeWarning, stacklevel=2)
