from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any

REFERENCE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "data"
    / "editorial"
    / "copy_quality_references.json"
)
LABEL = "EDITORIAL_REFERENCE_NOT_LIVE_NOT_RELEASE_EVIDENCE"
EXPECTED_DRAFT_HASHES = {
    "editorial_dq01": "de6e508bc7a3f5e7354930eed7e4e2fff29f36db663eea811ea5612434d593e3",
    "editorial_dq03": "a7fc802f219ede01f1aa988abebf9becf9aad94211d0cbc59f266abd6024c08e",
    "editorial_dq06": "ea54beabfa4d6e15cc9a287138bc1be101f0f7e63821db4f3e1e93a43d91fbb2",
    "editorial_dq07": "7c78e57888f896313352a9d6417e4be163255e56bf32b2346cc40d77b35b3dfe",
    "editorial_dq09": "a0c41c5d4a2951175af2a6747017fa0557091ddc4b1a21c4eb5dcdeda743279a",
    "editorial_dq11": "3a19210621e7f978bff2ffd94a3d7aa34040edbdf3c156d5c785ccc975708423",
    "editorial_dq12": "4b787e924cf612c7458eb118a05c84a69436d189ef2c590376356a34d7ca3f85",
}
FORBIDDEN_KEYS = {
    "events",
    "provider_ledger",
    "provenance",
    "receipt",
    "request_id",
    "run_id",
    "task_id",
    "usage",
}


def _document() -> dict[str, Any]:
    value = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value), set())
    return set()


def _hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_seven_editorial_references_are_sanitized_and_exact() -> None:
    document = _document()

    assert document["schema_version"] == 1
    assert document["label"] == LABEL
    assert len(document["references"]) == 7
    assert not (_keys(document) & FORBIDDEN_KEYS)
    serialized = json.dumps(document, ensure_ascii=False)
    assert not re.search(r"\.xlsx\b|workbook|private_workbook", serialized, re.IGNORECASE)
    assert {
        item["reference_id"]: _hash(item["saved_draft"]) for item in document["references"]
    } == EXPECTED_DRAFT_HASHES


def test_editorial_drafts_keep_product_cta_channel_and_length_integrity() -> None:
    for item in _document()["references"]:
        draft = item["saved_draft"]
        brief = item["brief"]
        product_name = (
            "Пульс Выплат"
            if item["custom_product"] is None
            else item["custom_product"]["exact_name"]
        )
        sms = draft["sms"]
        email = draft["email"]
        email_text = " ".join(
            [
                email["subject"],
                email["preheader"],
                email["headline"],
                email["lead"],
                *(section["heading"] for section in email["sections"]),
                *(section["body"] for section in email["sections"]),
            ]
        )

        assert item["label"] == LABEL
        assert sms["cta_url"] == email["cta_url"] == brief["cta_url"]
        assert email["cta_label"] == brief["cta_label"]
        assert product_name in sms["text"]
        assert product_name in email_text
        assert 160 <= len(sms["text"]) <= 300
        assert 2 <= len(email["sections"]) <= 4
        assert 120 <= len(email_text.split()) <= 220
        assert sms["text"] not in email_text
        assert item["custom_product"] is None or len(item["custom_product"]["facts"]) >= 1
