from __future__ import annotations

import os
import pathlib
import shutil
import sys

RUNTIME_UID = 10001
RUNTIME_GID = 10001
SOURCE_REPO = pathlib.Path("/opt/ouroboros")
PROVIDER_ENV_NAMES = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _supplemental_groups() -> list[int]:
    if not str(os.environ.get("CF_REQUEST_LEDGER_PATH") or "").strip():
        return []
    raw_gid = str(os.environ.get("CF_REQUEST_LEDGER_GID") or "").strip()
    if not raw_gid.isdigit() or int(raw_gid) <= 0:
        raise RuntimeError("request ledger group is invalid")
    return [int(raw_gid)]


def _drop_privileges() -> None:
    if os.geteuid() != 0:
        return
    os.setgroups(_supplemental_groups())
    os.setgid(RUNTIME_GID)
    os.setuid(RUNTIME_UID)


def _read_provider_key() -> tuple[str, str]:
    provider = str(os.environ.get("CF_RUNTIME_PROVIDER") or "openai").strip().lower()
    env_name = PROVIDER_ENV_NAMES.get(provider)
    if env_name is None:
        raise RuntimeError("CF_RUNTIME_PROVIDER must be openai or openrouter")
    file_env = f"{env_name}_FILE"
    configured_path = str(os.environ.get(file_env) or "").strip()
    raw = ""
    if configured_path:
        try:
            raw = pathlib.Path(configured_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("provider credential mount is unavailable") from exc
    else:
        raw = str(os.environ.pop(env_name, ""))
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise RuntimeError("provider credential mount has an invalid shape")
    return env_name, lines[0]


def _prepare_runtime_repo(
    *,
    source: pathlib.Path = SOURCE_REPO,
    target: pathlib.Path | None = None,
) -> pathlib.Path:
    runtime_repo = target or pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", "/runtime-repo"))
    try:
        if not (source / "server.py").is_file():
            raise RuntimeError("pinned runtime source is unavailable")
        if not runtime_repo.is_dir():
            raise RuntimeError("runtime repository tmpfs is unavailable")
        if any(runtime_repo.iterdir()):
            raise RuntimeError("runtime repository tmpfs is not empty")
        shutil.copytree(source, runtime_repo, dirs_exist_ok=True, symlinks=True)
        os.environ["OUROBOROS_REPO_DIR"] = str(runtime_repo)
        os.environ["PYTHONPATH"] = str(runtime_repo)
        os.chdir(runtime_repo)
    except OSError as exc:
        raise RuntimeError("could not prepare the ephemeral runtime repository") from exc
    return runtime_repo


def main() -> int:
    argv = sys.argv[1:] or ["python", "server.py"]
    try:
        if argv == ["contract-probe"]:
            _drop_privileges()
            _prepare_runtime_repo()
            os.execv(
                sys.executable,
                [sys.executable, "/opt/communication-factory/contract_probe.py"],
            )

        provider_env_name, provider_key = _read_provider_key()
        os.environ.pop(provider_env_name, None)
        _drop_privileges()
        _prepare_runtime_repo()
        from configure_runtime import configure_runtime

        configure_runtime(startup=True)
        os.environ[provider_env_name] = provider_key
        provider_key = ""
        os.execvp(argv[0], argv)
    except RuntimeError as exc:
        print(f"ouroboros-entrypoint: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
