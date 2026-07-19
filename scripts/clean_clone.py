from __future__ import annotations

import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from scripts.budget_control import DEFAULT_USAGE_LEDGER, RUN_ID_PATTERN, parse_usage_record
from scripts.smoke import _json_body, _request, validate_export

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "runtime" / "verification" / "clean-clone"
SOURCE_REVIEW_VOLUME = "communication-factory_ouroboros_data"
REVIEW_SEED_PROGRAM = """
import os
import pathlib
import shutil
source = pathlib.Path('/source/state/skills/communication_factory')
target_root = pathlib.Path('/target')
target = target_root / 'state/skills/communication_factory'
if not source.is_dir() or target.exists():
    raise SystemExit(2)
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(source, target, symlinks=False)
for path in [target_root, *target_root.rglob('*')]:
    os.chown(path, 10001, 10001)
""".strip()
PROVIDER_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
)


class CleanCloneError(RuntimeError):
    pass


def _positive_int(environment: dict[str, str], name: str) -> int:
    try:
        value = int(environment.get(name, "0"))
    except ValueError as exc:
        raise CleanCloneError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise CleanCloneError(f"{name} must be a positive integer")
    return value


def _positive_float(environment: dict[str, str], name: str) -> float:
    try:
        value = float(environment.get(name, "0"))
    except ValueError as exc:
        raise CleanCloneError(f"{name} must be positive") from exc
    if value <= 0:
        raise CleanCloneError(f"{name} must be positive")
    return value


def validate_rehearsal_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(source if source is not None else os.environ)
    if environment.get("ALLOW_CLEAN_CLONE_LIVE", "").casefold() != "true":
        raise CleanCloneError("clean-clone live smoke requires ALLOW_CLEAN_CLONE_LIVE=true")
    run_id = str(environment.get("CLEAN_CLONE_EVALUATION_ID") or "").strip()
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise CleanCloneError("CLEAN_CLONE_EVALUATION_ID is invalid")
    if environment.get("EVAL_PROVIDER_PROFILE") != "openai-gpt-5.4-mini":
        raise CleanCloneError("EVAL_PROVIDER_PROFILE must be openai-gpt-5.4-mini")
    _positive_int(environment, "EVAL_MAX_TOKENS")
    _positive_float(environment, "EVAL_MAX_COST_USD")
    _positive_int(environment, "EVAL_PROJECTED_TOKENS")
    _positive_float(environment, "EVAL_PROJECTED_COST_USD")
    if environment.get("EVAL_CONCURRENCY") != "1":
        raise CleanCloneError("EVAL_CONCURRENCY must equal 1")
    if (ROOT / "runtime" / "budget" / "runs" / f"{run_id}.json").exists():
        raise CleanCloneError("clean-clone live run ID already exists in project history")
    if (REPORT_ROOT / "runs" / run_id).exists():
        raise CleanCloneError("clean-clone evidence ID was already used")
    for name in PROVIDER_ENV_NAMES:
        if environment.get(name):
            raise CleanCloneError("host clean-clone process must not receive provider credentials")
    return environment


def validate_rehearsal_plan() -> dict[str, Any]:
    commands = [
        "git clone exact current commit",
        "make init",
        "make up",
        "open authenticated B01 UI in pinned Playwright",
        "guarded live B04 smoke and test-only export",
        "make verify-core",
        "make down",
    ]
    if any("eval-live" in command or "bootstrap review" in command for command in commands):
        raise CleanCloneError("clean-clone plan contains an unapproved paid surface")
    return {
        "status": "PASS",
        "schema_version": 1,
        "command_count": len(commands),
        "readme_startup_step_count": 4,
        "requires_explicit_live_caps": True,
        "reuses_official_skill_review_only": True,
        "provider_review_started": False,
        "commands": commands,
    }


