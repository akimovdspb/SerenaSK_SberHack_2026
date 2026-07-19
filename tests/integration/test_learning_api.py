from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.settings import Settings


def _settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'factory.db'}",
        ARTIFACTS_DIR=tmp_path / "artifacts",
        SYNTHETIC_DATA_DIR=DEFAULT_DATA_DIR,
        MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
    )


def _human(ordinal: int) -> dict[str, str]:
    return {
        "Idempotency-Key": f"learning-api-idempotency-{ordinal:04d}",
        "X-CF-Actor": "learning_api_editor",
        "X-CF-Actor-Role": "human",
    }


def test_b01_revision_rule_b03_and_rollback_api_flow(tmp_path: pathlib.Path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/campaigns", json={"case_id": "B01"}, headers=_human(1)
        ).json()
        campaign_id = created["campaign_id"]
        needs_input = client.post(
            f"/api/v1/campaigns/{campaign_id}/validate", headers=_human(2)
        ).json()
        assert needs_input["state"] == "NEEDS_INPUT"

        ready = client.post(
            f"/api/v1/campaigns/{campaign_id}/answers",
            json={
                "cta_label": "Собрать первый реестр",
                "cta_url": "https://pulse-pay.example.test/start",
            },
            headers=_human(3),
        ).json()
        assert ready["state"] == "READY"
        v1 = client.post(
            f"/api/v1/campaigns/{campaign_id}/runs",
            json={"mode": "deterministic_template"},
            headers=_human(4),
        ).json()
        assert "подготовка выплат в онлайн-банке" not in v1["bundle"]["email"]["plain_text"]

        denied_feedback = client.post(
            f"/api/v1/packages/{v1['package_id']}/feedback",
            json={
                "artifact_path": "/email/sections/0/body",
                "comment": "Добавьте payouts_via_online_bank.",
                "scope": "CURRENT_CHANNEL",
                "author_role": "editor",
            },
            headers={
                "Idempotency-Key": "learning-api-idempotency-denied",
                "X-CF-Actor": "learning_api_agent",
                "X-CF-Actor-Role": "agent",
            },
        )
        assert denied_feedback.status_code == 403

        feedback = client.post(
            f"/api/v1/packages/{v1['package_id']}/feedback",
            json={
                "artifact_path": "/email/sections/0/body",
                "comment": "Добавьте payouts_via_online_bank.",
                "scope": "CURRENT_CHANNEL",
                "author_role": "editor",
            },
            headers=_human(5),
        ).json()
        v2_response = client.post(
            f"/api/v1/packages/{v1['package_id']}/revision",
            json={
                "feedback_id": feedback["feedback_id"],
                "mode": "deterministic_template",
            },
            headers=_human(6),
        )
        assert v2_response.status_code == 201
        v2 = v2_response.json()
        diff = client.get(f"/api/v1/packages/{v2['package_id']}/diff").json()
        assert diff["changed_paths"] == [
            "/email/plain_text",
            "/email/sections/0/body",
        ]
        assert v2["quality_report"]["approvable"] is True

        proposal_response = client.post(
            f"/api/v1/feedback/{feedback['feedback_id']}/rule-proposals",
            json={
                "selected_scope": {
                    "product_ids": ["synthetic_payroll"],
                    "channel": "email",
                    "segment_ids": [],
                },
                "mode": "deterministic_template",
            },
            headers=_human(7),
        )
        assert proposal_response.status_code == 201
        proposal = proposal_response.json()
        assert proposal["status"] == "READY_FOR_APPROVAL"
        assert all(item["passed"] for item in proposal["tests"])

        approved = client.post(
            f"/api/v1/rule-proposals/{proposal['proposal_id']}/approve",
            json={
                "candidate_rules_version": proposal["proposal"]["candidate_rules_version"],
                "test_only": True,
            },
            headers=_human(8),
        ).json()
        assert approved["active"] is True
        assert approved["test_only"] is True

        b03 = client.post("/api/v1/campaigns", json={"case_id": "B03"}, headers=_human(9)).json()
        client.post(f"/api/v1/campaigns/{b03['campaign_id']}/validate", headers=_human(10))
        b03_package = client.post(
            f"/api/v1/campaigns/{b03['campaign_id']}/runs",
            json={"mode": "deterministic_template"},
            headers=_human(11),
        ).json()
        assert "подготовка выплат в онлайн-банке" in b03_package["bundle"]["email"]["plain_text"]
        assert "подготовка выплат в онлайн-банке" not in b03_package["bundle"]["sms"]["text"]
        assert b03_package["quality_report"]["findings"] == []

        rollback = client.post(
            f"/api/v1/rules/{approved['rule_version_id']}/rollback",
            json={
                "active_rules_version": approved["rules_version"],
                "reason": "Проверка rollback через API.",
                "test_only": True,
            },
            headers=_human(12),
        )
        assert rollback.status_code == 200
        assert rollback.json()["status"] == "ROLLED_BACK"
        assert rollback.json()["active"] is False
