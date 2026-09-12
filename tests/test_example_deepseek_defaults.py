from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = REPOSITORY_ROOT / "examples"
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt"}

AFFECTED_CONFIG_FILES = (
    "action_runtime/4_4_programmatic_vs_structured_deepseek.py",
    "archived/pre-4.1.3.8-skills-orchestration/skills_executor/02_deepseek_external_skill_cards.py",
    "archived/pre-4.1.3.8-skills-orchestration/skills_executor/04_dynamic_todo_triggerflow_realcase.py",
    "archived/pre-4.1.3.8-skills-orchestration/skills_executor/05_combo_skillpack_diagnostics.py",
    "archived/tools_using/tool_agent_search_test.py",
)


def test_examples_do_not_recommend_deepseek_chat() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in EXAMPLES_ROOT.rglob("*")
        if path.is_file()
        and path.suffix.lower() in TEXT_SUFFIXES
        and "deepseek-chat" in path.read_text(encoding="utf-8", errors="ignore")
    ]

    assert offenders == []


def test_updated_deepseek_examples_use_flash_without_thinking() -> None:
    for relative_path in AFFECTED_CONFIG_FILES:
        source = (EXAMPLES_ROOT / relative_path).read_text(encoding="utf-8")
        assert '"deepseek-v4-flash"' in source
        assert '"thinking": {"type": "disabled"}' in source
