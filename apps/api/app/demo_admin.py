from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from typing import Any

from apps.api.app.services.catalog import DEFAULT_DATA_DIR
from apps.api.app.settings import get_settings
from apps.api.app.workflow.store import WorkflowStore

DEFAULT_DATA_ROOT = pathlib.Path("/data")


class DemoAdminError(RuntimeError):
    pass


def _sqlite_path(database_url: str) -> pathlib.Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise DemoAdminError("demo reset supports only the project SQLite database")
    value = database_url.removeprefix(prefix)
    if not value:
        raise DemoAdminError("demo database path is empty")
    return pathlib.Path(value)


def _inside(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    candidate = path.resolve(strict=False)
    allowed = root.resolve(strict=True)
    try:
        candidate.relative_to(allowed)
    except ValueError as exc:
        raise DemoAdminError("demo path escapes the project data volume") from exc
    if candidate == allowed:
        raise DemoAdminError("demo path cannot be the data-volume root")
    return candidate


def reset_demo_state(
    *,
    database_url: str,
    artifacts_dir: pathlib.Path,
    data_root: pathlib.Path = DEFAULT_DATA_ROOT,
    data_dir: pathlib.Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    database = _inside(_sqlite_path(database_url), data_root)
    artifacts = _inside(artifacts_dir, data_root)
    if artifacts.is_symlink() or database.is_symlink():
        raise DemoAdminError("demo reset refuses symlinked mutable state")
    removed_database_files = 0
    for suffix in ("", "-wal", "-shm"):
        path = pathlib.Path(f"{database}{suffix}")
        if path.exists():
            if not path.is_file() or path.is_symlink():
                raise DemoAdminError("demo database state is not a regular file")
            path.unlink()
            removed_database_files += 1
    if artifacts.exists():
        if not artifacts.is_dir():
            raise DemoAdminError("demo artifacts path is not a directory")
        shutil.rmtree(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    store = WorkflowStore(database_url, data_dir=data_dir, artifacts_dir=artifacts)
    store.initialize()
    dashboard = store.dashboard()
    return {
        "status": "PASS",
        "action": "reset",
        "removed_database_files": removed_database_files,
        "catalog_case_count": dashboard.metrics.catalog_case_count,
        "observed_case_count": dashboard.metrics.observed_case_count,
        "live_case_count": dashboard.metrics.live_case_count,
        "provider_calls": 0,
    }


def check_demo_state(
    *,
    database_url: str,
    artifacts_dir: pathlib.Path,
    data_dir: pathlib.Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    store = WorkflowStore(database_url, data_dir=data_dir, artifacts_dir=artifacts_dir)
    store.initialize()
    dashboard = store.dashboard()
    case_ids = [item.case.case_id for item in dashboard.business_cases]
    if case_ids != [f"B{ordinal:02d}" for ordinal in range(1, 16)]:
        raise DemoAdminError("demo catalog does not contain exact B01-B15")
    return {
        "status": "PASS",
        "action": "check",
        "catalog_case_count": dashboard.metrics.catalog_case_count,
        "observed_case_count": dashboard.metrics.observed_case_count,
        "live_case_count": dashboard.metrics.live_case_count,
        "provider_calls": 0,
        "synthetic": dashboard.synthetic,
        "no_send": dashboard.no_send,
        "b01_ready": "B01" in case_ids,
        "b03_ready": "B03" in case_ids,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("reset", "check"))
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        result = (
            reset_demo_state(
                database_url=settings.DATABASE_URL,
                artifacts_dir=settings.ARTIFACTS_DIR,
                data_dir=settings.SYNTHETIC_DATA_DIR,
            )
            if args.action == "reset"
            else check_demo_state(
                database_url=settings.DATABASE_URL,
                artifacts_dir=settings.ARTIFACTS_DIR,
                data_dir=settings.SYNTHETIC_DATA_DIR,
            )
        )
    except (OSError, ValueError, DemoAdminError) as exc:
        print(f"demo-admin: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
