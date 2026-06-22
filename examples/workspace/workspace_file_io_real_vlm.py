import asyncio
import json
import os
import struct
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from dotenv import find_dotenv, load_dotenv

from agently import Agently


def write_solid_png(path: Path, color: tuple[int, int, int], *, width: int = 48, height: int = 48) -> None:
    raw_rows = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw_rows))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def require_api_key() -> str:
    api_key = (
        os.environ.get("WORKSPACE_FILE_IO_VLM_API_KEY")
        or os.environ.get("QWEN_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
    )
    if not api_key:
        raise RuntimeError(
            "This real VLM example requires WORKSPACE_FILE_IO_VLM_API_KEY, "
            "QWEN_API_KEY, or DASHSCOPE_API_KEY. It does not mock or fake image interpretation."
        )
    return api_key


def load_vlm_env() -> None:
    explicit_env_file = os.environ.get("WORKSPACE_FILE_IO_VLM_ENV_FILE")
    if explicit_env_file:
        load_dotenv(explicit_env_file)
        return
    load_dotenv(find_dotenv())


def configure_vlm(api_key: str) -> str:
    model = os.environ.get("WORKSPACE_FILE_IO_VLM_MODEL", "qwen3-vl-plus")
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.environ.get(
                "WORKSPACE_FILE_IO_VLM_BASE_URL",
                os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ),
            "model": model,
            "auth": api_key,
            "request_options": {"temperature": 0.0},
        },
    )
    return model


def normalize_result(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        raise AssertionError(f"Expected structured VLM result data, got: { type(data).__name__ }")
    return {
        "first_color": str(data.get("first_color", "")).strip().lower(),
        "second_color": str(data.get("second_color", "")).strip().lower(),
        "evidence": str(data.get("evidence", "")).strip(),
    }


def result_data(result: Any) -> Any:
    get_data = getattr(result, "get_data", None)
    if callable(get_data):
        return get_data()
    return result


async def main() -> None:
    load_vlm_env()
    model = configure_vlm(require_api_key())

    with TemporaryDirectory() as temp_dir:
        workspace = Agently.create_workspace(Path(temp_dir) / "workspace")
        red_path = workspace.files_root / "red.png"
        green_path = workspace.files_root / "green.png"
        write_solid_png(red_path, (220, 20, 20))
        write_solid_png(green_path, (20, 170, 70))

        red_read = await workspace.read_file("red.png")
        green_read = await workspace.read_file("green.png")
        assert red_read["handler_id"] == "image_vlm"
        assert green_read["handler_id"] == "image_vlm"
        red_attachments = red_read.get("attachments")
        green_attachments = green_read.get("attachments")
        assert red_attachments
        assert green_attachments

        attachment = [
            {
                "type": "text",
                "text": (
                    "These two generated PNG files are solid color swatches. "
                    "Return the dominant color of the first image and the second image."
                ),
            },
            *red_attachments,
            *green_attachments,
        ]

        result = (
            Agently.create_agent()
            .attachment(attachment)
            .output(
                {
                    "first_color": (str, "Dominant color of the first image: red, green, blue, yellow, black, or white.", True),
                    "second_color": (str, "Dominant color of the second image: red, green, blue, yellow, black, or white.", True),
                    "evidence": (str, "Brief visual evidence from the images.", True),
                },
                format="json",
            )
            .start()
        )
        data = normalize_result(result_data(result))
        summary = {
            "model": model,
            "workspace_handlers": [red_read["handler_id"], green_read["handler_id"]],
            "first_color": data["first_color"],
            "second_color": data["second_color"],
            "accepted": data["first_color"] == "red" and data["second_color"] == "green",
            "evidence": data["evidence"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        assert summary["accepted"], summary


if __name__ == "__main__":
    asyncio.run(main())

# Required environment:
# - WORKSPACE_FILE_IO_VLM_API_KEY, QWEN_API_KEY, or DASHSCOPE_API_KEY
# - Optional: WORKSPACE_FILE_IO_VLM_MODEL (defaults to qwen3-vl-plus)
# - Optional: WORKSPACE_FILE_IO_VLM_BASE_URL (defaults to DashScope compatible mode)
# - Optional: WORKSPACE_FILE_IO_VLM_ENV_FILE for a non-default dotenv path
#
# Expected key output from a successful real VLM run:
# {
#   "accepted": true,
#   "first_color": "red",
#   "model": "qwen3-vl-plus",
#   "second_color": "green",
#   "workspace_handlers": ["image_vlm", "image_vlm"]
# }
#
# This example is intentionally a real model E2E. It uses the Workspace image
# handler only to prepare ModelRequest-compatible image attachments; image
# interpretation belongs to the configured VLM model request.
