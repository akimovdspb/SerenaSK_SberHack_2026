from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile
import zipfile

from apps.api.app.domain.workflow import ApprovalDecision, ApprovalRequest, CampaignState
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.store import WorkflowStore

EXPECTED_STATES = {
    "B04": CampaignState.READY,
    "B06": CampaignState.READY,
    "B11": CampaignState.BLOCKED,
    "B12": CampaignState.NOT_APPLICABLE,
    "B13": CampaignState.NEEDS_INPUT,
}


def _assert_export(store: WorkflowStore, package_id: str, package_hash: str) -> str:
    approval = store.approve_package(
        package_id,
        ApprovalRequest(
            package_hash=package_hash,
            decision=ApprovalDecision.APPROVED,
            test_only=True,
        ),
        actor_id="gate1_test_actor",
    )
    exported = store.export_package(package_id)
    with zipfile.ZipFile(store.export_path(exported.export_id)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("approval_hash") != approval.approval_hash:
            raise RuntimeError("Gate 1 export approval hash mismatch")
        if manifest.get("synthetic") is not True or manifest.get("no_send") is not True:
            raise RuntimeError("Gate 1 export safety labels are missing")
        for name, expected_hash in manifest.get("files", {}).items():
            actual_hash = hashlib.sha256(archive.read(name)).hexdigest()
            if actual_hash != expected_hash:
                raise RuntimeError(f"Gate 1 export checksum mismatch: {name}")
    return exported.archive_sha256


def run_gate() -> dict[str, object]:
    outcomes: dict[str, str] = {}
    package_count = 0
    export_hash = ""
    with tempfile.TemporaryDirectory(prefix="communication-factory-gate1-") as directory:
        root = pathlib.Path(directory)
        store = WorkflowStore(
            f"sqlite:///{root / 'gate1.db'}",
            data_dir=DEFAULT_DATA_DIR,
            artifacts_dir=root / "artifacts",
        )
        store.initialize()
        for case_id, expected_state in EXPECTED_STATES.items():
            created = store.create_campaign(brief=None, case_id=case_id)
            validated = store.validate_campaign(created.campaign_id)
            if validated.state is not expected_state:
                raise RuntimeError(
                    f"Gate 1 {case_id}: expected {expected_state}, got {validated.state}"
                )
            if validated.validation is None or validated.validation.llm_calls != 0:
                raise RuntimeError(f"Gate 1 {case_id}: validation call accounting is invalid")
            outcomes[case_id] = validated.state.value
            if case_id not in {"B04", "B06"}:
                continue
            package = store.run_deterministic(created.campaign_id)
            package_count += 1
            if not package.quality_report.approvable or package.quality_report.findings:
                raise RuntimeError(f"Gate 1 {case_id}: deterministic package is not approvable")
            if len(package.quality_report.checked_ids) != 22:
                raise RuntimeError(f"Gate 1 {case_id}: QA registry is incomplete")
            if case_id == "B04" and not any(
                evidence.normalized_value == {"value": 14, "unit": "day"}
                for evidence in package.bundle.claim_evidence
            ):
                raise RuntimeError("Gate 1 B04: exact grounded duration evidence is missing")
            if case_id == "B06":
                notice = "Учебное предложение. Условия вымышлены."
                if (
                    package.bundle.sms is None
                    or notice not in package.bundle.sms.text
                    or package.bundle.email is None
                    or notice not in package.bundle.email.plain_text
                ):
                    raise RuntimeError("Gate 1 B06: required synthetic notice is missing")
                export_hash = _assert_export(
                    store,
                    package.package_id,
                    package.package_hash,
                )
    return {
        "status": "PASS",
        "gate": 1,
        "cases": outcomes,
        "deterministic_packages": package_count,
        "qa_checks": 22,
        "provider_calls": 0,
        "test_only_approval": True,
        "export_sha256_present": bool(export_hash),
    }


def main() -> int:
    print(json.dumps(run_gate(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
