from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_requires_matching_tag_and_dated_changelog(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    pyproject.write_text('[project]\nname = "example"\nversion = "0.2.0"\n', encoding="utf-8")
    changelog.write_text("# Changelog\n\n## [0.2.0] - 2026-07-29\n", encoding="utf-8")

    command = [
        sys.executable,
        "scripts/check_release.py",
        "--pyproject",
        str(pyproject),
        "--changelog",
        str(changelog),
    ]
    valid = subprocess.run(
        [*command, "--tag", "v0.2.0"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr or valid.stdout
    mismatch = subprocess.run(
        [*command, "--tag", "v0.3.0"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mismatch.returncode != 0
    assert "does not match" in mismatch.stderr
    changelog.write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")
    undated = subprocess.run(
        [*command, "--tag", "v0.2.0"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert undated.returncode != 0
    assert "dated" in undated.stderr


def test_ci_and_delivery_workflows_cover_release_gates() -> None:
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    staging = (PROJECT_ROOT / ".github/workflows/publish-staging.yml").read_text(encoding="utf-8")
    production = (PROJECT_ROOT / ".github/workflows/deploy-production.yml").read_text(encoding="utf-8")
    release = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    for required in (
        "python -m compileall",
        "pytest --junitxml",
        "tests/test_async_workflow.py",
        "alembic downgrade base",
        "scripts/check_frontend.py",
        "pip_audit",
        "deploy/docker/api.Dockerfile",
        "deploy/docker/worker.Dockerfile",
    ):
        assert required in ci
    assert "github.event.workflow_run.conclusion == 'success'" in staging
    assert "scripts/smoke_deployment.py" in staging
    assert "environment:\n      name: production" in production
    assert "confirmation" in production
    assert "scripts/check_release.py" in release
    assert "gh release create" in release


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_current_frontend_javascript_passes_node_syntax_check() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_frontend.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_release_compose_override_uses_immutable_image_variables() -> None:
    override = (PROJECT_ROOT / "deploy/compose.release.yaml").read_text(encoding="utf-8")
    assert "XIANYU_API_IMAGE" in override
    assert "XIANYU_WORKER_IMAGE" in override
    deploy_script = PROJECT_ROOT / "deploy/scripts/deploy_release.sh"
    assert deploy_script.stat().st_mode & 0o111


def test_deployment_smoke_script_accepts_healthy_environment() -> None:
    class HealthyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
            if self.path not in {"/health", "/ready", "/worker/health"}:
                self.send_error(404)
                return
            payload = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/smoke_deployment.py",
                f"http://127.0.0.1:{server.server_port}",
                "--attempts",
                "1",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0, result.stderr or result.stdout
