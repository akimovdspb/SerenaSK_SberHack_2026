from __future__ import annotations

import json
import pathlib

from scripts.license_scan import (
    _license_allowed,
    app_lock_failures,
    node_lock_components,
    ouroboros_components,
    python_lock_components,
)


def test_lock_inventory_uses_installed_metadata_and_version_bound_override(
    tmp_path: pathlib.Path,
) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "alpha"\nversion = "1.0"\n'
        '[[package]]\nname = "platform-only"\nversion = "2.0"\n',
        encoding="utf-8",
    )
    rows = python_lock_components(
        lock,
        installed={"alpha": {"name": "alpha", "version": "1.0", "license": "MIT"}},
        overrides={
            "platform-only@2.0": {
                "license": "BSD-3-Clause",
                "reason": "test override",
            }
        },
    )

    assert rows == [
        {
            "ecosystem": "python",
            "name": "alpha",
            "version": "1.0",
            "license": "MIT",
            "license_source": "installed_metadata",
        },
        {
            "ecosystem": "python",
            "name": "platform-only",
            "version": "2.0",
            "license": "BSD-3-Clause",
            "license_source": "version_bound_override",
        },
    ]


def test_node_and_runtime_inventory_preserve_exact_locked_versions(tmp_path: pathlib.Path) -> None:
    node = tmp_path / "package-lock.json"
    node.write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "root"},
                    "node_modules/example": {"version": "3.0.0", "license": "ISC"},
                    "node_modules/@communication-factory/web": {"link": True},
                }
            }
        ),
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements.lock"
    requirements.write_text(
        "example-runtime==4.0.0 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
    )

    assert node_lock_components(node) == [
        {
            "ecosystem": "npm",
            "name": "example",
            "version": "3.0.0",
            "license": "ISC",
            "license_source": "package_lock_metadata",
        }
    ]
    runtime = ouroboros_components(
        requirements,
        installed={
            "example-runtime": {
                "name": "example-runtime",
                "version": "4.0.0",
                "license": "Apache-2.0",
            }
        },
    )
    assert runtime[0]["installed_version_matches"] is True
    assert runtime[0]["license"] == "Apache-2.0"
    assert runtime[-1]["name"] == "ouroboros"


def test_license_policy_and_app_lock_mismatch_fail_closed(tmp_path: pathlib.Path) -> None:
    app_lock = tmp_path / "requirements.lock"
    app_lock.write_text("alpha==2.0.0\nmissing==1.0.0\n", encoding="utf-8")
    components = [
        {
            "ecosystem": "python",
            "name": "alpha",
            "version": "1.0.0",
            "license": "MIT",
        }
    ]

    assert _license_allowed("MIT") is True
    assert _license_allowed("Apache-2.0 OR GPL-2.0-or-later") is True
    assert _license_allowed("AGPL-3.0-only") is False
    assert _license_allowed("") is False
    assert app_lock_failures(components, app_lock) == [
        {
            "ecosystem": "app-python",
            "name": "alpha",
            "reason": "app_lock_version_mismatch",
        },
        {
            "ecosystem": "app-python",
            "name": "missing",
            "reason": "app_lock_missing_from_uv_lock",
        },
    ]
