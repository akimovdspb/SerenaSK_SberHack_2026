from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from datetime import UTC, datetime
from typing import Any

from apps.api.app.services.catalog import DEFAULT_DATA_DIR, load_catalog
from scripts.evaluation import expected_cases

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runtime" / "seed" / "latest.json"


class SeedError(RuntimeError):
    pass


def build_seed_manifest(data_dir: pathlib.Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    catalog = load_catalog(data_dir)
    try:
        expected = expected_cases(data_dir / "evaluation" / "business_expected.json")
    except RuntimeError as exc:
        raise SeedError("synthetic expected basket is invalid") from exc
    case_ids = sorted(catalog.cases)
    exact_case_ids = [f"B{ordinal:02d}" for ordinal in range(1, 16)]
    if case_ids != exact_case_ids or set(expected) != set(case_ids):
        raise SeedError("synthetic catalog and expected basket do not contain exact B01-B15")
    if any(
        case.synthetic is not True
        or case.brief.synthetic is not True
        or (case.brief.cta_url is not None and not str(case.brief.cta_url).startswith("https://"))
        for case in catalog.cases.values()
    ):
        raise SeedError("synthetic catalog flags or CTA URLs are invalid")
    files = sorted(item for item in data_dir.rglob("*.json") if item.is_file())
    hashes = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "seed": catalog.seed,
        "business_case_count": len(case_ids),
        "product_count": len(catalog.products),
        "persona_count": len(catalog.personas),
        "policy_count": len(catalog.contact_policies) + len(catalog.legal_policies),
        "file_hashes": hashes,
        "synthetic": True,
        "provider_calls": 0,
        "mutable_records_created": 0,
    }


def main() -> int:
    try:
        report = build_seed_manifest()
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = REPORT_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, REPORT_PATH)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"seed: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"seed: PASS cases={report['business_case_count']} products={report['product_count']} "
        "synthetic=true provider_calls=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
