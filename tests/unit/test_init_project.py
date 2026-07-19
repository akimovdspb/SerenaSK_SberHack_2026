from __future__ import annotations

import os
import pathlib
import stat

from scripts.init_project import initialize

EXAMPLE = """LOCAL_UID=1000
LOCAL_GID=1000
APP_ACCESS_PASSWORD_HASH=
APP_ACCESS_USERNAME=
MCP_SHARED_TOKEN=
OPENAI_API_KEY=
OPENROUTER_API_KEY=
"""


def test_init_generates_local_secrets_without_provider_key(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")

    credentials_path, created = initialize(tmp_path)
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")

    assert created is True
    assert "APP_ACCESS_USERNAME=cf_" in env_text
    assert "APP_ACCESS_PASSWORD_HASH='$2" in env_text
    assert "MCP_SHARED_TOKEN=" in env_text
    assert "MCP_SHARED_TOKEN=\n" not in env_text
    assert "OPENAI_API_KEY=\n" in env_text
    assert "OPENROUTER_API_KEY=\n" in env_text
    assert credentials_path.read_text(encoding="utf-8").count("\n") == 2
    assert (tmp_path / "artifacts" / "evidence").is_dir()
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
        assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600


def test_init_is_idempotent_and_does_not_rotate(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    credentials_path, _ = initialize(tmp_path)
    first_env = (tmp_path / ".env").read_bytes()
    first_credentials = credentials_path.read_bytes()

    _, created = initialize(tmp_path)

    assert created is False
    assert (tmp_path / ".env").read_bytes() == first_env
    assert credentials_path.read_bytes() == first_credentials
