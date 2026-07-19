from __future__ import annotations

import os
import pathlib
import stat

import pytest

from scripts import local_init
from scripts.wait_local_ready import local_url

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _private_file(path: pathlib.Path, value: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def _stub_engineering(root: pathlib.Path) -> tuple[pathlib.Path, bool]:
    credentials = root / "runtime" / "operator" / "access.txt"
    _private_file(credentials, "username=local_judge\npassword=local-password\n")
    _private_file(root / ".env", "LEGACY_ENGINEERING_ONLY=true\n")
    return credentials, True


def test_local_init_keeps_provider_value_outside_checkout_and_config(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    provider = _private_file(tmp_path / "secrets" / "openrouter", "synthetic-provider\n")
    monkeypatch.setattr(local_init, "initialize_engineering", _stub_engineering)
    monkeypatch.setenv("LOCAL_COMPOSE_PROJECT", "cf-clean-clone")
    monkeypatch.setenv("LOCAL_PORT", "18080")

    credentials, created = local_init.initialize_local(
        checkout,
        provider_key_file=provider.resolve(),
    )

    assert created is True
    assert credentials == checkout / "runtime" / "operator" / "access.txt"
    local_text = (checkout / ".env.local").read_text(encoding="utf-8")
    assert "synthetic-provider" not in local_text
    assert "local-password" not in local_text
    assert "COMPOSE_PROJECT_NAME=cf-clean-clone" in local_text
    assert "GATEWAY_HOST_PORT=18080" in local_text
    assert provider.resolve().as_posix() in local_text.replace("\\", "/")
    assert (checkout / "runtime" / "operator" / "password.txt").read_text(
        encoding="utf-8"
    ) == "local-password\n"
    assert local_init.validate_local_setup(checkout)["APP_ACCESS_USERNAME"] == "local_judge"
    assert local_url(checkout) == "http://127.0.0.1:18080"

    _, repeated = local_init.initialize_local(
        checkout,
        provider_key_file=provider.resolve(),
    )
    assert repeated is False


def test_local_init_rejects_provider_key_inside_checkout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    provider = _private_file(checkout / "OPENROUTER_API_KEY.txt", "synthetic-provider\n")
    monkeypatch.setattr(local_init, "initialize_engineering", _stub_engineering)

    with pytest.raises(local_init.LocalInitializationError, match="outside the checkout"):
        local_init.initialize_local(checkout, provider_key_file=provider.resolve())


def test_local_compose_uses_file_secrets_and_the_proven_single_image() -> None:
    compose = (ROOT / "compose.local.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "dockerfile: Dockerfile" in compose
    assert "APP_ACCESS_PASSWORD_FILE: /run/cf-inputs/app_access_password" in compose
    assert "OPENROUTER_API_KEY_FILE: /run/cf-inputs/openrouter_api_key" in compose
    assert "AUTH_COOKIE_SECURE: ${AUTH_COOKIE_SECURE:-false}" in compose
    assert "127.0.0.1:${GATEWAY_HOST_PORT:-8080}:8080" in compose
    assert "openrouter::z-ai/glm-5.2" in dockerfile
    assert "APP_ACCESS_PASSWORD:" not in compose
    assert "OPENROUTER_API_KEY:" not in compose
