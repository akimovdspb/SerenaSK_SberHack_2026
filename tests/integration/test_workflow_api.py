from __future__ import annotations

import io
import pathlib
import zipfile

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.services.catalog import DEFAULT_DATA_DIR, load_catalog
from apps.api.app.settings import Settings


def _settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'factory.db'}",
        ARTIFACTS_DIR=tmp_path / "artifacts",
        SYNTHETIC_DATA_DIR=DEFAULT_DATA_DIR,
        MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
    )


def _key(ordinal: int) -> dict[str, str]:
    return {"Idempotency-Key": f"integration-idempotency-{ordinal:04d}"}


def _human(ordinal: int) -> dict[str, str]:
    return {
        **_key(ordinal),
        "X-CF-Actor": "api_test_editor",
        "X-CF-Actor-Role": "human",
    }


def test_versioned_api_flow_idempotency_human_approval_and_export(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/v1/ready").json()["synthetic_case_count"] == 15
        cases = client.get("/api/v1/cases")
        assert cases.status_code == 200
        assert {item["case_id"] for item in cases.json()} == {
            f"B{ordinal:02d}" for ordinal in range(1, 16)
        }

        created = client.post(
            "/api/v1/campaigns",
            json={"case_id": "B04"},
            headers=_key(1),
        )
        replay = client.post(
            "/api/v1/campaigns",
            json={"case_id": "B04"},
            headers=_key(1),
        )
        conflict = client.post(
            "/api/v1/campaigns",
            json={"case_id": "B06"},
            headers=_key(1),
        )

        assert created.status_code == 201
        assert replay.json()["campaign_id"] == created.json()["campaign_id"]
        assert conflict.status_code == 409
        campaign_id = created.json()["campaign_id"]

        validated = client.post(
            f"/api/v1/campaigns/{campaign_id}/validate",
            headers=_key(2),
        )
        assert validated.status_code == 200
        assert validated.json()["state"] == "READY"
        assert (
            validated.json()["ready_brief"]["input_hash"] == validated.json()["draft"]["input_hash"]
        )

        package_response = client.post(
            f"/api/v1/campaigns/{campaign_id}/runs",
            json={"mode": "deterministic_template"},
            headers=_key(3),
        )
        assert package_response.status_code == 201
        package = package_response.json()
        assert package["quality_report"]["approvable"] is True
        assert len(package["quality_report"]["checked_ids"]) == 22
        package_id = package["package_id"]

        premature_export = client.post(
            f"/api/v1/packages/{package_id}/export",
            headers=_human(4),
        )
        denied_actor = client.post(
            f"/api/v1/packages/{package_id}/approve",
            json={
                "package_hash": package["package_hash"],
                "decision": "APPROVED",
                "test_only": True,
            },
            headers={**_key(5), "X-CF-Actor": "api_agent", "X-CF-Actor-Role": "agent"},
        )
        assert premature_export.status_code == 409
        assert denied_actor.status_code == 403

        approval = client.post(
            f"/api/v1/packages/{package_id}/approve",
            json={
                "package_hash": package["package_hash"],
                "decision": "APPROVED",
                "acknowledged_warning_ids": [],
                "test_only": True,
            },
            headers=_human(6),
        )
        assert approval.status_code == 200
        assert approval.json()["actor_role"] == "human"
        assert approval.json()["test_only"] is True

        exported = client.post(
            f"/api/v1/packages/{package_id}/export",
            headers=_human(7),
        )
        assert exported.status_code == 201
        export_id = exported.json()["export_id"]
        metadata = client.get(f"/api/v1/exports/{export_id}")
        download = client.get(f"/api/v1/exports/{export_id}/download")

        assert metadata.status_code == 200
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert "manifest.json" in archive.namelist()
            assert "SYNTHETIC · NO SEND" in archive.read("README.txt").decode()


def test_incomplete_and_blocked_api_states_do_not_start_generation(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    catalog = load_catalog()
    brief = catalog.case("B04").brief.model_dump(mode="json")
    brief.update({"cta_label": None, "cta_url": None})
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/campaigns",
            json={"brief": brief},
            headers=_key(10),
        )
        assert created.status_code == 201
        campaign_id = created.json()["campaign_id"]
        incomplete = client.post(
            f"/api/v1/campaigns/{campaign_id}/validate",
            headers=_key(11),
        )

        assert incomplete.status_code == 200
        assert incomplete.json()["state"] == "NEEDS_INPUT"
        assert incomplete.json()["validation"]["llm_calls"] == 0
        assert (
            client.post(
                f"/api/v1/campaigns/{campaign_id}/runs",
                json={},
                headers=_key(12),
            ).status_code
            == 409
        )

        answered = client.post(
            f"/api/v1/campaigns/{campaign_id}/answers",
            json={
                "cta_label": "Посмотреть детали",
                "cta_url": "https://flow.example.test/term-14",
            },
            headers=_key(13),
        )
        assert answered.status_code == 200
        assert answered.json()["state"] == "READY"
        assert answered.json()["draft_version"] == 2

        blocked = client.post(
            "/api/v1/campaigns",
            json={"case_id": "B11"},
            headers=_key(14),
        )
        blocked_id = blocked.json()["campaign_id"]
        blocked_validation = client.post(
            f"/api/v1/campaigns/{blocked_id}/validate",
            headers=_key(15),
        )
        blocked_run = client.post(
            f"/api/v1/campaigns/{blocked_id}/runs",
            json={},
            headers=_key(16),
        )

        assert blocked_validation.json()["state"] == "BLOCKED"
        assert blocked_validation.json()["validation"]["llm_calls"] == 0
        assert blocked_run.status_code == 409


def test_email_only_consent_creates_explicit_sms_suppression(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        campaign = client.post(
            "/api/v1/campaigns",
            json={"case_id": "B09"},
            headers=_key(30),
        ).json()
        validated = client.post(
            f"/api/v1/campaigns/{campaign['campaign_id']}/validate",
            headers=_key(31),
        )
        package = client.post(
            f"/api/v1/campaigns/{campaign['campaign_id']}/runs",
            json={"mode": "deterministic_template"},
            headers=_key(32),
        )

        assert validated.json()["state"] == "READY"
        assert package.status_code == 201
        assert package.json()["bundle"]["sms"] is None
        assert package.json()["bundle"]["email"] is not None
        assert package.json()["bundle"]["channel_suppressions"] == [
            {
                "channel": "sms",
                "reason_code": "CHANNEL_CONSENT_BLOCKED",
                "reason": "Синтетический профиль не разрешает этот канал.",
            }
        ]
