from __future__ import annotations

import json
import pathlib
import subprocess
import tarfile
import uuid
from typing import IO, Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_KEY_PATH = pathlib.Path("/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt")
IMAGES = (
    "communication-factory/app:local",
    "communication-factory/gateway:local",
    "communication-factory/ouroboros:v6.61.4",
)
FORBIDDEN_PROVIDER_ENV = {
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
}
FORBIDDEN_BASENAMES = {
    ".env",
    "OPENAI_API_KEY.txt",
    "OPENROUTER_API_KEY.txt",
    "operator-limits.local.yaml",
    "operator-limits.yaml",
}


def _run(command: list[str], *, text: bool = True) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=text,
        timeout=120,
        check=False,
    )


def _read_key(path: pathlib.Path) -> bytes:
    raw = path.read_bytes()
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise RuntimeError("provider key source has an invalid shape")
    return lines[0]


def _stream_contains(handle: IO[bytes], needle: bytes) -> bool:
    overlap = b""
    keep = max(0, len(needle) - 1)
    while chunk := handle.read(64 * 1024):
        candidate = overlap + chunk
        if needle in candidate:
            return True
        overlap = candidate[-keep:] if keep else b""
    return False


def _inspect_image(image: str, provider_key: bytes) -> list[str]:
    errors: list[str] = []
    inspected = _run(["docker", "image", "inspect", image])
    if inspected.returncode != 0:
        return [f"image {image} is unavailable"]
    payload = json.loads(inspected.stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        return [f"image {image} inspection shape is invalid"]
    config = payload[0].get("Config") or {}
    for entry in config.get("Env") or []:
        name, _, value = str(entry).partition("=")
        if name in FORBIDDEN_PROVIDER_ENV and value:
            errors.append(f"image {image} embeds a provider credential environment value")
    if provider_key in inspected.stdout.encode("utf-8"):
        errors.append(f"image {image} metadata contains the owner provider credential")

    history = _run(
        ["docker", "image", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image],
        text=False,
    )
    if history.returncode != 0:
        errors.append(f"image {image} history could not be inspected")
    elif provider_key in bytes(history.stdout):
        errors.append(f"image {image} history contains the owner provider credential")
    return errors


def _scan_filesystem(image: str, provider_key: bytes) -> list[str]:
    errors: list[str] = []
    container_name = f"cf-image-scan-{uuid.uuid4().hex}"
    created = _run(
        ["docker", "create", "--name", container_name, "--entrypoint", "/bin/true", image]
    )
    if created.returncode != 0:
        return [f"image {image} filesystem container could not be created"]
    container_id = created.stdout.strip()
    export: subprocess.Popen[bytes] | None = None
    try:
        export = subprocess.Popen(
            ["docker", "export", container_id],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if export.stdout is None:
            return [f"image {image} filesystem stream could not be opened"]
        with tarfile.open(fileobj=export.stdout, mode="r|*") as archive:
            for member in archive:
                name = pathlib.PurePosixPath(member.name)
                if name.name in FORBIDDEN_BASENAMES:
                    errors.append(f"image {image} contains forbidden credential/config filename")
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is not None and _stream_contains(extracted, provider_key):
                    errors.append(
                        f"image {image} filesystem contains the owner provider credential"
                    )
                    export.terminate()
                    break
        return_code = export.wait(timeout=120)
        if return_code != 0 and not errors:
            errors.append(f"image {image} filesystem export failed")
    finally:
        if export is not None and export.poll() is None:
            export.kill()
            export.wait(timeout=10)
        _run(["docker", "rm", "--force", container_id])
    return errors


def scan_images(
    *, key_path: pathlib.Path = DEFAULT_KEY_PATH, images: tuple[str, ...] = IMAGES
) -> list[str]:
    provider_key = _read_key(key_path)
    errors: list[str] = []
    for image in images:
        errors.extend(_inspect_image(image, provider_key))
        errors.extend(_scan_filesystem(image, provider_key))
    provider_key = b""
    return errors


def main() -> int:
    try:
        errors = scan_images()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"image-security: FAIL: {exc}")
        return 1
    if errors:
        for error in errors:
            print(f"image-security: FAIL: {error}")
        return 1
    print(f"image-security: PASS images={len(IMAGES)} history=clean filesystem=clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
