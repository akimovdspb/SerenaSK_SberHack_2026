from __future__ import annotations

import hashlib
import json
import pathlib
import zipfile

from scripts.security_scan import (
    PHONE_RE,
    scan_dependency_and_license_files_at,
    scan_generated_artifacts,
    scan_synthetic_data,
    secret_value_present,
)


def test_secret_detector_catches_value_without_storing_it_in_report() -> None:
    synthetic_secret = "sk-" + "A" * 40

    assert secret_value_present(synthetic_secret) is True
    assert secret_value_present("OPENAI_API_KEY=") is False


def test_synthetic_and_artifact_scans_report_only_safe_locations(tmp_path: pathlib.Path) -> None:
    synthetic = tmp_path / "synthetic"
    synthetic.mkdir()
    (synthetic / "case.json").write_text(
        '{"url":"https://safe.example.test/path","synthetic":true}',
        encoding="utf-8",
    )
    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    (artifact / "safe.json").write_text('{"status":"PASS"}', encoding="utf-8")

    assert scan_synthetic_data(synthetic) == []
    assert scan_generated_artifacts([artifact]) == []

    (artifact / "unsafe.json").write_text(
        '{"contact":"person@example.com"}',
        encoding="utf-8",
    )
    findings = scan_generated_artifacts([artifact])
    assert findings == ["artifact_pii:unsafe.json"]


def test_phone_detector_ignores_machine_ids_but_catches_contact_numbers() -> None:
    assert PHONE_RE.search("a8d1234567890bc") is None
    assert PHONE_RE.search("rv-899912345678") is None
    assert PHONE_RE.search("+79991234567") is not None
    assert PHONE_RE.search("8 (999) 123-45-67") is not None


def test_artifact_scan_inspects_archives_without_extracting_them(tmp_path: pathlib.Path) -> None:
    safe = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("report.json", '{"synthetic":true,"no_send":true}')
    assert scan_generated_artifacts([tmp_path]) == []

    unsafe = tmp_path / "unsafe.zip"
    credential = "Basic " + "Q" * 20
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.json", "{}")
        archive.writestr("trace.json", '{"authorization":"' + credential + '"}')
    findings = scan_generated_artifacts([tmp_path])
    assert "artifact_unsafe_archive_path:unsafe.zip" in findings
    assert "artifact_archive_basic_auth:unsafe.zip" in findings

    machine_root = tmp_path / "machine"
    machine_root.mkdir()
    machine_ref = machine_root / "machine-ref.zip"
    with zipfile.ZipFile(machine_ref, "w") as archive:
        archive.writestr(
            "0-trace.trace",
            '{"type":"screencast-frame","sha1":"hash@resource.jpeg"}\n',
        )
    findings = scan_generated_artifacts([machine_root])
    assert findings == []


def test_dependency_report_must_be_green_and_bound_to_current_inputs(
    tmp_path: pathlib.Path,
) -> None:
    relative_inputs = (
        "uv.lock",
        "package-lock.json",
        "apps/requirements.lock",
        "ouroboros/requirements.lock",
        "data/dependency_license_overrides.json",
    )
    for relative in ("LICENSE", "THIRD_PARTY_NOTICES.md", *relative_inputs):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{relative}\n", encoding="utf-8")
    security_root = tmp_path / "runtime" / "security"
    security_root.mkdir(parents=True)
    (security_root / "THIRD_PARTY_NOTICES.generated.md").write_text("# fixture\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "status": "PASS",
        "failure_count": 0,
        "component_count": 1,
        "input_files": [
            {
                "path": relative,
                "sha256": hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest(),
            }
            for relative in relative_inputs
        ],
    }
    report_path = security_root / "dependencies.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert scan_dependency_and_license_files_at(tmp_path) == []
    (tmp_path / "uv.lock").write_text("changed\n", encoding="utf-8")
    assert scan_dependency_and_license_files_at(tmp_path) == ["dependency_report_inputs_stale"]
