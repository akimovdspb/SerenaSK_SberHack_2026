from __future__ import annotations

import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

from scripts.local_init import LOCAL_ENV_NAME, ROOT, LocalInitializationError, _parse_env


class LocalReadinessError(RuntimeError):
    pass


def local_url(root: pathlib.Path = ROOT) -> str:
    values = _parse_env(root / LOCAL_ENV_NAME)
    port = values.get("GATEWAY_HOST_PORT", "8080")
    try:
        parsed = int(port)
    except ValueError as exc:
        raise LocalReadinessError("local gateway port is invalid") from exc
    if not 1 <= parsed <= 65535:
        raise LocalReadinessError("local gateway port is invalid")
    return f"http://127.0.0.1:{parsed}"


def wait_until_ready(
    url: str,
    *,
    timeout_seconds: float = 1200,
    interval_seconds: float = 3,
) -> float:
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise LocalReadinessError("readiness timing must be positive")
    started = time.monotonic()
    next_notice = started
    while True:
        now = time.monotonic()
        if now - started >= timeout_seconds:
            raise LocalReadinessError(
                f"service did not become ready within {int(timeout_seconds)} seconds"
            )
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=5) as response:
                if response.status == 200:
                    return time.monotonic() - started
        except (OSError, urllib.error.URLError):
            pass
        if now >= next_notice:
            print(f"local-start: waiting for Ouroboros bootstrap at {url}")
            next_notice = now + 15
        time.sleep(interval_seconds)


def main() -> int:
    try:
        timeout = float(os.environ.get("LOCAL_READY_TIMEOUT_SECONDS", "1200"))
        url = local_url()
        elapsed = wait_until_ready(url, timeout_seconds=timeout)
    except (OSError, ValueError, LocalInitializationError, LocalReadinessError) as exc:
        print(f"local-start: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"local-start: PASS url={url} ready_seconds={elapsed:.1f} "
        "credentials=runtime/operator/access.txt"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
