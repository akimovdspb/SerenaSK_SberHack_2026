from __future__ import annotations

import csv
import hashlib
import io
import json
import pathlib
import shutil

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.settings import Settings


def _settings(
    tmp_path: pathlib.Path,
    *,
    evidence_dir: pathlib.Path | None = None,
    contract_path: pathlib.Path | None = None,
    mvp_report_dir: pathlib.Path | None = None,
) -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'factory.db'}",
        ARTIFACTS_DIR=tmp_path / "artifacts",
        SYNTHETIC_DATA_DIR=DEFAULT_DATA_DIR,
        CONTRACT_LOCK_PATH=contract_path or tmp_path / "missing-contract-lock.json",
        EVIDENCE_DIR=evidence_dir or tmp_path / "evidence",
        MVP_REPORT_DIR=mvp_report_dir
        or pathlib.Path(__file__).resolve().parents[2] / "reports/basket03-mvp-testing",
        MCP_SHARED_TOKEN="mcp-test-token-that-is-at-least-32-chars",
    )


def _write_frozen_evidence(root: pathlib.Path) -> str:
    evaluation_id = "frozen-presentation-fixture"
    evidence = root / "20260712_fixture"
    evidence.mkdir(parents=True)
    files: dict[str, bytes] = {
        "report.pdf": b"%PDF-1.4\nfixture\n%%EOF\n",
        "report.jpg": b"\xff\xd8fixture\xff\xd9",
        "report.html": b"<!doctype html><main>fixture</main>",
    }
    metrics = {
        "schema_version": 1,
        "evaluation_id": evaluation_id,
        "business": {
            "case_count": 15,
            "passed_count": 15,
            "live_case_count": 12,
            "mode_counts": {"live_ouroboros": 12, "validation_only": 3},
        },
        "normal_live_latency_ms": {
            "user_visible_terminal": {"p50": 1000, "p95": 2000, "max": 2500}
        },
        "provider_usage": {
            "totals": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "cost_usd": 0.02,
            }
        },
        "stability": {"crash_count": 0, "timeout_over_30s_count": 0},
        "qualitative_review": {"status": "WAITING_FOR_OPERATOR"},
        "synthetic": True,
        "no_send": True,
    }
    files["metrics.json"] = (json.dumps(metrics) + "\n").encode()
    rows = []
    for ordinal in range(1, 16):
        case_id = f"B{ordinal:02d}"
        rows.append(
            {
                "case_id": case_id,
                "mode": "validation_only" if case_id in {"B11", "B12", "B13"} else "live_ouroboros",
                "passed": "True",
                "actual_terminal": "APPROVABLE" if ordinal not in {11, 12, 13} else "BLOCKED",
                "user_visible_terminal_ms": "" if ordinal in {11, 12, 13} else "1000",
            }
        )
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    files["business-results.csv"] = csv_buffer.getvalue().encode()
    stability = {
        "schema_version": 1,
        "normal_live": metrics["stability"],
        "chaos_isolated": {
            "cases": [
                {"case_id": f"X{ordinal:02d}", "passed": True, "duration_ms": ordinal}
                for ordinal in range(1, 6)
            ]
        },
    }
    files["stability-report.json"] = (json.dumps(stability) + "\n").encode()
    manifest = {
        "schema_version": 1,
        "evidence_kind": "implementation",
        "evaluation_id": evaluation_id,
        "created_at": "2026-07-12T00:00:00+00:00",
        "frozen": True,
        "synthetic": True,
        "no_send": True,
        "metrics_status": "PASS",
    }
    files["manifest.json"] = (json.dumps(manifest) + "\n").encode()
    for name, content in files.items():
        (evidence / name).write_bytes(content)
    checksum_rows = []
    for name in sorted(files):
        checksum_rows.append(f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n")
    (evidence / "checksums.sha256").write_text("".join(checksum_rows), encoding="utf-8")
    marker = {
        "schema_version": 1,
        "status": "IMMUTABLE",
        "evaluation_id": evaluation_id,
        "manifest_sha256": hashlib.sha256((evidence / "manifest.json").read_bytes()).hexdigest(),
        "checksums_sha256": hashlib.sha256(
            (evidence / "checksums.sha256").read_bytes()
        ).hexdigest(),
    }
    (evidence / "IMMUTABLE.json").write_text(json.dumps(marker), encoding="utf-8")
    return evaluation_id


def _headers(ordinal: int) -> dict[str, str]:
    return {
        "Idempotency-Key": f"presentation-idempotency-{ordinal:04d}",
        "X-CF-Actor": "presentation_test_editor",
        "X-CF-Actor-Role": "human",
    }


def test_dashboard_workspace_evaluation_and_diagnostics_are_real_read_models(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        empty_dashboard = client.get("/api/v1/dashboard")
        assert empty_dashboard.status_code == 200
        assert empty_dashboard.json()["metrics"] == {
            "catalog_case_count": 15,
            "target_business_case_count": 15,
            "observed_case_count": 0,
            "live_case_count": 0,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "max_latency_ms": None,
            "crash_count": 0,
            "timeout_count": 0,
            "provider_tokens": 0,
            "provider_cost_usd": 0.0,
        }

        campaign = client.post(
            "/api/v1/campaigns", json={"case_id": "B04"}, headers=_headers(1)
        ).json()
        campaign_id = campaign["campaign_id"]
        client.post(f"/api/v1/campaigns/{campaign_id}/validate", headers=_headers(2))
        package = client.post(
            f"/api/v1/campaigns/{campaign_id}/runs",
            json={"mode": "deterministic_template"},
            headers=_headers(3),
        ).json()

        workspace = client.get(f"/api/v1/campaigns/{campaign_id}/workspace")
        assert workspace.status_code == 200
        payload = workspace.json()
        assert payload["package"]["package_id"] == package["package_id"]
        assert payload["context"]["classification"] == "untrusted_data"
        assert payload["approval_eligible"] is True
        assert payload["export_eligible"] is False
        assert payload["export_disabled_reason"] == "PACKAGE_NOT_APPROVED"
        assert {event["event_type"] for event in payload["safe_trace"]} == {
            "campaign.created",
            "package.version_created",
        }

        dashboard = client.get("/api/v1/dashboard").json()
        b04 = next(item for item in dashboard["business_cases"] if item["case"]["case_id"] == "B04")
        assert b04["actual_status"] == "APPROVABLE"
        assert b04["execution_mode"] == "deterministic_template"
        assert b04["qa_score"] == 100
        assert dashboard["metrics"]["observed_case_count"] == 1

        summaries = client.get("/api/v1/evaluation/runs").json()
        assert summaries[0]["evaluation_id"] == "current_development_slice"
        evaluation = client.get("/api/v1/evaluation/runs/current_development_slice").json()
        assert evaluation["status"] == "NOT_FROZEN"
        assert evaluation["frozen"] is False
        assert evaluation["qualitative_review_status"] == "WAITING_FOR_OPERATOR"
        assert evaluation["report_links"][0]["format"] == "json"

        diagnostics = client.get("/api/v1/diagnostics")
        assert diagnostics.status_code == 200
        diagnostic_payload = diagnostics.json()
        assert diagnostic_payload["public_config_only"] is True
        assert diagnostic_payload["admission_state"] == "OPEN"
        provider = next(
            item for item in diagnostic_payload["components"] if item["component_id"] == "provider"
        )
        assert provider["status"] == "ISOLATED"
        assert "OPENAI_API_KEY" not in diagnostics.text
        assert "mcp-test-token" not in diagnostics.text


def test_presentation_read_models_preserve_empty_and_unknown_states(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        campaign = client.post(
            "/api/v1/campaigns", json={"case_id": "B01"}, headers=_headers(10)
        ).json()
        needs_input = client.post(
            f"/api/v1/campaigns/{campaign['campaign_id']}/validate",
            headers=_headers(11),
        )
        assert needs_input.json()["state"] == "NEEDS_INPUT"

        workspace = client.get(f"/api/v1/campaigns/{campaign['campaign_id']}/workspace").json()
        assert workspace["package"] is None
        assert workspace["approval_eligible"] is False
        assert workspace["approval_disabled_reason"] == "PACKAGE_UNAVAILABLE"
        assert workspace["campaign"]["validation"]["llm_calls"] == 0

        assert client.get("/api/v1/campaigns/not_found/workspace").status_code == 404
        assert client.get("/api/v1/evaluation/runs/not_found").status_code == 404


def test_frozen_evidence_is_read_only_default_and_serves_allowlisted_reports(
    tmp_path: pathlib.Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evaluation_id = _write_frozen_evidence(evidence_root)
    app = create_app(_settings(tmp_path, evidence_dir=evidence_root))

    with TestClient(app) as client:
        summaries = client.get("/api/v1/evaluation/runs")
        assert summaries.status_code == 200
        assert summaries.json()[0]["evaluation_id"] == evaluation_id
        assert summaries.json()[0]["frozen"] is True

        frozen = client.get(f"/api/v1/evaluation/runs/{evaluation_id}")
        assert frozen.status_code == 200
        payload = frozen.json()
        assert payload["status"] == "FROZEN"
        assert payload["metrics"]["observed_case_count"] == 15
        assert payload["metrics"]["live_case_count"] == 12
        assert len(payload["business_cases"]) == 15
        assert len(payload["chaos_cases"]) == 5
        assert all(link["checksum"] for link in payload["report_links"])

        report = client.get(f"/api/v1/evaluation/artifacts/{evaluation_id}/report.pdf")
        assert report.status_code == 200
        assert report.content.startswith(b"%PDF-")
        assert (
            client.get(f"/api/v1/evaluation/artifacts/{evaluation_id}/not-allowed.txt").status_code
            == 404
        )


def test_mvp_results_expose_exactly_ten_confirmed_live_cases(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(_settings(tmp_path))
    expected_ids = [
        "B01",
        "B02",
        "B03",
        "B04",
        "B06",
        "B07",
        "B09",
        "B10",
        "B14",
        "B15",
    ]

    with TestClient(app) as client:
        response = client.get("/api/v1/results/mvp")
        assert response.status_code == 200
        payload = response.json()
        assert [item["case_id"] for item in payload["cases"]] == expected_ids
        assert payload["metrics"]["confirmed_live_case_count"] == 10
        assert payload["canonical_release_evidence"] is False
        assert payload["synthetic"] is True
        assert payload["no_send"] is True
        assert all(item["actual_terminal"] == "APPROVABLE" for item in payload["cases"])
        assert all(item["qa_score"] == 100 for item in payload["cases"])
        assert all(item["channels"] for item in payload["cases"])
        assert "B05" not in response.text
        assert "B08" not in response.text

        report = client.get("/api/v1/results/mvp/artifacts/report.pdf")
        assert report.status_code == 200
        assert report.content.startswith(b"%PDF-")
        assert client.get("/api/v1/results/mvp/artifacts/private.txt").status_code == 404


def test_mvp_results_fail_closed_when_a_report_checksum_changes(tmp_path: pathlib.Path) -> None:
    source = pathlib.Path(__file__).resolve().parents[2] / "reports/basket03-mvp-testing"
    copied = tmp_path / "mvp-report"
    shutil.copytree(source, copied)
    (copied / "cases/B01.json").write_text("{}", encoding="utf-8")
    app = create_app(_settings(tmp_path, mvp_report_dir=copied))

    with TestClient(app) as client:
        assert client.get("/api/v1/results/mvp").status_code == 503


def test_diagnostics_projects_post_deny_two_tool_set_and_exact_git_commit(
    tmp_path: pathlib.Path,
) -> None:
    contract_path = tmp_path / "communication_factory.lock.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-07-12T00:00:00+00:00",
                "runtime": {
                    "tag": "v6.61.4",
                    "commit": "a00d51dd414f794d830cacf7da760061e442fa88",
                },
                "skill": {
                    "ready": True,
                    "skill_content_hash": "a" * 64,
                    "prompt_hash": "b" * 64,
                },
                "tools": {
                    "effective_tool_names": ["built_in", "mcp_factory__cf_context_get"],
                    "post_deny_tool_names": [
                        "mcp_factory__cf_context_get",
                        "mcp_factory__cf_draft_save",
                    ],
                    "inventory_hash": "c" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    app = create_app(_settings(tmp_path, contract_path=contract_path))

    with TestClient(app) as client:
        diagnostics = client.get("/api/v1/diagnostics")
        assert diagnostics.status_code == 200
        payload = diagnostics.json()
        assert payload["runtime_commit"] == "a00d51dd414f794d830cacf7da760061e442fa88"
        assert payload["discovered_tools"] == [
            "mcp_factory__cf_context_get",
            "mcp_factory__cf_draft_save",
        ]
        assert payload["admission_state"] == "CLOSED"