def _run(
    command: Sequence[str],
    *,
    cwd: pathlib.Path,
    environment: dict[str, str],
    timeout: int,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and process.returncode != 0:
        raise CleanCloneError(f"clean-clone command failed: {command[0]} {command[1]}")
    return process


def _git(arguments: list[str]) -> str:
    process = _run(
        ["git", *arguments],
        cwd=ROOT,
        environment=dict(os.environ),
        timeout=30,
        capture=True,
    )
    return process.stdout.strip()


def _review_seed_metadata(volume: str, environment: dict[str, str]) -> dict[str, Any]:
    program = (
        "import json,pathlib;"
        "b=pathlib.Path('/source/state/skills/communication_factory');"
        "r=json.loads((b/'review.json').read_text());"
        "e=json.loads((b/'enabled.json').read_text());"
        "j=json.loads((b/'review_job.json').read_text());"
        "print(json.dumps({'content_hash':r.get('content_hash'),'enabled':e.get('enabled'),"
        "'review_status':j.get('review_status'),'finding_count':len(r.get('findings') or [])}))"
    )
    process = _run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "python",
            "-v",
            f"{volume}:/source:ro",
            "communication-factory/app:local",
            "-c",
            program,
        ],
        cwd=ROOT,
        environment=environment,
        timeout=30,
        capture=True,
    )
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise CleanCloneError("official skill review seed metadata is malformed") from exc
    if not isinstance(value, dict):
        raise CleanCloneError("official skill review seed metadata is invalid")
    return {str(key): item for key, item in value.items()}


def _seed_review_state(source: str, target: str, environment: dict[str, str]) -> None:
    _run(
        ["docker", "volume", "create", target],
        cwd=ROOT,
        environment=environment,
        timeout=30,
        capture=True,
    )
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "python",
            "-v",
            f"{source}:/source:ro",
            "-v",
            f"{target}:/target",
            "communication-factory/app:local",
            "-c",
            REVIEW_SEED_PROGRAM,
        ],
        cwd=ROOT,
        environment=environment,
        timeout=60,
        capture=True,
    )


