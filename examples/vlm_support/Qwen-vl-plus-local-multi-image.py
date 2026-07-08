import os
import struct
import tempfile
import zlib
from pathlib import Path

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


def configure_vlm():
    load_dotenv(find_dotenv())
    api_key = os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing QWEN_API_KEY or DASHSCOPE_API_KEY. Put one in your environment or .env.")

    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "model": os.environ.get("QWEN_VLM_MODEL", "qwen-vl-plus"),
            "auth": api_key,
            "request_options": {"temperature": 0.1},
        },
    )


def main():
    configure_vlm()
    agent = Agently.create_agent()

    with tempfile.TemporaryDirectory() as temp_dir:
        red_path = Path(temp_dir) / "red.png"
        green_path = Path(temp_dir) / "green.png"
        write_solid_png(red_path, (220, 20, 20))
        write_solid_png(green_path, (20, 170, 70))

        result = agent.image(
            question="These two generated PNG files are solid color swatches. Name the dominant color of each image in order.",
            files=[red_path, green_path],
        ).start()

    print(result)


if __name__ == "__main__":
    main()


# Requires QWEN_API_KEY or DASHSCOPE_API_KEY.
# Expected key output (real run with qwen-vl-plus on 2026-06-02):
# first image is red; second image is green.
#
# How it works:
# .image(question="...", files=[...]) converts each local image to a data URL
# and sends one text part plus multiple image_url parts through the existing
# rich-content prompt path. Use url="..." / urls=[...] for remote images.
