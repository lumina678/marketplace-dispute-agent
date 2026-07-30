from __future__ import annotations

import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINTS = ("/health", "/ready", "/worker/health")


def probe(base_url: str, endpoints: tuple[str, ...], timeout_seconds: float) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for endpoint in endpoints:
        url = f"{base_url.rstrip('/')}{endpoint}"
        request = Request(url, headers={"User-Agent": "xianyu-deployment-smoke/1.0"})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
                if response.status != 200 or payload.get("status") != "ok":
                    failures.append(f"{endpoint}: HTTP {response.status}, body={body[:300]}")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            failures.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    return not failures, failures


def wait_until_ready(
    base_url: str,
    endpoints: tuple[str, ...] = DEFAULT_ENDPOINTS,
    *,
    attempts: int = 30,
    interval_seconds: float = 5,
    timeout_seconds: float = 5,
) -> None:
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    for attempt in range(1, attempts + 1):
        healthy, failures = probe(base_url, endpoints, timeout_seconds)
        if healthy:
            print(f"[PASS] deployment smoke test: {base_url} ({', '.join(endpoints)})")
            return
        print(f"[WAIT] attempt {attempt}/{attempts}: {'; '.join(failures)}")
        if attempt < attempts:
            time.sleep(interval_seconds)
    raise RuntimeError(f"deployment did not become ready after {attempts} attempts")


def main() -> None:
    parser = argparse.ArgumentParser(description="Wait for a deployed Xianyu environment to become ready")
    parser.add_argument("base_url")
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--endpoint", action="append", dest="endpoints")
    args = parser.parse_args()
    wait_until_ready(
        args.base_url,
        tuple(args.endpoints or DEFAULT_ENDPOINTS),
        attempts=args.attempts,
        interval_seconds=args.interval,
        timeout_seconds=args.timeout,
    )


if __name__ == "__main__":
    main()
