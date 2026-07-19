from __future__ import annotations

import json

import pytest

from scripts.source_audit import ROOT, audit_source, validate_pinned_source


@pytest.mark.contract
def test_pinned_source_license_and_tracking_boundaries_are_audited() -> None:
    assert audit_source() == []


def test_pinned_source_audit_rejects_lock_identity_drift() -> None:
    lock = json.loads((ROOT / "ouroboros" / "ouroboros.lock").read_text(encoding="utf-8"))
    lock["tag"] = "v9.9.9"

    errors = validate_pinned_source(ROOT, lock)

    assert "pinned Ouroboros tag or commit is not canonical" in errors
    assert "pinned Ouroboros VERSION readback drifted" in errors
