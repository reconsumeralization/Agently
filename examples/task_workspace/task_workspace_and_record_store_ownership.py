from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from agently import Agently


async def main() -> dict[str, object]:
    with TemporaryDirectory(prefix="agently-owner-boundaries-") as temp_dir:
        root = Path(temp_dir).resolve()
        first = Agently.create_agent("owner-a")
        second = Agently.create_agent("owner-b")

        default_task_workspaces_are_isolated = first.task_workspace.root != second.task_workspace.root

        first.use_task_workspace(root / "shared-files", mode="read_write")
        second.use_task_workspace(root / "shared-files", mode="read_write")
        first.use_record_store(root / "records", mode="read_write")
        second.use_record_store(root / "records", mode="read_write")

        await first.task_workspace.write_file("outputs/probe.txt", "shared file boundary")
        readback = await second.task_workspace.read_file("outputs/probe.txt")

        await first.record_store.put(
            "first agent record",
            collection="observations",
            kind="owner_probe",
        )
        first_records = await first.record_store.search(
            "agent record",
            filters={"kind": "owner_probe", "scope.agent_id": first.id},
        )
        second_records = await second.record_store.search(
            "agent record",
            filters={"kind": "owner_probe", "scope.agent_id": second.id},
        )

        return {
            "default_task_workspaces_are_isolated": default_task_workspaces_are_isolated,
            "explicit_file_boundary_is_shared": readback.content == "shared file boundary",
            "record_store_physical_root_is_shared": first.record_store.root == second.record_store.root,
            "record_store_scope_is_explicit": len(first_records) == 1 and len(second_records) == 0,
        }


if __name__ == "__main__":
    result = asyncio.run(main())
    print(result)
    assert all(result.values())


# Sharing a filesystem directory and sharing record persistence are independent
# application decisions. TaskWorkspace and RecordStore never imply each other.
