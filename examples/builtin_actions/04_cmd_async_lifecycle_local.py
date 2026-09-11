"""Model-free Cmd infrastructure smoke: event-loop progress and timeout output.

This tests the command runner, not model planning. Applications exposing shell
to a model should prefer agent.enable_shell(...) with explicit host policy.
Cmd remains an argv runner; this example adds no shell or sandbox permission.
"""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from agently.builtins.actions import Cmd


async def main() -> None:
    with TemporaryDirectory(prefix="agently-cmd-example-") as directory:
        cmd = Cmd(
            allowed_cmd_prefixes=[sys.executable],
            allowed_workdir_roots=[Path(directory)],
            timeout=1,
        )
        progressed = asyncio.Event()
        timer = asyncio.get_running_loop().call_later(0.02, progressed.set)
        try:
            completed = await cmd.run(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.15); print('done')",
                ]
            )
        finally:
            timer.cancel()
        timed_out = await cmd.run(
            [
                sys.executable,
                "-c",
                "import time; print('partial', flush=True); time.sleep(10)",
            ]
        )
        print(
            {
                "event_loop_progressed": progressed.is_set(),
                "returncode": completed["returncode"],
                "stdout": completed["stdout"].strip(),
                "timeout_status": timed_out["status"],
                "partial_stdout": timed_out["stdout"].strip(),
            }
        )


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output (local infrastructure run):
# {'event_loop_progressed': True, 'returncode': 0, 'stdout': 'done',
#  'timeout_status': 'timed_out', 'partial_stdout': 'partial'}
# Host timer runs while Cmd awaits the process. Timeout kills the owned process
# and retains captured partial stdout; no model request or semantic judgment runs.
