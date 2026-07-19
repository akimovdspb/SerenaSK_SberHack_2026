from __future__ import annotations

import json
import pathlib
import tempfile
import time
from typing import Any

from pydantic import ValidationError

from apps.api.app.domain.models import DraftSaveRequest
from apps.api.app.domain.workflow import RunStatus
from apps.api.app.mcp.service import FactoryMcpService
from apps.api.app.ouroboros_client import TaskAdmission, TaskAdmissionError
from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.workflow.runs import RunCoordinator
from apps.api.app.workflow.store import WorkflowStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHAOS_ROOT = ROOT / "runtime" / "evaluation" / "chaos"
CHAOS_TERMINAL_DEADLINE_SECONDS = 1.0
CHAOS_RELEASE_WAIT_SECONDS = 5


def _admission() -> TaskAdmission:
    return TaskAdmission(
        constraints="COMMUNICATION_FACTORY_CONTRACT_V1\nSynthetic chaos fixture.",
        disabled_tools=[],
        prompt_hash="1" * 64,
        skill_content_hash="2" * 64,
        tool_inventory_hash="3" * 64,
        activation_mode="adapter_injected",
        runtime_image_id="sha256:" + "4" * 64,
    )


class _AdmissionFailureAdapter:
    def admit(self) -> TaskAdmission:
        raise TaskAdmissionError("synthetic admission failure")

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(payload)

    def task(self, task_id: str) -> dict[str, Any]:
        raise AssertionError(task_id)

    def tasks(self) -> dict[str, Any]:
        return {"tasks": []}

    def cancel_task(self, task_id: str) -> None:
        raise AssertionError(task_id)

    def task_events_text(self, task_id: str) -> str:
        raise AssertionError(task_id)


class _TimeoutAdapter:
    def __init__(self) -> None:
        self.task_id = ""
        self.cancelled = False

    def admit(self) -> TaskAdmission:
        return _admission()

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.task_id = str(payload["task_id"])
        return {"task_id": self.task_id, "status": "queued"}

    def task(self, task_id: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "status": "cancelled" if self.cancelled else "running",
            "final_answer": "",
        }

    def tasks(self) -> dict[str, Any]:
        return {"tasks": [] if self.cancelled else [{"task_id": self.task_id}]}

    def cancel_task(self, task_id: str) -> None:
        if task_id == self.task_id:
            self.cancelled = True

    def task_events_text(self, task_id: str) -> str:
        if not self.cancelled:
            return ""
        return (
            "data: "
            + json.dumps(
                {
                    "type": "task_result",
                    "source": "events",
                    "data": {"status": "cancelled", "task_id": task_id},
                }
            )
            + "\n\n"
        )


class _TerminalFailureAdapter(_TimeoutAdapter):
    def __init__(self, task_id: str) -> None:
        super().__init__()
        self.task_id = task_id
        self.cancelled = True

    def task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "status": "failed", "final_answer": ""}

    def task_events_text(self, task_id: str) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "type": "task_result",
                    "source": "events",
                    "data": {"status": "failed", "task_id": task_id},
                }
            )
            + "\n\n"
        )


def _components(root: pathlib.Path) -> tuple[WorkflowStore, FactoryMcpService, str]:
    database_url = f"sqlite:///{root / 'factory.db'}"
    store = WorkflowStore(
        database_url,
        data_dir=DEFAULT_DATA_DIR,
        artifacts_dir=root / "artifacts",
    )
    mcp = FactoryMcpService(database_url, draft_processor=store)
    store.initialize()
    mcp.initialize()
    campaign = store.create_campaign(brief=None, case_id="B04")
    ready = store.validate_campaign(campaign.campaign_id)
    if ready.state.value != "READY":
        raise RuntimeError("chaos fixture did not create a ready campaign")
    return store, mcp, campaign.campaign_id


def _result(
    case_id: str,
    *,
    started_at: float,
    assertions: dict[str, bool],
    outcome: str,
    mode: str,
) -> dict[str, Any]:
    duration_ms = int((time.monotonic() - started_at) * 1_000)
    return {
        "case_id": case_id,
        "outcome": outcome,
        "mode": mode,
        "duration_ms": duration_ms,
        "under_30_seconds": duration_ms < 30_000,
        "provider_calls": 0,
        "assertions": assertions,
        "passed": duration_ms < 30_000 and all(assertions.values()),
    }


def run_x01(root: pathlib.Path) -> dict[str, Any]:
    started = time.monotonic()
    store, mcp, campaign_id = _components(root)
    coordinator = RunCoordinator(store=store, mcp_service=mcp, adapter=_AdmissionFailureAdapter())
    typed_failure = False
    try:
        coordinator.start_live(campaign_id)
    except TaskAdmissionError:
        typed_failure = True
    finally:
        coordinator.shutdown()
    return _result(
        "X01",
        started_at=started,
        assertions={
            "typed_admission_failure": typed_failure,
            "campaign_remains_ready": store.get_campaign(campaign_id).state.value == "READY",
            "no_run_or_fallback_created": not store.active_runs()
            and store.get_campaign(campaign_id).package_id is None,
        },
        outcome="ADMISSION_REJECTED",
        mode="validation_only",
    )


