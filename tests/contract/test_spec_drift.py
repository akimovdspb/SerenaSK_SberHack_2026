from __future__ import annotations

import copy
import json

import pytest
import yaml

from scripts.compose_contract import load_rendered_compose
from scripts.spec_drift import ROOT, validate_spec_constants


def _inputs() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    constants = yaml.safe_load((ROOT / "spec_constants.yaml").read_text(encoding="utf-8"))
    runtime_lock = json.loads((ROOT / "ouroboros" / "ouroboros.lock").read_text())
    assert isinstance(constants, dict)
    assert isinstance(runtime_lock, dict)
    return constants, runtime_lock, load_rendered_compose()


@pytest.mark.contract
def test_machine_projection_matches_implementation_constants() -> None:
    constants, runtime_lock, compose = _inputs()

    assert validate_spec_constants(constants, runtime_lock, compose) == []


def test_machine_projection_reports_runtime_and_ledger_drift() -> None:
    constants, runtime_lock, compose = _inputs()
    changed = copy.deepcopy(constants)
    changed["ouroboros"]["baseline_tag"] = "v9.9.9"  # type: ignore[index]
    changed["post_task"]["provider_call_categories"] = []  # type: ignore[index]

    errors = validate_spec_constants(changed, runtime_lock, compose)

    assert "pinned Ouroboros tag drifted from spec_constants" in errors
    assert "provider call ledger categories drifted from spec_constants" in errors
