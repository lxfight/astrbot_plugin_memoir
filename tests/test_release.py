"""Exercise the release CLI against real files without publishing anything."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_notes.py"


@pytest.fixture
def release_project(tmp_path):
    (tmp_path / "metadata.yaml").write_text("version: 0.4.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "astrbot-plugin-memoir"\nversion = "0.4.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "aiosqlite"\nversion = "0.22.1"\n'
        '[[package]]\nname = "astrbot-plugin-memoir"\nversion = "0.4.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# 更新日志\n\n## [Unreleased]\n\n- Future changes.\n\n"
        "## [0.4.0] — 2026-09-07\n\n### 新增\n\n- 记忆图标。\n\n"
        "```markdown\n## This is a code example\n```\n\n"
        "## [0.3.0.dev0]\n\n- Older changes.\n\n"
        "[Unreleased]: https://example.com/compare\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize("tag", [None, "v0.4.0"])
def test_release_notes_extract_exact_section_and_preserve_markdown(
    release_project, tag
):
    output = release_project / "nested" / "notes.md"
    args = [sys.executable, str(SCRIPT), "--output", str(output)]
    if tag:
        args.extend(["--tag", tag])
    result = subprocess.run(args, cwd=release_project, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == (
        "### 新增\n\n- 记忆图标。\n\n```markdown\n## This is a code example\n```\n"
    )


@pytest.mark.parametrize(
    ("file", "content", "message"),
    [
        ("metadata.yaml", "version: invalid\n", "unsupported release version"),
        ("metadata.yaml", "version: 0.4.0\nversion: 0.4.0\n", "exactly one version"),
        ("pyproject.toml", '[project]\nversion = "0.3.0"\n', "pyproject.toml version"),
        ("uv.lock", "version = 1\n", "uv.lock plugin version is stale"),
        ("CHANGELOG.md", "## [Unreleased]\n\n- Pending.\n", "exactly one ## [0.4.0]"),
        ("CHANGELOG.md", "## [0.4.0]\n\n## [0.3.0]\n\n- Old.\n", "is empty"),
        ("CHANGELOG.md", "## [0.4.0]\n\n### Added\n", "is empty"),
        (
            "CHANGELOG.md",
            "## [0.4.0]\n\n- First.\n\n## [0.4.0]\n\n- Duplicate.\n",
            "exactly one ## [0.4.0]",
        ),
    ],
)
def test_invalid_release_metadata_blocks_notes(release_project, file, content, message):
    (release_project / file).write_text(content, encoding="utf-8")
    output = release_project / "notes.md"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v0.4.0", "--output", str(output)],
        cwd=release_project,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("tag", ["v0.5.0", "0.4.0", "main", "v0.4.0; echo injected"])
def test_tag_must_match_current_version(release_project, tag):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", tag, "--output", "notes.md"],
        cwd=release_project,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "does not match metadata version" in result.stderr
    assert not (release_project / "notes.md").exists()


def test_prerelease_at_end_of_changelog_excludes_link_definitions(release_project):
    for name in ["metadata.yaml", "pyproject.toml", "uv.lock"]:
        file = release_project / name
        file.write_text(
            file.read_text(encoding="utf-8").replace("0.4.0", "0.4.0rc1"),
            encoding="utf-8",
        )
    (release_project / "CHANGELOG.md").write_text(
        "## [0.4.0rc1]\n\n- Release candidate.\n\n"
        "[0.4.0rc1]: https://example.com/release\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v0.4.0rc1", "--output", "notes.md"],
        cwd=release_project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (release_project / "notes.md").read_text(encoding="utf-8") == (
        "- Release candidate.\n"
    )
