from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path


JAVASCRIPT_TYPES = {"", "application/javascript", "text/javascript", "module"}


class ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._collecting = False
        self._chunks: list[str] = []
        self.inline_scripts: list[str] = []
        self.local_sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        source = attributes.get("src", "")
        script_type = attributes.get("type", "").lower()
        if source:
            if not source.startswith(("http://", "https://", "//")):
                self.local_sources.append(source.split("?", 1)[0])
            return
        self._collecting = script_type in JAVASCRIPT_TYPES
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._collecting:
            self.inline_scripts.append("".join(self._chunks))
            self._collecting = False
            self._chunks = []


def _check_javascript(node: str, source: str, label: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as temporary:
        temporary.write(source)
        temporary.flush()
        result = subprocess.run(
            [node, "--check", temporary.name],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).replace(temporary.name, label)
        raise RuntimeError(f"JavaScript syntax check failed for {label}:\n{details}")


def check_frontend(web_root: Path, node_command: str = "node") -> int:
    node = shutil.which(node_command)
    if node is None:
        raise RuntimeError(f"Node.js executable not found: {node_command}")
    if not web_root.is_dir():
        raise RuntimeError(f"Frontend directory not found: {web_root}")

    checked = 0
    for javascript_file in sorted(web_root.rglob("*.js")):
        _check_javascript(node, javascript_file.read_text(encoding="utf-8"), str(javascript_file))
        checked += 1

    for html_file in sorted(web_root.rglob("*.html")):
        collector = ScriptCollector()
        collector.feed(html_file.read_text(encoding="utf-8"))
        for source in collector.local_sources:
            source_path = (html_file.parent / source).resolve()
            if not source_path.is_file():
                raise RuntimeError(f"Missing local script referenced by {html_file}: {source}")
        for index, script in enumerate(collector.inline_scripts, start=1):
            _check_javascript(node, script, f"{html_file}#inline-script-{index}")
            checked += 1

    if checked == 0:
        raise RuntimeError(f"No JavaScript found under {web_root}")
    return checked


def main() -> None:
    parser = argparse.ArgumentParser(description="Check static and inline frontend JavaScript syntax")
    parser.add_argument("--web-root", type=Path, default=Path("web"))
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    checked = check_frontend(args.web_root, args.node)
    print(f"[PASS] JavaScript syntax: {checked} script block(s)")


if __name__ == "__main__":
    main()
