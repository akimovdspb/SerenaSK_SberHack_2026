from __future__ import annotations

import argparse
import os
import pathlib
import stat
import subprocess
import sys

from provider_profiles import (
    CANONICAL_PROFILE_NAME,
    ProviderProfileError,
    requested_provider_profile,
)
from scripts.budget_control import BudgetPolicyError, load_operator_profile
from scripts.compose_contract import load_rendered_compose, validate_compose, validate_static_files

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY_PATH = pathlib.Path("/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt")
OPERATOR_LIMITS_PATH = pathlib.Path(
    "/home/dmitry/secrets/communication-factory/operator-limits.yaml"
)
ENV_PATH = ROOT / ".env"
PROVIDER_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
)


class PreflightError(RuntimeError):
    pass


def validate_git_worktree(root: pathlib.Path = ROOT) -> None:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError("workspace is not a Git repository") from exc
    if process.returncode != 0 or process.stdout.strip() != "true":
        raise PreflightError("workspace is not a Git repository")


def _require_private_file(path: pathlib.Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise PreflightError(f"{label} must be a regular file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise PreflightError(f"{label} must have mode 0600")


def validate_key_source(path: pathlib.Path = KEY_PATH, *, root: pathlib.Path = ROOT) -> None:
    _require_private_file(path, label="provider key source")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise PreflightError("provider key source must remain outside the checkout")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PreflightError("provider key source is unreadable") from exc
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise PreflightError("provider key source must contain one trimmed non-empty line")
    raw = b""


def _parse_env(path: pathlib.Path) -> dict[str, str]:
    _require_private_file(path, label="local Compose environment")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreflightError("local Compose environment is unreadable") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value.strip().strip("'").strip('"')
    return values


def validate_local_environment(path: pathlib.Path = ENV_PATH) -> None:
    values = _parse_env(path)
    for key in ("APP_ACCESS_USERNAME", "APP_ACCESS_PASSWORD_HASH", "MCP_SHARED_TOKEN"):
        if not values.get(key):
            raise PreflightError(f"local Compose setting {key} is missing; run make init")
    for key in PROVIDER_ENV_NAMES:
        if values.get(key):
            raise PreflightError(f"provider value {key} must not be stored in .env")
    if values.get("OPENAI_API_KEY_HOST_PATH") != str(KEY_PATH):
        raise PreflightError("provider key mount source is not canonical")
    if values.get("OPERATOR_LIMITS_HOST_PATH") != str(OPERATOR_LIMITS_PATH):
        raise PreflightError("operator limits source is not canonical")


def validate_host_environment(environment: dict[str, str] | None = None) -> None:
    effective = environment if environment is not None else dict(os.environ)
    if any(effective.get(key) for key in PROVIDER_ENV_NAMES):
        raise PreflightError("host process must not receive provider credentials")


def validate_no_repo_secret_copies(root: pathlib.Path = ROOT) -> None:
    forbidden = (
        root / "OPENAI_API_KEY.txt",
        root / "OPENROUTER_API_KEY.txt",
        root / "operator-limits.yaml",
        root / "operator-limits.local.yaml",
    )
    if any(path.exists() for path in forbidden):
        raise PreflightError("credential or operator file exists inside the checkout")


def run_preflight(action: str) -> None:
    if action not in {"up", "bootstrap", "live"}:
        raise PreflightError("unsupported preflight action")
    validate_git_worktree()
    validate_host_environment()
    try:
        selected = requested_provider_profile(
            dict(os.environ),
            default=CANONICAL_PROFILE_NAME,
        )
    except ProviderProfileError as exc:
        raise PreflightError(str(exc)) from exc
    configured_key_path = pathlib.Path(
        os.environ.get("PROVIDER_API_KEY_HOST_PATH", selected.default_secret_host_path)
    )
    configured_container_path = os.environ.get(
        "PROVIDER_API_KEY_CONTAINER_PATH", selected.secret_container_path
    )
    if configured_key_path != pathlib.Path(selected.default_secret_host_path):
        raise PreflightError("provider key mount source does not match the selected profile")
    if configured_container_path != selected.secret_container_path:
        raise PreflightError("provider key mount target does not match the selected profile")
    validate_key_source(configured_key_path)
    validate_local_environment()
    validate_no_repo_secret_copies()
    _require_private_file(OPERATOR_LIMITS_PATH, label="operator limits source")
    try:
        load_operator_profile(OPERATOR_LIMITS_PATH, model="gpt-5.4-mini")
    except BudgetPolicyError as exc:
        raise PreflightError(str(exc)) from exc
    compose_errors = (
        validate_compose(load_rendered_compose(), selected_profile=selected)
        + validate_static_files()
    )
    if compose_errors:
        raise PreflightError(compose_errors[0])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("up", "bootstrap", "live"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_preflight(args.action)
    except (OSError, ValueError, PreflightError) as exc:
        print(f"preflight: FAIL action={args.action}: {exc}", file=sys.stderr)
        return 1
    print(
        f"preflight: PASS action={args.action} secrets=external compose=canonical "
        "operator_profile=valid account_remaining=unknown"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
