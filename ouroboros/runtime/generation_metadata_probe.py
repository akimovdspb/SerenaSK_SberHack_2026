from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GENERATION_ID = re.compile(r"gen-[A-Za-z0-9_-]{8,128}")
SAFE_FIELDS = (
    "id",
    "model",
    "provider_name",
    "native_tokens_prompt",
    "native_tokens_completion",
    "native_tokens_cached",
    "tokens_prompt",
    "tokens_completion",
    "total_cost",
    "usage",
)


def _secret() -> str:
    value = str(os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not value:
        path = pathlib.Path(
            os.environ.get("OPENROUTER_API_KEY_FILE", "/run/secrets/openai_api_key")
        )
        value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value:
        raise RuntimeError("OpenRouter secret source is invalid")
    return value


def query(generation_id: str, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    if GENERATION_ID.fullmatch(generation_id) is None:
        raise ValueError("generation ID is invalid")
    url = "https://openrouter.ai/api/v1/generation?" + urllib.parse.urlencode({"id": generation_id})
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_secret()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = int(response.status)
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {
            "schema_version": 1,
            "generation_id": generation_id,
            "found": False,
            "status_code": int(exc.code),
        }
    raw_data = payload.get("data") if isinstance(payload, dict) else None
    data = raw_data if isinstance(raw_data, dict) else {}
    safe = {field: data.get(field) for field in SAFE_FIELDS if field in data}
    if str(safe.get("id") or "") != generation_id:
        raise RuntimeError("generation metadata identity differs")
    return {
        "schema_version": 1,
        "generation_id": generation_id,
        "found": True,
        "status_code": status_code,
        "data": safe,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = query(args.generation_id)
    except Exception as exc:
        result = {
            "schema_version": 1,
            "generation_id": args.generation_id,
            "found": False,
            "status_code": 0,
            "error_type": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("found") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
