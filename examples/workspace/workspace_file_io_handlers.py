import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from agently.core import WorkspaceManager


class MissingRendererExportHandler:
    name = "missing_renderer_demo"
    priority = 10
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register():
        return None

    @staticmethod
    def _on_unregister():
        return None

    def supports(self, *, operation, file_info, export_kind=None):
        _ = file_info
        return operation == "export" and export_kind == "demo_pdf"

    async def read(self, *, path, file_info, max_bytes=20000, offset=0, options=None):
        _ = (path, file_info, max_bytes, offset, options)
        raise NotImplementedError("This demo handler only implements export.")

    async def write(self, *, path, file_info, content, append=False, options=None):
        _ = (path, file_info, content, append, options)
        raise NotImplementedError("This demo handler only implements export.")

    async def export(
        self,
        *,
        source_path,
        output_path,
        source_info,
        output_info,
        export_kind,
        options=None,
    ):
        _ = (source_path, output_path, output_info, export_kind, options)
        return {
            "ok": False,
            "exported": False,
            "source_path": source_info["path"],
            "output_path": output_info["path"],
            "export_kind": "demo_pdf",
            "bytes": 0,
            "sha256": output_info["sha256"],
            "media_type": output_info.get("media_type"),
            "content_kind": output_info.get("content_kind", "unknown"),
            "handler_id": self.name,
            "diagnostics": [
                {
                    "code": "workspace.file.demo_renderer_dependency_missing",
                    "message": "Demo PDF export requires optional dependency 'demo-renderer'.",
                    "handler_id": self.name,
                    "dependency": "demo-renderer",
                }
            ],
            "file_refs": [
                {
                    "path": source_info["path"],
                    "bytes": source_info["bytes"],
                    "sha256": source_info["sha256"],
                    "media_type": source_info.get("media_type"),
                    "content_kind": source_info.get("content_kind", "unknown"),
                    "role": "source",
                }
            ],
        }


async def main():
    with TemporaryDirectory() as temp_dir:
        manager = WorkspaceManager()
        manager.register_file_io_handler(cast(Any, MissingRendererExportHandler()))
        workspace = manager.create(Path(temp_dir) / "workspace")

        await workspace.write_file("notes/todo.txt", "ship workspace file io")
        text_read = await workspace.read_file("notes/todo.txt", max_bytes=32)

        (workspace.files_root / "payload.bin").write_bytes(b"\x00\xffbinary")
        binary_read = await workspace.read_file("payload.bin")

        await workspace.write_file("report.md", "# Report\n")
        export_result = await workspace.export_file(
            "report.md",
            "report.pdf",
            export_kind="demo_pdf",
            handler="missing_renderer_demo",
        )

        summary = {
            "text_content": text_read["content"],
            "text_sha256_present": bool(text_read["sha256"]),
            "binary_readable": binary_read["readable"],
            "binary_code": binary_read["diagnostics"][0]["code"],
            "exported": export_result["exported"],
            "export_dependency": export_result["diagnostics"][0].get("dependency"),
        }
        print(summary)
        assert summary == {
            "text_content": "ship workspace file io",
            "text_sha256_present": True,
            "binary_readable": False,
            "binary_code": "workspace.file.no_read_handler",
            "exported": False,
            "export_dependency": "demo-renderer",
        }


asyncio.run(main())

# Expected key output:
# {'text_content': 'ship workspace file io', 'text_sha256_present': True, 'binary_readable': False, 'binary_code': 'workspace.file.no_read_handler', 'exported': False, 'export_dependency': 'demo-renderer'}
#
# This example uses a deterministic demo export handler so the output is stable
# even when optional renderer packages are installed locally. The default text
# handler owns plain text read/write; unsupported binary returns structured
# diagnostics; export remains an explicit handler-owned operation, not hidden
# inside write_file(...).
