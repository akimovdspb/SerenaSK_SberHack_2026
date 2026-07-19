from __future__ import annotations

import pathlib

import pytest

from scripts.architecture_scan import ROOT, scan_backend


@pytest.mark.contract
def test_backend_has_no_direct_llm_or_provider_client() -> None:
    assert scan_backend(ROOT) == []


def test_backend_scan_rejects_provider_import_and_endpoint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "apps" / "api" / "app").mkdir(parents=True)
    (tmp_path / "apps" / "requirements.lock").write_text("openai==9.9.9\n", encoding="utf-8")
    (tmp_path / "apps" / "api" / "app" / "bad.py").write_text(
        'import openai\nURL = "https://api.openai.com/v1/responses"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "scripts.architecture_scan.load_rendered_compose",
        lambda: {"services": {"app": {"environment": {}}}},
    )
    errors = scan_backend(tmp_path)

    assert any("direct provider module" in error for error in errors)
    assert any("direct provider endpoint" in error for error in errors)
    assert any("provider SDK" in error for error in errors)
