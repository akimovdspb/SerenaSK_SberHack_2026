from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from scripts.preflight import (
    PreflightError,
    validate_git_worktree,
    validate_host_environment,
    validate_key_source,
    validate_local_environment,
    validate_no_repo_secret_copies,
)


def _private_file(path: pathlib.Path, content: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_secret_preflight_accepts_external_single_line_source(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    key = _private_file(tmp_path / "secrets" / "key.txt", "synthetic-secret\n")

    validate_key_source(key, root=root)


def test_secret_preflight_rejects_repo_copy_and_host_environment(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _private_file(root / "OPENAI_API_KEY.txt", "synthetic-secret\n")

    with pytest.raises(PreflightError, match="inside the checkout"):
        validate_no_repo_secret_copies(root)
    with pytest.raises(PreflightError, match="host process"):
        validate_host_environment({"OPENAI_API_KEY": "synthetic-secret"})


def test_local_environment_requires_empty_provider_values(tmp_path: pathlib.Path) -> None:
    path = _private_file(
        tmp_path / ".env",
        "\n".join(
            (
                "APP_ACCESS_USERNAME=user",
                "APP_ACCESS_PASSWORD_HASH=hash",
                "MCP_SHARED_TOKEN=token",
                "OPENAI_API_KEY=must-not-be-here",
                "OPENAI_API_KEY_HOST_PATH=/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt",
                "OPERATOR_LIMITS_HOST_PATH=/home/dmitry/secrets/communication-factory/operator-limits.yaml",
            )
        )
        + "\n",
    )

    with pytest.raises(PreflightError, match="must not be stored"):
        validate_local_environment(path)


def test_private_inputs_require_exact_mode_0600(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    key = _private_file(tmp_path / "secrets" / "key.txt", "synthetic-secret\n")
    os.chmod(key, 0o640)

    with pytest.raises(PreflightError, match="0600"):
        validate_key_source(key, root=root)


def test_git_preflight_accepts_linked_worktree(tmp_path: pathlib.Path) -> None:
    repository = tmp_path / "repository"
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-q", str(worktree)],
        check=True,
    )

    assert (worktree / ".git").is_file()
    validate_git_worktree(worktree)


def test_git_preflight_rejects_non_repository(tmp_path: pathlib.Path) -> None:
    with pytest.raises(PreflightError, match="not a Git repository"):
        validate_git_worktree(tmp_path)
