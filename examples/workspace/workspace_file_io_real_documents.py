import asyncio
import json
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from agently import Agently


def create_pdf(path: Path) -> None:
    canvas_module = import_module("reportlab.pdfgen.canvas")
    canvas = canvas_module.Canvas(str(path))
    canvas.drawString(72, 720, "workspace pdf success")
    canvas.save()


def create_docx(path: Path) -> None:
    docx_module = import_module("docx")
    document = docx_module.Document()
    document.add_paragraph("workspace docx success")
    document.save(str(path))


def create_xlsx(path: Path) -> None:
    openpyxl = import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["kind", "status"])
    sheet.append(["workspace xlsx", "success"])
    workbook.save(str(path))


def create_pptx(path: Path) -> None:
    pptx_module = import_module("pptx")
    presentation = pptx_module.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    textbox = slide.shapes.add_textbox(1000000, 1000000, 6000000, 1000000)
    textbox.text = "workspace pptx success"
    presentation.save(str(path))


async def main() -> None:
    with TemporaryDirectory() as temp_dir:
        workspace = Agently.create_workspace(Path(temp_dir) / "workspace")
        root = workspace.files_root

        create_pdf(root / "sample.pdf")
        create_docx(root / "sample.docx")
        create_xlsx(root / "sample.xlsx")
        create_pptx(root / "sample.pptx")
        await workspace.write_file("notes/readback.txt", "workspace text read write success")
        await workspace.write_file("sample.html", "<html><body><h1>workspace html export success</h1></body></html>")
        await workspace.write_file("sample.md", "# workspace markdown export success\n")

        text = await workspace.read_file("notes/readback.txt")
        pdf = await workspace.read_file("sample.pdf")
        docx = await workspace.read_file("sample.docx")
        xlsx = await workspace.read_file("sample.xlsx")
        pptx = await workspace.read_file("sample.pptx")
        html_pdf = await workspace.export_file("sample.html", "sample.html.pdf", export_kind="html_pdf")
        markdown_pdf = await workspace.export_file("sample.md", "sample.md.pdf", export_kind="markdown_pdf")
        screenshot = await workspace.export_file("sample.html", "sample.png", export_kind="html_screenshot")

        summary = {
            "text_read_write": {
                "ok": text["ok"],
                "method": text["extraction_method"],
                "content": text["content"],
                "sha256_present": bool(text["sha256"]),
            },
            "pdf": {
                "ok": pdf["ok"],
                "method": pdf["extraction_method"],
                "contains_expected_text": "workspace pdf success" in pdf["content"],
            },
            "docx": {
                "ok": docx["ok"],
                "method": docx["extraction_method"],
                "contains_expected_text": "workspace docx success" in docx["content"],
            },
            "xlsx": {
                "ok": xlsx["ok"],
                "method": xlsx["extraction_method"],
                "contains_expected_text": "workspace xlsx\tsuccess" in xlsx["content"],
            },
            "pptx": {
                "ok": pptx["ok"],
                "method": pptx["extraction_method"],
                "contains_expected_text": "workspace pptx success" in pptx["content"],
            },
            "html_pdf": {
                "exported": html_pdf["exported"],
                "bytes_positive": html_pdf["bytes"] > 0,
            },
            "markdown_pdf": {
                "exported": markdown_pdf["exported"],
                "bytes_positive": markdown_pdf["bytes"] > 0,
            },
            "html_screenshot": {
                "exported": screenshot["exported"],
                "bytes_positive": screenshot["bytes"] > 0,
            },
        }

        print(json.dumps(summary, indent=2, sort_keys=True))

        expected = [
            summary["text_read_write"]["content"] == "workspace text read write success",
            summary["text_read_write"]["sha256_present"],
            summary["pdf"]["contains_expected_text"],
            summary["docx"]["contains_expected_text"],
            summary["xlsx"]["contains_expected_text"],
            summary["pptx"]["contains_expected_text"],
            summary["html_pdf"]["exported"] and summary["html_pdf"]["bytes_positive"],
            summary["markdown_pdf"]["exported"] and summary["markdown_pdf"]["bytes_positive"],
            summary["html_screenshot"]["exported"] and summary["html_screenshot"]["bytes_positive"],
        ]
        assert all(expected), summary


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output from a real local run:
# {
#   "docx": {"contains_expected_text": true, "method": "python-docx", "ok": true},
#   "html_pdf": {"bytes_positive": true, "exported": true},
#   "html_screenshot": {"bytes_positive": true, "exported": true},
#   "markdown_pdf": {"bytes_positive": true, "exported": true},
#   "pdf": {"contains_expected_text": true, "method": "pypdf.extract_text", "ok": true},
#   "pptx": {"contains_expected_text": true, "method": "python-pptx", "ok": true},
#   "text_read_write": {"content": "workspace text read write success", "method": "text.decode", "ok": true, "sha256_present": true},
#   "xlsx": {"contains_expected_text": true, "method": "openpyxl", "ok": true}
# }
#
# This example is intentionally a real optional-dependency E2E: it creates real
# text/PDF/DOCX/XLSX/PPTX/HTML/Markdown inputs, proves text read/write through
# Workspace handlers, and exports real PDF/screenshot files through the renderer
# handler.
