from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


SEMVER_TAG = re.compile(
    r"^v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))(?:-[0-9A-Za-z.-]+)?$"
)


def project_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as handle:
        payload = tomllib.load(handle)
    return str(payload["project"]["version"])


def validate_release(tag: str, pyproject_path: Path, changelog_path: Path) -> str:
    match = SEMVER_TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"release tag must use semantic version format vX.Y.Z: {tag}")
    version = match.group("version")
    configured_version = project_version(pyproject_path)
    if version != configured_version:
        raise ValueError(
            f"tag {tag} does not match pyproject project.version={configured_version}; "
            "prepare the release version before tagging"
        )
    changelog = changelog_path.read_text(encoding="utf-8")
    heading = re.compile(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", re.MULTILINE)
    if heading.search(changelog) is None:
        raise ValueError(
            f"CHANGELOG.md must contain a dated '## [{version}] - YYYY-MM-DD' section before tagging"
        )
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate version and changelog before a Git release")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args()
    version = validate_release(args.tag, args.pyproject, args.changelog)
    print(f"[PASS] release metadata: v{version}")


if __name__ == "__main__":
    main()
