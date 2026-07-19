from __future__ import annotations

import json
import pathlib

import pytest

from scripts import controlled_retry_e2e
from scripts.e2e import E2EError, validate_playwright_results


def test_playwright_result_validator_requires_five_traces_and_current_pass(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / ".last-run.json").write_text(
        json.dumps({"status": "passed", "failedTests": []}), encoding="utf-8"
    )
    for ordinal in range(8):
        target = tmp_path / f"case-{ordinal}"
        target.mkdir()
        (target / "screen.png").write_bytes(b"png")
        if ordinal < 5:
            (target / "trace.zip").write_bytes(b"zip")

    assert validate_playwright_results(tmp_path) == {
        "screenshot_count": 8,
        "trace_count": 5,
    }
    (tmp_path / "case-0" / "trace.zip").unlink()
    with pytest.raises(E2EError, match="artifact contract"):
        validate_playwright_results(tmp_path)


def test_controlled_retry_e2e_precreates_user_owned_bind_mounts(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controlled_retry_e2e.prepare_bind_mounts(tmp_path)

    for relative in controlled_retry_e2e.BIND_MOUNT_PATHS:
        assert (tmp_path / relative).is_dir()

    monkeypatch.setattr(controlled_retry_e2e.os, "getuid", lambda: 2**31 - 1)
    with pytest.raises(
        controlled_retry_e2e.ControlledRetryE2EError,
        match="not owned and writable",
    ):
        controlled_retry_e2e.prepare_bind_mounts(tmp_path)
