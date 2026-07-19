# ruff: noqa: RUF001 -- assertions intentionally match reviewed Russian prompt text.
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from scripts import skill_contract


def test_canonical_skill_and_projection_are_byte_equal() -> None:
    contract = skill_contract.validate_contract()

    assert contract.manifest["name"] == "communication_factory"
    assert contract.manifest["type"] == "instruction"
    assert contract.body.startswith("COMMUNICATION_FACTORY_CONTRACT_V1\n")
    assert skill_contract.PROJECTION_PATH.read_bytes() == contract.body_bytes
    assert contract.body_bytes.endswith(b"\n")
    assert not contract.body_bytes.endswith(b"\n\n")


def test_generation_contract_contains_one_pass_grounding_preflight() -> None:
    contract = skill_contract.load_contract()
    body = contract.body

    assert contract.manifest["timeout_sec"] == 300
    assert "# Проверка перед сохранением" in body
    assert "Ошибка схемы или QA — конечный исход" in body
    assert "mcp_factory__cf_context_get` ровно один раз" in body
    assert "mcp_factory__cf_draft_save` ровно один раз" in body
    assert "повторяй сохранение" in body
    assert "не повторяй его" in body
    assert "второй генеративный, исправляющий или проверяющий проход" in body
    assert "content_plan.channel_selected_fact_ids[channel]" in body
    assert "content_plan.selected_fact_ids` как совместимый" in body
    assert "его не нужно целиком повторять в каждом канале" in body
    assert "| Вид факта | SMS | E-mail |" in body
    assert "в секции URL запрещён" in body
    assert "`canonical_text` URL-факта не выводи" in body
    assert "не более 201 кодовой единицы" in body
    assert "140–190 кодовых единиц" in body
    assert "Не используй эмодзи" in body
    assert "не превратился в новое продуктовое утверждение" in body
    assert "устранит сверку" in body
    assert "`/sms/text` соблюдает `maxLength`" in body
    assert "160–220 символов" not in body
    assert "120–220 слов" in body
    assert "всегда от двух до четырёх" in body
    assert "не расширенная копия SMS" in body
    assert "Одна секция может содержать" in body
    assert "`canonical_text` или" in body and "`allowed_surface_forms`" in body
    assert "`normalized_value`" in body and "`artifact_path`" in body
    assert "точную подстроку" in body
    assert "имеет одну запись" in body and "`claim_evidence` на каждый путь" in body
    assert "точное название продукта" in body
    assert "Они не доказывают свойства продукта" in body
    assert "`CHANNEL_CONSENT_BLOCKED`" in body
    assert "соблюдай `minLength`, `maxLength`" in body
    assert "`minItems` и `maxItems`" in body


def test_projection_tampering_fails_closed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projection = tmp_path / "communication_factory.ru.md"
    projection.write_text("tampered\n", encoding="utf-8")
    monkeypatch.setattr(skill_contract, "PROJECTION_PATH", projection)

    with pytest.raises(ValueError, match="not byte-equal"):
        skill_contract.validate_contract()


def test_prompt_and_model_handoff_are_cross_platform_hash_stable() -> None:
    attributes = (skill_contract.ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "/ouroboros/skills/communication_factory/SKILL.md text eol=lf" in attributes
    assert "/prompts/communication_factory.ru.md text eol=lf" in attributes
    assert "/model_handoff/** text eol=lf" in attributes

    handoff = skill_contract.ROOT / "model_handoff"
    manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
    for relative_path, expected_hash in manifest["checksums_sha256"].items():
        payload = (skill_contract.ROOT / relative_path).read_bytes()
        assert b"\r\n" not in payload, relative_path
        assert hashlib.sha256(payload).hexdigest() == expected_hash, relative_path

    for case_id in ("B02", "B04", "B07", "B08", "B14"):
        expected = json.loads(
            (handoff / "expected" / f"{case_id}.json").read_text(encoding="utf-8")
        )
        assert all(
            fact["placement"]["forbidden_everywhere_else"] is True
            for fact in expected["selected_facts"]
        )
