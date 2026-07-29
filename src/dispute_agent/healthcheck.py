from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from sqlalchemy import text

from dispute_agent.config import get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.workflow_queue import RQWorkflowQueue


def api_health(url: str) -> tuple[bool, dict]:
    try:
        with urlopen(url, timeout=5) as response:  # noqa: S310 - fixed container-local URL
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200, payload
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return False, {"status": "unavailable", "error": type(exc).__name__}


def worker_health() -> tuple[bool, dict]:
    settings = get_settings()
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        database = {"status": "ok"}
    except Exception as exc:
        database = {"status": "unavailable", "error": type(exc).__name__}
    workers = RQWorkflowQueue(settings).worker_health()
    healthy = database["status"] == "ok" and workers["status"] == "ok"
    return healthy, {
        "status": "ok" if healthy else "unavailable",
        "database": database,
        "workers": workers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Container health probes for the Xianyu deployment")
    parser.add_argument("component", choices=("api", "worker"))
    parser.add_argument("--url", default="http://127.0.0.1:8000/ready")
    args = parser.parse_args()
    healthy, payload = api_health(args.url) if args.component == "api" else worker_health()
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
