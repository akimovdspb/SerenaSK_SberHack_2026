from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.settings import Settings


def _settings(tmp_path: pathlib.Path, *, enabled: bool) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'factory.db'}",
        ARTIFACTS_DIR=tmp_path / "artifacts",
        EVIDENCE_DIR=tmp_path / "evidence",
        SYNTHETIC_DATA_DIR=DEFAULT_DATA_DIR,
        MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
        DEMO_RESET_ENABLED=enabled,
    )


def _headers(ordinal: int) -> dict[str, str]:
    return {
        "Idempotency-Key": f"demo-reset-integration-{ordinal:04d}",
        "X-CF-Actor": "demo_operator",
        "X-CF-Actor-Role": "human",
    }


def test_hosted_demo_reset_clears_mutable_state_and_preserves_catalog(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path, enabled=True))
    with TestClient(app) as client:
        campaign = client.post(
            "/api/v1/campaigns",
            json={"case_id": "B04"},
            headers={"Idempotency-Key": "demo-reset-campaign-0001"},
        ).json()
        client.post(
            f"/api/v1/campaigns/{campaign['campaign_id']}/validate",
            headers={"Idempotency-Key": "demo-reset-validate-0001"},
        )
        client.post(
            f"/api/v1/campaigns/{campaign['campaign_id']}/runs",
            json={"mode": "deterministic_template"},
            headers={"Idempotency-Key": "demo-reset-run-00000001"},
        )

        reset = client.post(
            "/api/v1/admin/demo-reset",
            json={"confirmation": "СБРОСИТЬ ДЕМО"},
            headers=_headers(1),
        )
        dashboard = client.get("/api/v1/dashboard")

        assert reset.status_code == 200
        assert reset.json()["status"] == "RESET"
        assert reset.json()["catalog_case_count"] == 15
        assert reset.json()["provider_calls"] == 0
        assert dashboard.json()["metrics"]["observed_case_count"] == 0
        assert len(client.get("/api/v1/cases").json()) == 15


def test_demo_reset_is_hidden_when_profile_does_not_enable_it(tmp_path: pathlib.Path) -> None:
    app = create_app(_settings(tmp_path, enabled=False))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/demo-reset",
            json={"confirmation": "СБРОСИТЬ ДЕМО"},
            headers=_headers(2),
        )

        assert response.status_code == 404