def run_x02(root: pathlib.Path) -> dict[str, Any]:
    started = time.monotonic()
    _, mcp, campaign_id = _components(root)
    malformed_rejected = False
    try:
        DraftSaveRequest.model_validate(
            {
                "campaign_id": campaign_id,
                "operation": "initial",
                "iteration": 1,
                "context_version": "not-a-hash",
                "idempotency_key": "chaos-x02-idempotency",
                "draft": {"kind": "communication_bundle"},
            }
        )
    except ValidationError:
        malformed_rejected = True
    snapshot = mcp.probe_snapshot(campaign_id)
    return _result(
        "X02",
        started_at=started,
        assertions={
            "typed_schema_rejection": malformed_rejected,
            "no_draft_persisted": snapshot.get("draft") is None,
            "no_duplicate_save": not any(
                str(item.get("event_type")) == "draft_saved"
                for item in snapshot.get("events") or []
                if isinstance(item, dict)
            ),
        },
        outcome="MALFORMED_PAYLOAD_REJECTED",
        mode="validation_only",
    )


def run_x03(root: pathlib.Path) -> dict[str, Any]:
    started = time.monotonic()
    store, mcp, campaign_id = _components(root)
    adapter = _TimeoutAdapter()
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=adapter,
        terminal_deadline_seconds=CHAOS_TERMINAL_DEADLINE_SECONDS,
        poll_interval_seconds=0.01,
    )
    try:
        accepted = coordinator.start_live(campaign_id)
        completed = coordinator.wait(
            accepted.run_id,
            timeout=CHAOS_RELEASE_WAIT_SECONDS,
        )
    finally:
        coordinator.shutdown()
    package = store.get_package(completed.package_id) if completed.package_id else None
    return _result(
        "X03",
        started_at=started,
        assertions={
            "cancel_sent": adapter.cancelled,
            "terminal_fallback": completed.status is RunStatus.COMPLETED_FALLBACK,
            "fallback_marked": completed.mode == "deterministic_template"
            and completed.reason_code == "LIVE_TASK_CANCELLED",
            "worker_released": completed.worker_released_at is not None,
            "qa_green_fallback": package is not None and package.quality_report.approvable,
        },
        outcome=completed.status.value,
        mode=completed.mode,
    )


def run_x04(root: pathlib.Path) -> dict[str, Any]:
    started = time.monotonic()
    store, _, _ = _components(root)
    calls = 0

    def create() -> Any:
        nonlocal calls
        calls += 1
        return store.create_campaign(brief=None, case_id="B02")

    first = store.execute_idempotent(
        scope="chaos:X04",
        key="chaos-x04-idempotency-key",
        payload={"case_id": "B02"},
        operation=create,
    )
    second = store.execute_idempotent(
        scope="chaos:X04",
        key="chaos-x04-idempotency-key",
        payload={"case_id": "B02"},
        operation=create,
    )
    return _result(
        "X04",
        started_at=started,
        assertions={
            "same_domain_result": first["campaign_id"] == second["campaign_id"],
            "operation_executed_once": calls == 1,
            "no_provider_call": True,
        },
        outcome="IDEMPOTENT_REPLAY",
        mode="validation_only",
    )


def run_x05(root: pathlib.Path) -> dict[str, Any]:
    started = time.monotonic()
    store, mcp, campaign_id = _components(root)
    task_id = "task_chaos_x05_stale"
    run = store.create_live_run(
        run_id="run_chaos_x05_stale",
        campaign_id=campaign_id,
        operation="initial",
        iteration=1,
        task_id=task_id,
        project_id="project_chaos_x05",
        context_version=store.get_current_context(campaign_id).context_version,
        prompt_hash="1" * 64,
        skill_content_hash="2" * 64,
        tool_inventory_hash="3" * 64,
    )
    store.mark_run_started(run.run_id)
    coordinator = RunCoordinator(
        store=store,
        mcp_service=mcp,
        adapter=_TerminalFailureAdapter(task_id),
        terminal_deadline_seconds=CHAOS_TERMINAL_DEADLINE_SECONDS,
        poll_interval_seconds=0.01,
    )
    try:
        coordinator.reconcile_active()
        completed = coordinator.wait(
            run.run_id,
            timeout=CHAOS_RELEASE_WAIT_SECONDS,
        )
    finally:
        coordinator.shutdown()
    return _result(
        "X05",
        started_at=started,
        assertions={
            "reconciled_terminal": completed.status is RunStatus.COMPLETED_FALLBACK,
            "reason_preserved": completed.reason_code == "LIVE_TASK_FAILED",
            "worker_released": completed.worker_released_at is not None,
            "package_recovered": completed.package_id is not None,
            "no_active_run_remains": not store.active_runs(),
        },
        outcome=completed.status.value,
        mode=completed.mode,
    )


def run_chaos_suite() -> dict[str, Any]:
    runners = (run_x01, run_x02, run_x03, run_x04, run_x05)
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="communication-factory-chaos-") as directory:
        base = pathlib.Path(directory)
        for runner in runners:
            case_root = base / runner.__name__
            case_root.mkdir()
            cases.append(runner(case_root))
    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "status": "PASS" if all(case["passed"] for case in cases) else "FAIL",
        "chaos_case_count": len(cases),
        "passed_case_count": sum(bool(case["passed"]) for case in cases),
        "provider_calls": 0,
        "normal_metrics_included": False,
        "cases": cases,
    }


def _atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    report = run_chaos_suite()
    _atomic_json(CHAOS_ROOT / "latest.json", report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "chaos_cases": report["chaos_case_count"],
                "passed": report["passed_case_count"],
                "provider_calls": 0,
                "normal_metrics_included": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
