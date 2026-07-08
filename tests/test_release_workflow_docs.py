from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_workflows_require_foundation_example_effect_gate():
    english = (ROOT / "docs/en/development/release-workflows.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs/cn/development/release-workflows.md").read_text(encoding="utf-8")

    for text in (english, chinese):
        assert "Foundation Example Effect Gate" in text
        assert "examples/" in text
        assert "DeepSeek" in text
        assert "Ollama" in text
        assert "pyright" in text
        assert "pytest" in text
        assert "fail closed" in text or "fails closed" in text
        assert "Foundation example effect checks" in text
        assert "Pinned Developer Usage Example Gate" in text or "锁定开发者用法 Example Gate" in text
        assert "ask the maintainer" in text or "请示维护者" in text
        assert "recommended usage" in text or "推荐用法" in text
        assert "all-allowed test capability policy" in text or "全开的测试 capability" in text
