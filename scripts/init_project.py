from __future__ import annotations

import os
import pathlib
import secrets
import stat

import bcrypt

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
ENV_PATH = ROOT / ".env"
RUNTIME_ROOT = ROOT / "runtime"
CREDENTIALS_PATH = RUNTIME_ROOT / "operator" / "access.txt"


def _replace_value(source: str, key: str, value: str, *, quoted: bool = False) -> str:
    prefix = f"{key}="
    replacement = f"{prefix}'{value}'" if quoted else f"{prefix}{value}"
    lines = source.splitlines()
    matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(matches) != 1:
        raise RuntimeError(f"{ENV_EXAMPLE.name} must contain exactly one {prefix} entry")
    lines[matches[0]] = replacement
    return "\n".join(lines) + "\n"


def initialize(root: pathlib.Path = ROOT) -> tuple[pathlib.Path, bool]:
    env_example = root / ".env.example"
    env_path = root / ".env"
    runtime_root = root / "runtime"
    credentials_path = runtime_root / "operator" / "access.txt"
    (runtime_root / "contracts").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "evidence").mkdir(parents=True, exist_ok=True)
    credentials_path.parent.mkdir(parents=True, exist_ok=True)

    if env_path.exists():
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
        return credentials_path, False

    username = f"cf_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    mcp_token = secrets.token_urlsafe(48)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()

    source = env_example.read_text(encoding="utf-8")
    source = _replace_value(source, "LOCAL_UID", str(os.getuid()))
    source = _replace_value(source, "LOCAL_GID", str(os.getgid()))
    source = _replace_value(source, "APP_ACCESS_USERNAME", username)
    source = _replace_value(source, "APP_ACCESS_PASSWORD_HASH", password_hash, quoted=True)
    source = _replace_value(source, "MCP_SHARED_TOKEN", mcp_token)
    env_path.write_text(source, encoding="utf-8")
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)

    credentials_path.write_text(
        f"username={username}\npassword={password}\n",
        encoding="utf-8",
    )
    os.chmod(credentials_path, stat.S_IRUSR | stat.S_IWUSR)
    return credentials_path, True


def main() -> int:
    credentials_path, created = initialize()
    state = "created" if created else "preserved"
    print(f"init: PASS configuration={state} access_credentials={credentials_path}")
    if not credentials_path.exists():
        print("init: NOTE access credentials are not recoverable from the stored hash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
