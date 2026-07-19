from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import re
import secrets
import stat
import sys

from scripts.init_project import initialize as initialize_engineering

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCAL_ENV_NAME = ".env.local"
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


class LocalInitializationError(RuntimeError):
    pass


def _write_private(path: pathlib.Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _read_one_line(path: pathlib.Path, *, label: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise LocalInitializationError(f"{label} must be an absolute regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LocalInitializationError(f"{label} is unreadable") from exc
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise LocalInitializationError(f"{label} must contain one trimmed non-empty line")
    try:
        value = lines[0].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalInitializationError(f"{label} must be UTF-8") from exc
    raw = b""
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise LocalInitializationError(f"{label} permissions must be 0600")
    return value


def _default_provider_path() -> pathlib.Path:
    config_root = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
    return (config_root / "communication-factory" / "openrouter_api_key").resolve()


def _provider_path(
    root: pathlib.Path,
    supplied: pathlib.Path | None,
    *,
    allow_prompt: bool,
) -> pathlib.Path:
    default = _default_provider_path()
    candidate = supplied.expanduser() if supplied is not None else None
    if candidate is not None and not candidate.is_absolute():
        raise LocalInitializationError("PROVIDER_KEY_FILE must be an absolute path")
    if candidate is None and default.is_file():
        candidate = default
    if candidate is None:
        if not allow_prompt or not sys.stdin.isatty():
            raise LocalInitializationError(
                "OpenRouter key is missing; run interactively or set PROVIDER_KEY_FILE"
            )
        value = getpass.getpass("OpenRouter API key (input hidden): ")
        if not value or value != value.strip() or "\n" in value or "\r" in value:
            raise LocalInitializationError("OpenRouter key has an invalid shape")
        _write_private(default, value)
        value = ""
        candidate = default
    candidate = candidate.resolve()
    if _inside(candidate, root):
        raise LocalInitializationError("provider key must remain outside the checkout")
    _read_one_line(candidate, label="OpenRouter key file")
    return candidate


def _parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LocalInitializationError(f"{path.name} is unreadable") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise LocalInitializationError("local configuration contains a newline")
    return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'


def _credentials(path: pathlib.Path) -> tuple[str, str]:
    values = _parse_env(path)
    username = values.get("username", "")
    password = values.get("password", "")
    if not USERNAME_PATTERN.fullmatch(username) or not password:
        raise LocalInitializationError("local access credentials are incomplete")
    return username, password


def validate_local_setup(root: pathlib.Path = ROOT) -> dict[str, str]:
    local_env = root / LOCAL_ENV_NAME
    if not local_env.is_file():
        raise LocalInitializationError(f"{LOCAL_ENV_NAME} is missing; run make init")
    values = _parse_env(local_env)
    required = {
        "COMPOSE_PROJECT_NAME",
        "GATEWAY_HOST_PORT",
        "APP_ACCESS_USERNAME",
        "APP_ACCESS_PASSWORD_HOST_PATH",
        "OPENROUTER_API_KEY_HOST_PATH",
    }
    missing = sorted(name for name in required if not values.get(name))
    if missing:
        raise LocalInitializationError(f"{LOCAL_ENV_NAME} is missing {missing[0]}")
    if not PROJECT_PATTERN.fullmatch(values["COMPOSE_PROJECT_NAME"]):
        raise LocalInitializationError("COMPOSE_PROJECT_NAME is invalid")
    try:
        port = int(values["GATEWAY_HOST_PORT"])
    except ValueError as exc:
        raise LocalInitializationError("GATEWAY_HOST_PORT is invalid") from exc
    if not 1 <= port <= 65535:
        raise LocalInitializationError("GATEWAY_HOST_PORT is invalid")
    if not USERNAME_PATTERN.fullmatch(values["APP_ACCESS_USERNAME"]):
        raise LocalInitializationError("APP_ACCESS_USERNAME is invalid")

    password_path = pathlib.Path(values["APP_ACCESS_PASSWORD_HOST_PATH"])
    expected_password_path = (root / "runtime" / "operator" / "password.txt").resolve()
    if password_path.resolve() != expected_password_path or password_path.is_symlink():
        raise LocalInitializationError("application password file path is invalid")
    password = _read_one_line(expected_password_path, label="application password file")
    provider_path = pathlib.Path(values["OPENROUTER_API_KEY_HOST_PATH"])
    if _inside(provider_path, root):
        raise LocalInitializationError("provider key must remain outside the checkout")
    _read_one_line(provider_path, label="OpenRouter key file")
    access_username, access_password = _credentials(root / "runtime" / "operator" / "access.txt")
    if access_username != values["APP_ACCESS_USERNAME"] or access_password != password:
        raise LocalInitializationError("local access files disagree")
    password = ""
    access_password = ""
    return values


def initialize_local(
    root: pathlib.Path = ROOT,
    *,
    provider_key_file: pathlib.Path | None = None,
    allow_prompt: bool = False,
) -> tuple[pathlib.Path, bool]:
    local_env = root / LOCAL_ENV_NAME
    if local_env.exists():
        values = validate_local_setup(root)
        if provider_key_file is not None:
            configured = pathlib.Path(values["OPENROUTER_API_KEY_HOST_PATH"]).resolve()
            if configured != provider_key_file.expanduser().resolve():
                raise LocalInitializationError(
                    "existing local setup uses another provider key file"
                )
        return root / "runtime" / "operator" / "access.txt", False

    provider_path = _provider_path(root, provider_key_file, allow_prompt=allow_prompt)
    engineering_credentials, _ = initialize_engineering(root)
    if engineering_credentials.is_file():
        username, password = _credentials(engineering_credentials)
    else:
        username = f"cf_{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(24)
        _write_private(
            engineering_credentials,
            f"username={username}\npassword={password}\n",
        )

    password_path = root / "runtime" / "operator" / "password.txt"
    _write_private(password_path, password + "\n")
    project_name = os.environ.get("LOCAL_COMPOSE_PROJECT", "communication-factory-local")
    port = os.environ.get("LOCAL_PORT", "8080")
    if not PROJECT_PATTERN.fullmatch(project_name):
        raise LocalInitializationError("LOCAL_COMPOSE_PROJECT is invalid")
    try:
        parsed_port = int(port)
    except ValueError as exc:
        raise LocalInitializationError("LOCAL_PORT is invalid") from exc
    if not 1 <= parsed_port <= 65535:
        raise LocalInitializationError("LOCAL_PORT is invalid")

    local_text = "\n".join(
        (
            "# Generated by make init. It contains paths and non-secret settings only.",
            f"COMPOSE_PROJECT_NAME={project_name}",
            f"GATEWAY_HOST_PORT={parsed_port}",
            f"APP_ACCESS_USERNAME={username}",
            f"APP_ACCESS_PASSWORD_HOST_PATH={_env_value(str(password_path.resolve()))}",
            f"OPENROUTER_API_KEY_HOST_PATH={_env_value(str(provider_path))}",
            "AUTH_COOKIE_SECURE=false",
            "TOTAL_BUDGET=20",
            "OUROBOROS_PER_TASK_COST_USD=2",
            "",
        )
    )
    _write_private(local_env, local_text)
    password = ""
    validate_local_setup(root)
    return engineering_credentials, True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.check:
            validate_local_setup()
            print("local-init: PASS configuration=valid secrets=external")
            return 0
        source = os.environ.get("PROVIDER_KEY_FILE")
        credentials, created = initialize_local(
            provider_key_file=pathlib.Path(source) if source else None,
            allow_prompt=True,
        )
    except (OSError, ValueError, LocalInitializationError, RuntimeError) as exc:
        print(f"local-init: FAIL: {exc}", file=sys.stderr)
        return 1
    state = "created" if created else "preserved"
    print(f"local-init: PASS configuration={state} access_credentials={credentials}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
