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


def _key(value: str) -> dict[str, str]:
    return {"Idempotency-Key": f"authoring-{value}-idempotency-key"}


def _custom_product(*, monthly_price: int = 490) -> dict[str, object]:
    return {
        "exact_name": "Ритм Команды",
        "cta_label": "Собрать сценарий",
        "cta_url": "https://team-rhythm.example.test/start",
        "facts": [
            {
                "label": "Единый план",
                "canonical_text": "Задачи команды собираются в едином плане.",
                "kind": "text",
                "source_label": "Синтетическая карточка продукта",
                "normalized_value": "one_team_plan",
                "allowed_surface_forms": ["Задачи команды — в едином плане."],
            },
            {
                "label": "Цена в месяц",
                "canonical_text": f"Стоимость составляет {monthly_price} ₽ в месяц.",
                "kind": "money",
                "source_label": "Синтетическая тарифная карточка",
                "normalized_value": {"value": monthly_price, "unit": "RUB/month"},
                "allowed_surface_forms": [f"{monthly_price} ₽ в месяц"],
            },
            {
                "label": "Статусы",
                "canonical_text": "Статусы задач обновляются в рабочем пространстве.",
                "kind": "text",
                "source_label": "Синтетическая карточка продукта",
                "normalized_value": "workspace_statuses",
            },
        ],
        "synthetic_confirmed": True,
        "no_pii_confirmed": True,
    }


def test_custom_product_overlay_is_versioned_and_drives_a_normal_campaign(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        before_cases = client.get("/api/v1/cases").json()
        catalog = client.get("/api/v1/authoring/catalog")
        assert catalog.status_code == 200
        assert catalog.json()["no_send"] is True
        assert len(catalog.json()["references"]) == 7
        assert all("saved_draft" not in reference for reference in catalog.json()["references"])
        persona = next(
            item
            for item in catalog.json()["personas"]
            if set(item["available_channels"]) == {"sms", "email"}
        )

        created = client.post(
            "/api/v1/authoring/products",
            json=_custom_product(),
            headers=_key("product-v1"),
        )
        assert created.status_code == 201
        product = created.json()
        assert product["version"] == 1
        assert product["origin"] == "custom"
        assert product["product_id"].endswith("_v1")
        assert len(product["facts"]) == 4
        assert product["facts"][-1]["kind"] == "url"

        duplicate = client.post(
            "/api/v1/authoring/products",
            json=_custom_product(),
            headers=_key("same-product-new-request"),
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["product_id"] == product["product_id"]
        assert duplicate.json()["version"] == 1

        campaign = client.post(
            "/api/v1/campaigns",
            json={
                "brief": {
                    "name": "Возвращение в планирование",
                    "objective": "Помочь команде собрать задачи после паузы.",
                    "product_id": product["product_id"],
                    "segment_id": persona["segment_id"],
                    "trigger_id": persona["trigger_id"],
                    "channels": ["sms", "email"],
                    "cta_label": product["cta_label"],
                    "cta_url": product["cta_url"],
                    "tone": "спокойный и деловой",
                    "notes": "Без давления и обещаний результата.",
                    "synthetic": True,
                }
            },
            headers=_key("campaign-v1"),
        )
        assert campaign.status_code == 201
        campaign_id = campaign.json()["campaign_id"]
        validated = client.post(
            f"/api/v1/campaigns/{campaign_id}/validate",
            headers=_key("validate-v1"),
        )
        assert validated.status_code == 200
        assert validated.json()["state"] == "READY"

        workspace = client.get(f"/api/v1/campaigns/{campaign_id}/workspace").json()
        context = workspace["context"]
        assert context["product"]["product_id"] == product["product_id"]
        assert context["product"]["version"] == 1
        assert context["source_manifest"]
        assert {item["version"] for item in context["source_manifest"]} == {"1"}
        assert len(context["content_plan"]["channel_selected_fact_ids"]["sms"]) == 3
        assert len(context["content_plan"]["channel_selected_fact_ids"]["email"]) == 4

        updated = client.post(
            "/api/v1/authoring/products",
            json=_custom_product(monthly_price=590),
            headers=_key("product-v2"),
        )
        assert updated.status_code == 201
        assert updated.json()["version"] == 2
        assert updated.json()["product_id"].endswith("_v2")

        revalidated = client.post(
            f"/api/v1/campaigns/{campaign_id}/validate",
            headers=_key("revalidate-v1"),
        )
        assert revalidated.status_code == 200
        workspace = client.get(f"/api/v1/campaigns/{campaign_id}/workspace").json()
        assert workspace["context"]["product"]["version"] == 1

        visible_custom = [
            item
            for item in client.get("/api/v1/authoring/catalog").json()["products"]
            if item["origin"] == "custom"
        ]
        assert [item["version"] for item in visible_custom] == [2]
        recent = client.get("/api/v1/campaigns").json()
        assert recent[0]["campaign_id"] == campaign_id
        assert recent[0]["product_name"] == "Ритм Команды"
        assert client.get("/api/v1/cases").json() == before_cases


def test_custom_product_admission_rejects_untyped_values_and_contact_pii(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        untyped = _custom_product()
        untyped["facts"][1]["normalized_value"] = "490 рублей"  # type: ignore[index]
        assert (
            client.post(
                "/api/v1/authoring/products",
                json=untyped,
                headers=_key("untyped"),
            ).status_code
            == 422
        )

        pii = _custom_product()
        pii["facts"][0]["canonical_text"] = "Напишите owner@example.com."  # type: ignore[index]
        assert (
            client.post(
                "/api/v1/authoring/products",
                json=pii,
                headers=_key("pii"),
            ).status_code
            == 422
        )


def test_blank_normal_campaign_is_not_silently_replaced_with_b01(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/campaigns",
            json={"brief": {"synthetic": True}},
            headers=_key("blank"),
        )
        campaign_id = created.json()["campaign_id"]
        validated = client.post(
            f"/api/v1/campaigns/{campaign_id}/validate",
            headers=_key("blank-validate"),
        )

        assert created.status_code == 201
        assert created.json()["draft"]["name"] is None
        assert created.json()["draft"]["product_id"] is None
        assert validated.json()["state"] == "NEEDS_INPUT"
        assert validated.json()["validation"]["llm_calls"] == 0
        assert 1 <= len(validated.json()["validation"]["questions"]) <= 5