def _credentials(path: pathlib.Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if not values.get("username") or not values.get("password"):
        raise CleanCloneError("clean-clone access credentials are incomplete")
    return values["username"], values["password"]


def _port(clone: pathlib.Path, project: str, environment: dict[str, str]) -> int:
    process = _run(
        ["docker", "compose", "--project-name", project, "port", "gateway", "8080"],
        cwd=clone,
        environment=environment,
        timeout=30,
        capture=True,
    )
    try:
        return int(process.stdout.strip().rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise CleanCloneError("clean-clone gateway port is invalid") from exc


def _recorded_step(
    transcript: list[dict[str, Any]],
    label: str,
    command: Sequence[str],
    *,
    cwd: pathlib.Path,
    environment: dict[str, str],
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    process = _run(
        command,
        cwd=cwd,
        environment=environment,
        timeout=timeout,
        check=False,
    )
    transcript.append(
        {
            "label": label,
            "command": list(command),
            "returncode": process.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_stderr_recorded": False,
            "credentials_recorded": False,
        }
    )
    if check and process.returncode != 0:
        raise CleanCloneError(f"clean-clone step failed: {label}")
    return process


def _merge_usage(source: pathlib.Path, run_id: str) -> int:
    if not source.is_file():
        raise CleanCloneError("clean-clone live smoke produced no usage ledger")
    selected: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CleanCloneError("clean-clone usage ledger is malformed") from exc
        record = parse_usage_record(payload)
        if record.run_id == run_id:
            selected.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if not selected:
        raise CleanCloneError("clean-clone live smoke has no metered usage rows")
    existing_ids: set[str] = set()
    if DEFAULT_USAGE_LEDGER.is_file():
        for line in DEFAULT_USAGE_LEDGER.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing_ids.add(parse_usage_record(json.loads(line)).run_id)
    if run_id in existing_ids:
        raise CleanCloneError("clean-clone usage run already exists in canonical ledger")
    DEFAULT_USAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_USAGE_LEDGER.open("a", encoding="utf-8") as handle:
        for line in selected:
            handle.write(line + "\n")
    return len(selected)


def _approve_and_export(
    *,
    base_url: str,
    username: str,
    password: str,
    live_report: dict[str, Any],
) -> dict[str, Any]:
    package = live_report.get("package")
    if (
        live_report.get("ok") is not True
        or not isinstance(package, dict)
        or package.get("mode") != "live_ouroboros"
        or not isinstance(package.get("quality_report"), dict)
        or package["quality_report"].get("approvable") is not True
    ):
        raise CleanCloneError("clean-clone B04 smoke is not a QA-green live package")
    package_id = str(package.get("package_id") or "")
    status, _, body, _ = _request(
        base_url,
        f"/api/v1/packages/{package_id}/approve",
        username=username,
        password=password,
        method="POST",
        payload={
            "package_hash": package.get("package_hash"),
            "decision": "APPROVED",
            "acknowledged_warning_ids": [],
            "test_only": True,
        },
        idempotency_key="clean-clone-package-approval-0001",
    )
    approval = _json_body(body)
    if status != 200 or approval.get("test_only") is not True:
        raise CleanCloneError("clean-clone test-only package approval failed")
    status, _, body, _ = _request(
        base_url,
        f"/api/v1/packages/{package_id}/export",
        username=username,
        password=password,
        method="POST",
        idempotency_key="clean-clone-package-export-0001",
    )
    exported = _json_body(body)
    if status != 201:
        raise CleanCloneError("clean-clone package export failed")
    export_id = str(exported.get("export_id") or "")
    status, _, archive, _ = _request(
        base_url,
        f"/api/v1/exports/{export_id}/download",
        username=username,
        password=password,
    )
    if status != 200:
        raise CleanCloneError("clean-clone export download failed")
    manifest = validate_export(archive)
    return {
        "package_id": package_id,
        "export_id": export_id,
        "export_file_count": len(manifest["files"]) + 1,
    }


def run_rehearsal(environment: dict[str, str] | None = None) -> dict[str, Any]:
    effective = validate_rehearsal_environment(environment)
    commit = _git(["rev-parse", "HEAD"])
    if _git(["status", "--porcelain=v1"]):
        raise CleanCloneError("clean-clone rehearsal requires a clean committed worktree")
    run_id = effective["CLEAN_CLONE_EVALUATION_ID"]
    project = f"cf-clean-{secrets.token_hex(6)}"
    target_volume = f"{project}_ouroboros_data"
    working_root = pathlib.Path(tempfile.mkdtemp(prefix="communication-factory-clean-"))
    clone = working_root / "communication-factory"
    transcript: list[dict[str, Any]] = []
    usage_rows = 0
    try:
        contract = json.loads(
            (ROOT / "runtime" / "contracts" / "communication_factory.lock.json").read_text(
                encoding="utf-8"
            )
        )
        expected_skill_hash = str((contract.get("skill") or {}).get("skill_content_hash") or "")
        review = _review_seed_metadata(SOURCE_REVIEW_VOLUME, effective)
        if (
            review.get("content_hash") != expected_skill_hash
            or review.get("enabled") is not True
            or review.get("review_status") != "clean"
            or review.get("finding_count") != 0
        ):
            raise CleanCloneError("official skill review seed is stale or disabled")
        _seed_review_state(SOURCE_REVIEW_VOLUME, target_volume, effective)
        _recorded_step(
            transcript,
            "clone",
            ["git", "clone", "--local", "--no-hardlinks", str(ROOT), str(clone)],
            cwd=working_root,
            environment=effective,
            timeout=120,
        )
        shutil.copytree(ROOT / "runtime" / "upstream", clone / "runtime" / "upstream")
        clone_environment = dict(effective)
        for name in PROVIDER_ENV_NAMES:
            clone_environment.pop(name, None)
        clone_environment.update(
            {
                "COMPOSE_PROJECT_NAME": project,
                "GATEWAY_HOST_BIND": "127.0.0.1",
                "GATEWAY_HOST_PORT": "0",
                "ALLOW_BOOTSTRAP_REVIEW": "false",
                "FORCE_BOOTSTRAP_REVIEW": "false",
                "OPENROUTER_ENABLED": "false",
            }
        )
        _recorded_step(
            transcript,
            "readme_step_2_init",
            ["make", "init"],
            cwd=clone,
            environment=clone_environment,
            timeout=120,
        )
        _recorded_step(
            transcript,
            "readme_step_3_up",
            ["make", "up"],
            cwd=clone,
            environment=clone_environment,
            timeout=900,
        )
        username, password = _credentials(clone / "runtime" / "operator" / "access.txt")
        port = _port(clone, project, clone_environment)
        base_url = f"http://127.0.0.1:{port}"
        browser_environment = dict(clone_environment)
        browser_environment.update(
            {
                "CF_UI_USERNAME": username,
                "CF_UI_PASSWORD": password,
                "PLAYWRIGHT_BASE_URL": base_url,
            }
        )
        _recorded_step(
            transcript,
            "readme_step_4_browser",
            [
                str(ROOT / "node_modules" / ".bin" / "playwright"),
                "test",
                "tests/e2e/narrow-responsive.spec.ts",
            ],
            cwd=ROOT,
            environment=browser_environment,
            timeout=180,
        )
        clone_ledger = clone / "runtime" / "budget" / "usage.jsonl"
        clone_ledger.parent.mkdir(parents=True, exist_ok=True)
        if DEFAULT_USAGE_LEDGER.is_file():
            shutil.copy2(DEFAULT_USAGE_LEDGER, clone_ledger)
        live_environment = dict(clone_environment)
        live_environment.update(
            {
                "ALLOW_GATE2_LIVE": "true",
                "EVALUATION_ID": run_id,
            }
        )
        live_process = _recorded_step(
            transcript,
            "guarded_live_b04_smoke",
            ["make", "gate2-live-pilot"],
            cwd=clone,
            environment=live_environment,
            timeout=180,
            check=False,
        )
        usage_rows = _merge_usage(clone_ledger, run_id)
        preserved = REPORT_ROOT / "runs" / run_id
        preserved.parent.mkdir(parents=True, exist_ok=True)
        if preserved.exists():
            raise CleanCloneError("clean-clone preserved run directory already exists")
        preserved.mkdir()
        source_evidence = clone / "runtime" / "live-campaigns" / run_id
        if source_evidence.is_dir():
            shutil.copytree(source_evidence, preserved / "live-campaign")
        marker = clone / "runtime" / "budget" / "runs" / f"{run_id}.json"
        if marker.is_file():
            shutil.copy2(marker, preserved / "run-marker.json")
        if live_process.returncode != 0:
            raise CleanCloneError("clean-clone guarded live B04 smoke failed")
        live_report = json.loads((source_evidence / "report.json").read_text(encoding="utf-8"))
        export = _approve_and_export(
            base_url=base_url,
            username=username,
            password=password,
            live_report=live_report,
        )
        _recorded_step(
            transcript,
            "verify_core",
            ["make", "verify-core"],
            cwd=clone,
            environment=clone_environment,
            timeout=1800,
        )
        _recorded_step(
            transcript,
            "make_down",
            ["make", "down"],
            cwd=clone,
            environment=clone_environment,
            timeout=120,
        )
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "PASS",
            "git_commit": commit,
            "readme_startup_step_count": 4,
            "provider_review_started": False,
            "official_skill_review_state_reused": True,
            "skill_content_hash": expected_skill_hash,
            "live_smoke_run_id": run_id,
            "live_smoke_case_id": "B04",
            "live_smoke_mode": "live_ouroboros",
            "usage_rows_appended": usage_rows,
            "campaign_export": export,
            "verify_core_status": "PASS",
            "transcript": transcript,
            "credentials_recorded": False,
            "account_remaining": "unknown",
        }
        REPORT_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = REPORT_ROOT / "latest.tmp"
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, REPORT_ROOT / "latest.json")
        return report
    finally:
        if clone.is_dir():
            _run(
                [
                    "docker",
                    "compose",
                    "--project-name",
                    project,
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "10",
                ],
                cwd=clone,
                environment=effective,
                timeout=120,
                check=False,
            )
        _run(
            ["docker", "volume", "rm", "--force", target_volume],
            cwd=ROOT,
            environment=effective,
            timeout=30,
            check=False,
            capture=True,
        )
        shutil.rmtree(working_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    dry_run = argv == ["--dry-run"]
    try:
        if dry_run:
            report = validate_rehearsal_plan()
            print(
                "clean-clone: DRY-RUN PASS "
                f"commands={report['command_count']} explicit_live_caps=true"
            )
            return 0
        report = run_rehearsal()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"clean-clone: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "clean-clone: PASS startup_steps=4 live=B04 export=true "
        f"verify_core={report['verify_core_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
