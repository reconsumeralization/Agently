"""Validate the standard SKILL.md registry against real Anthropic Skill shapes.

Two layers:

* Layer A (always runs): deterministic fixtures built in tmp_path that reproduce
  the *structural* shapes observed in github.com/anthropics/skills — long quoted
  descriptions with backticks / arrows / em-dashes / escaped quotes, frontmatter
  with no `keywords` and no `version`, a `license` field, and resource trees that
  mix standard (`scripts`/`references`/`assets`) with non-standard layout
  (`reference/` singular, `agents/`, loose root `.md`). No proprietary skill text
  is copied into the repo.

* Layer B (runs only when a real checkout is present): installs the actual
  Anthropic Skills from a local clone resolved via `ANTHROPIC_SKILLS_REPO` or
  `.example_runtime/skills_executor/anthropic-skills`. Skips cleanly otherwise.

These lock the real-world contract so the implementation cannot silently drift.
"""

import json
import os
from pathlib import Path

import pytest

from agently import Agently

_ALLOWED_CARD_KEYS = {
    "skill_id",
    "name",
    "description",
    "keywords",
    "guidance_excerpt",
    "resource_summary",
    "checksum",
}
_FORBIDDEN_CARD_KEYS = {"only_when", "exclude_when", "not_for", "required_context", "availability"}
_STANDARD_RESOURCE_TOP = {"scripts", "references", "assets"}


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path):
    Agently.settings.set("skills.registry.root", str(tmp_path / "registry"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── Layer A: real-shape fixtures ────────────────────────────────────────────

# A docx/claude-api-style quoted description: YAML double-quoted scalar holding
# backticks, an em-dash, an arrow, escaped quotes, colons, and dotted filenames.
_QUOTED_DESC = (
    'Use this skill whenever a .docx file is involved — create, read, or edit Word '
    'documents. TRIGGER when: code imports `python-docx`; user mentions "Word doc" '
    'or migrating 4.6 → 4.7. SKIP: PDFs or `*.xlsx`.'
)


def _docx_like_skill(root: Path):
    # Mirrors the dominant real frontmatter: name + (quoted) description + license,
    # no keywords, no version. The double-quoted YAML scalar escapes inner quotes.
    escaped = _QUOTED_DESC.replace('"', '\\"')
    _write(
        root / "SKILL.md",
        f'---\nname: docx\ndescription: "{escaped}"\nlicense: Proprietary. LICENSE.txt has complete terms\n---\n\n'
        "# docx\n\nFollow these steps when producing a Word document.\n",
    )


def test_real_quoted_description_with_special_chars_round_trips(tmp_path):
    source = tmp_path / "docx"
    _docx_like_skill(source)

    contract = Agently.skills_executor.install_skills(source, trust_level="local")

    assert contract["skill_id"] == "docx"
    assert contract["card"]["display_name"] == "docx"
    # Description survives YAML unescaping with every special token intact.
    description = contract["card"]["description"]
    assert description == _QUOTED_DESC
    for token in ("`python-docx`", '"Word doc"', "4.6 → 4.7", "—", ".docx", "*.xlsx"):
        assert token in description
    assert contract["decision_card"]["description"] == _QUOTED_DESC


def test_real_frontmatter_without_keywords_or_version_uses_safe_defaults(tmp_path):
    source = tmp_path / "docx"
    _docx_like_skill(source)

    contract = Agently.skills_executor.install_skills(source, trust_level="local")

    # No real Anthropic skill ships keywords or version.
    assert contract["version"] == "0.1.0"
    assert contract["card"]["activation_hints"]["keywords"] == []
    assert contract["decision_card"]["keywords"] == []
    # A present (non-empty) description must not raise a missing_description diagnostic.
    codes = {d.get("code") for d in contract["diagnostics"]}
    assert "missing_description" not in codes
    # Unknown frontmatter keys (e.g. license) are preserved, not promoted to the contract.
    assert contract["metadata"]["frontmatter"]["license"].startswith("Proprietary")
    assert contract["metadata"]["skill_format"] == "anthropic-skill"


def test_resource_index_covers_standard_dirs_only_but_copies_full_tree(tmp_path):
    """Reproduces mcp-builder / pdf / skill-creator: non-standard dirs and loose
    root files are copied verbatim but never enter the resource index."""
    source = tmp_path / "skill-creator"
    _write(
        source / "SKILL.md",
        "---\nname: skill-creator\ndescription: Create and optimize skills.\n---\n\n# skill-creator\n\nGuidance.\n",
    )
    # Standard resource dirs (indexed):
    _write(source / "scripts" / "init.py", "print('hi')\n")
    _write(source / "references" / "patterns.md", "# patterns\n")
    _write(source / "assets" / "logo.txt", "logo\n")
    # Non-standard layout (copied, NOT indexed): singular `reference/`, `agents/`,
    # `eval-viewer/`, and loose root files.
    _write(source / "reference" / "legacy.md", "# legacy\n")
    _write(source / "agents" / "helper.md", "# helper\n")
    _write(source / "eval-viewer" / "index.html", "<html></html>\n")
    _write(source / "forms.md", "# forms\n")
    _write(source / "LICENSE.txt", "license text\n")

    contract = Agently.skills_executor.install_skills(source, trust_level="local")
    installed = Path(contract["source"]["installed_path"])

    # Full tree copied verbatim.
    for rel in ("reference/legacy.md", "agents/helper.md", "eval-viewer/index.html", "forms.md", "LICENSE.txt"):
        assert (installed / rel).is_file(), f"expected copied: {rel}"

    resource_paths = {r["path"] for r in contract["resource_index"]["resources"]}
    assert resource_paths == {"scripts/init.py", "references/patterns.md", "assets/logo.txt"}
    assert {r["path"].split("/")[0] for r in contract["resource_index"]["resources"]} <= _STANDARD_RESOURCE_TOP
    assert {r["kind"] for r in contract["resource_index"]["resources"]} == {"script", "reference", "asset"}
    # Non-standard content stays out of the index.
    for stray in ("reference/legacy.md", "agents/helper.md", "eval-viewer/index.html", "forms.md", "LICENSE.txt"):
        assert stray not in resource_paths


def test_real_shape_decision_card_is_descriptive_only_and_checksum_matches(tmp_path):
    source = tmp_path / "docx"
    _docx_like_skill(source)
    _write(source / "scripts" / "build.py", "print('build')\n")

    contract = Agently.skills_executor.install_skills(source, trust_level="local")
    card = contract["decision_card"]

    assert set(card.keys()) <= _ALLOWED_CARD_KEYS
    assert _FORBIDDEN_CARD_KEYS.isdisjoint(card.keys())
    assert card["checksum"] == contract["checksums"]["root_checksum"]
    # The persisted card on disk matches the contract card.
    on_disk = json.loads((Path(contract["source"]["installed_path"]) / ".agently" / "decision_card.json").read_text())
    assert on_disk["checksum"] == card["checksum"]


# ── Layer B: the actual Anthropic skills checkout (optional) ─────────────────

def _resolve_anthropic_repo() -> Path | None:
    configured = os.getenv("ANTHROPIC_SKILLS_REPO")
    root = Path(__file__).resolve().parents[1]
    candidates = [
        Path(configured).expanduser() if configured else None,
        root / ".example_runtime" / "skills_executor" / "anthropic-skills",
    ]
    for candidate in candidates:
        if candidate and (candidate / "skills").is_dir():
            return candidate / "skills"
    return None


_SKILLS_ROOT = _resolve_anthropic_repo()
_REAL_SKILL_DIRS = (
    sorted(p for p in _SKILLS_ROOT.iterdir() if (p / "SKILL.md").is_file()) if _SKILLS_ROOT else []
)
_requires_checkout = pytest.mark.skipif(
    not _REAL_SKILL_DIRS,
    reason="No Anthropic skills checkout. Set ANTHROPIC_SKILLS_REPO or clone into .example_runtime/.",
)


@_requires_checkout
@pytest.mark.parametrize("skill_dir", _REAL_SKILL_DIRS, ids=[p.name for p in _REAL_SKILL_DIRS])
def test_each_real_anthropic_skill_normalizes(skill_dir, tmp_path):
    contract = Agently.skills_executor.install_skills(skill_dir, trust_level="local", update=True)

    assert contract["skill_id"]  # non-empty slug derived from frontmatter name
    assert contract["metadata"]["skill_format"] == "anthropic-skill"
    assert contract["guidance"]["content"].strip(), "SKILL.md body must be captured as guidance"
    assert contract["card"]["description"].strip(), "real skills always carry a description"

    card = contract["decision_card"]
    assert set(card.keys()) <= _ALLOWED_CARD_KEYS
    assert _FORBIDDEN_CARD_KEYS.isdisjoint(card.keys())
    assert card["checksum"] == contract["checksums"]["root_checksum"]
    # Every indexed resource lives under a standard resource dir.
    assert {r["path"].split("/")[0] for r in contract["resource_index"]["resources"]} <= _STANDARD_RESOURCE_TOP


@_requires_checkout
def test_real_anthropic_skills_install_as_a_pack(tmp_path):
    record = Agently.skills_executor.install_skills_pack(
        str(_SKILLS_ROOT), name="anthropic-skills", trust_level="local"
    )

    assert record["status"] == "success"
    assert record["failed_skills"] == []
    installed = set(record["installed_skills"])
    # Every top-level real skill is represented in the pack.
    expected = {p.name for p in _REAL_SKILL_DIRS}
    assert expected <= installed
