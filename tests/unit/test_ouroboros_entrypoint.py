from __future__ import annotations

import importlib.util
import pathlib

import pytest

ENTRYPOINT_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "runtime" / "entrypoint.py"
)


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("cf_ouroboros_entrypoint", ENTRYPOINT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Ouroboros entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_repo_is_copied_to_an_empty_ephemeral_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()
    source = tmp_path / "source"
    target = tmp_path / "runtime"
    source.mkdir()
    target.mkdir()
    (source / "server.py").write_text("# pinned runtime\n", encoding="utf-8")
    (source / "ouroboros").mkdir()
    (source / "ouroboros" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    prepared = entrypoint._prepare_runtime_repo(source=source, target=target)

    assert prepared == target
    assert (target / "server.py").read_text(encoding="utf-8") == "# pinned runtime\n"
    assert pathlib.Path.cwd() == target
    assert entrypoint.os.environ["OUROBOROS_REPO_DIR"] == str(target)
    assert entrypoint.os.environ["PYTHONPATH"] == str(target)


def test_runtime_repo_rejects_persistent_or_stale_contents(
    tmp_path: pathlib.Path,
) -> None:
    entrypoint = _load_entrypoint()
    source = tmp_path / "source"
    target = tmp_path / "runtime"
    source.mkdir()
    target.mkdir()
    (source / "server.py").write_text("# pinned runtime\n", encoding="utf-8")
    (target / "stale.py").write_text("# drift\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not empty"):
        entrypoint._prepare_runtime_repo(source=source, target=target)


def test_openrouter_credential_can_arrive_via_environment_and_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-provider-value")
    monkeypatch.delenv("OPENROUTER_API_KEY_FILE", raising=False)

    env_name, value = entrypoint._read_provider_key()

    assert env_name == "OPENROUTER_API_KEY"
    assert value == "synthetic-test-provider-value"
    assert "OPENROUTER_API_KEY" not in entrypoint.os.environ


def test_openai_credential_file_path_remains_supported(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()
    key_path = tmp_path / "provider-key"
    key_path.write_text("synthetic-test-provider-value\n", encoding="utf-8")
    monkeypatch.setenv("CF_RUNTIME_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(key_path))

    env_name, value = entrypoint._read_provider_key()

    assert env_name == "OPENAI_API_KEY"
    assert value == "synthetic-test-provider-value"


def test_privilege_drop_preserves_only_the_configured_ledger_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()
    calls: list[tuple[str, object]] = []
    monkeypatch.setenv("CF_REQUEST_LEDGER_PATH", "/accounting/request-ledger.json")
    monkeypatch.setenv("CF_REQUEST_LEDGER_GID", "1234")
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entrypoint.os, "setgroups", lambda value: calls.append(("groups", value)))
    monkeypatch.setattr(entrypoint.os, "setgid", lambda value: calls.append(("gid", value)))
    monkeypatch.setattr(entrypoint.os, "setuid", lambda value: calls.append(("uid", value)))

    entrypoint._drop_privileges()

    assert calls == [
        ("groups", [1234]),
        ("gid", entrypoint.RUNTIME_GID),
        ("uid", entrypoint.RUNTIME_UID),
    ]


def test_default_privilege_drop_clears_all_supplemental_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()
    calls: list[list[int]] = []
    monkeypatch.delenv("CF_REQUEST_LEDGER_PATH", raising=False)
    monkeypatch.delenv("CF_REQUEST_LEDGER_GID", raising=False)
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entrypoint.os, "setgroups", calls.append)
    monkeypatch.setattr(entrypoint.os, "setgid", lambda _value: None)
    monkeypatch.setattr(entrypoint.os, "setuid", lambda _value: None)

    entrypoint._drop_privileges()

    assert calls == [[]]


def test_privilege_drop_rejects_an_invalid_ledger_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_entrypoint()
    monkeypatch.setenv("CF_REQUEST_LEDGER_PATH", "/accounting/request-ledger.json")
    monkeypatch.setenv("CF_REQUEST_LEDGER_GID", "invalid")
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)

    with pytest.raises(RuntimeError, match="ledger group"):
        entrypoint._drop_privileges()
