from __future__ import annotations

from scripts.chaos_cases import run_chaos_suite


def test_x01_x05_have_bounded_separate_controlled_outcomes() -> None:
    report = run_chaos_suite()

    assert report["status"] == "PASS"
    assert report["chaos_case_count"] == 5
    assert report["passed_case_count"] == 5
    assert report["provider_calls"] == 0
    assert report["normal_metrics_included"] is False
    cases = {item["case_id"]: item for item in report["cases"]}
    assert set(cases) == {f"X{ordinal:02d}" for ordinal in range(1, 6)}
    assert all(item["under_30_seconds"] for item in cases.values())
    assert all(item["passed"] for item in cases.values())
    assert cases["X01"]["outcome"] == "ADMISSION_REJECTED"
    assert cases["X02"]["outcome"] == "MALFORMED_PAYLOAD_REJECTED"
    assert cases["X03"]["mode"] == "deterministic_template"
    assert cases["X04"]["assertions"]["operation_executed_once"] is True
    assert cases["X05"]["assertions"]["no_active_run_remains"] is True
