from __future__ import annotations

import importlib.util
import os
import pathlib

CONTRACT_PROBE_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "runtime" / "contract_probe.py"
)


def _load_contract_probe():
    spec = importlib.util.spec_from_file_location("cf_contract_probe", CONTRACT_PROBE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load contract probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_system_message_text_preserves_multiline_constraints() -> None:
    probe = _load_contract_probe()
    body = "COMMUNICATION_FACTORY_CONTRACT_V1\n\n# Правила\n\n- Только два инструмента."

    assert probe.message_content_text({"role": "system", "content": body}) == body
    assert body not in probe.canonical_json([{"role": "system", "content": body}]).decode()


def test_system_message_text_supports_structured_text_parts() -> None:
    probe = _load_contract_probe()

    assert (
        probe.message_content_text(
            {"content": [{"type": "text", "text": "first"}, {"text": "second"}]}
        )
        == "first\nsecond"
    )


def test_runtime_context_decoder_recovers_exact_multiline_constraints() -> None:
    probe = _load_contract_probe()
    body = "COMMUNICATION_FACTORY_CONTRACT_V1\n\n# Правила\n\n- Только два инструмента."
    runtime_context = {
        "task_contract": {"constraints": body},
        "operational_reality_rule": "authoritative",
    }
    system_text = (
        "base prompt\n\n"
        + probe.RUNTIME_CONTEXT_HEADER
        + probe.canonical_json(runtime_context).decode("utf-8")
        + "\n\n## Following section\n\ncontent"
    )

    decoded = probe.decoded_runtime_context(system_text)

    assert decoded["task_contract"]["constraints"] == body
    assert system_text.count(probe.MARKER) == 1


def test_runtime_context_decoder_rejects_ambiguous_sections() -> None:
    probe = _load_contract_probe()
    system_text = probe.RUNTIME_CONTEXT_HEADER + "{}\n\n" + probe.RUNTIME_CONTEXT_HEADER + "{}"

    try:
        probe.decoded_runtime_context(system_text)
    except RuntimeError as exc:
        assert str(exc) == "first provider system message must contain one runtime context"
    else:
        raise AssertionError("duplicate runtime context was accepted")


def test_admission_projections_are_ordered_and_secret_free() -> None:
    probe = _load_contract_probe()
    extensions = probe.extension_admission_projection(
        [
            {
                "name": "zeta",
                "type": "extension",
                "version": "1",
                "enabled": False,
                "review_status": "pending",
                "source": "native",
                "secret": "not-copied",
            },
            {
                "name": "alpha",
                "type": "instruction",
                "version": "1",
                "enabled": True,
                "review_status": "clean",
                "executable_review": True,
                "source": "user_repo",
            },
        ]
    )
    mcp = probe.mcp_admission_projection(
        {
            "enabled": True,
            "sdk_available": True,
            "tool_timeout_sec": 5,
            "servers": [
                {
                    "id": "factory",
                    "name": "Factory",
                    "enabled": True,
                    "transport": "streamable_http",
                    "url": "http://app:8000/internal/mcp",
                    "auth_configured": True,
                    "auth_token": "not-copied",
                    "tools": [
                        {"name": "second", "prefixed_name": "mcp_factory__second"},
                        {"name": "first", "prefixed_name": "mcp_factory__first"},
                    ],
                }
            ],
        }
    )

    assert [item["name"] for item in extensions] == ["alpha", "zeta"]
    assert "secret" not in probe.canonical_json(extensions).decode()
    assert [item["name"] for item in mcp["servers"][0]["tools"]] == ["first", "second"]
    assert "auth_token" not in probe.canonical_json(mcp).decode()


def test_atomic_contract_lock_is_group_readable_for_non_root_app(
    tmp_path: pathlib.Path,
) -> None:
    probe = _load_contract_probe()
    path = tmp_path / "communication_factory.lock.json"

    probe.atomic_write_lock(path, {"schema_version": 1})

    assert path.read_text(encoding="utf-8").endswith("\n")
    assert os.stat(path).st_mode & 0o777 == 0o640
