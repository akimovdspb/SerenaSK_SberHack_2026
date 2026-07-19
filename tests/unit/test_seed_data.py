from __future__ import annotations

import json
import shutil

import pytest

from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from scripts.seed_data import SeedError, build_seed_manifest


def test_seed_manifest_binds_exact_synthetic_basket_without_mutation() -> None:
    report = build_seed_manifest()

    assert report["status"] == "PASS"
    assert report["business_case_count"] == 15
    assert report["product_count"] == 6
    assert report["persona_count"] == 9
    assert report["synthetic"] is True
    assert report["provider_calls"] == 0
    assert report["mutable_records_created"] == 0


def test_seed_manifest_rejects_incomplete_expected_basket(tmp_path) -> None:
    data_dir = tmp_path / "synthetic"
    shutil.copytree(DEFAULT_DATA_DIR, data_dir)
    expected_path = data_dir / "evaluation" / "business_expected.json"
    document = json.loads(expected_path.read_text(encoding="utf-8"))
    document["cases"] = document["cases"][:-1]
    expected_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SeedError, match="expected basket is invalid"):
        build_seed_manifest(data_dir)
