"""Validate release versions and extract the matching CHANGELOG section."""

import argparse
import re
from pathlib import Path

VERSION = re.compile(r"\d+\.\d+\.\d+(?:[.-]?(?:dev|alpha|beta|rc|a|b)[.-]?\d+)?")


def main() -> None:
    """Validate metadata and write only the requested version's release notes.

    Raises:
        SystemExit: Required files, versions or changelog entries are invalid.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Existing release tag, for example v0.4.0")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        metadata = Path("metadata.yaml").read_text(encoding="utf-8")
        project = Path("pyproject.toml").read_text(encoding="utf-8")
        lock = Path("uv.lock").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(str(exc))

    versions = re.findall(r"^version:[ \t]*([^\n#]+)", metadata, re.MULTILINE)
    if len(versions) != 1:
        parser.error("metadata.yaml must contain exactly one version")
    version = versions[0].strip().strip("\"'")
    if not VERSION.fullmatch(version):
        parser.error("metadata.yaml contains an unsupported release version")
    if args.tag is not None and args.tag != f"v{version}":
        parser.error(f"Tag {args.tag!r} does not match metadata version v{version}")

    section = re.search(r"(?ms)^\[project\][ \t]*\n(.*?)(?=^\[|\Z)", project)
    project_versions = re.findall(
        r"^version[ \t]*=[ \t]*[\"']([^\"']+)[\"']",
        section[1] if section else "",
        re.MULTILINE,
    )
    if project_versions != [version]:
        parser.error("pyproject.toml version does not match metadata.yaml")
    locked_versions = []
    for package in re.findall(
        r"(?ms)^\[\[package\]\][ \t]*\n(.*?)(?=^\[\[package\]\]|\Z)", lock
    ):
        if re.search(r'^name = "astrbot-plugin-memoir"$', package, re.MULTILINE):
            locked_versions.extend(
                re.findall(r'^version = "([^"]+)"', package, re.MULTILINE)
            )
    if locked_versions != [version]:
        parser.error("uv.lock plugin version is stale; run uv lock")

    sections, body, active, fence = [], [], False, ""
    for line in changelog.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            if active:
                body.append(line)
            continue
        if not fence and (line.startswith("## ") or re.match(r"^\[[^\]]+\]:\s+", line)):
            if active:
                sections.append("\n".join(body).strip())
            active = bool(re.match(rf"^## \[{re.escape(version)}\](?:\s|$)", line))
            body = []
            continue
        if active:
            body.append(line)
    if active:
        sections.append("\n".join(body).strip())
    if len(sections) != 1:
        parser.error(f"CHANGELOG.md must contain exactly one ## [{version}] section")
    if not sections[0] or not any(
        line.strip() and not line.startswith("#") for line in sections[0].splitlines()
    ):
        parser.error(f"CHANGELOG.md section [{version}] is empty")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(sections[0] + "\n", encoding="utf-8")
    print(f"Validated v{version}; release notes written to {args.output}")


if __name__ == "__main__":
    main()
