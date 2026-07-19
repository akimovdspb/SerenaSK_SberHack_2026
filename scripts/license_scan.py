from __future__ import annotations

import hashlib
import importlib.metadata
import json
import pathlib
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "runtime" / "security" / "dependencies.json"
NOTICES_PATH = ROOT / "runtime" / "security" / "THIRD_PARTY_NOTICES.generated.md"
OVERRIDES_PATH = ROOT / "data" / "dependency_license_overrides.json"
REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
PROHIBITED_LICENSE_MARKERS = ("AGPL", "SSPL", "BUSL", "PROPRIETARY", "COMMONS CLAUSE")
INPUT_PATHS = (
    ROOT / "uv.lock",
    ROOT / "package-lock.json",
    ROOT / "apps" / "requirements.lock",
    ROOT / "ouroboros" / "requirements.lock",
    OVERRIDES_PATH,
)


class LicenseScanError(RuntimeError):
    pass


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _metadata_license(metadata: Any) -> str:
    expression = str(metadata.get("License-Expression") or "").strip()
    raw_license = str(metadata.get("License") or "").strip()
    classifiers = [
        str(item).split(" :: ")[-1]
        for item in metadata.get_all("Classifier", [])
        if str(item).startswith("License ::")
    ]
    if expression:
        return expression
    if raw_license and raw_license.casefold() not in {"unknown", "dual license"}:
        if raw_license.startswith("The MIT License"):
            return "MIT"
        return raw_license
    return " OR ".join(sorted(set(classifiers)))


def _installed_python() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").strip()
        if not name:
            continue
        result[_canonical_name(name)] = {
            "name": name,
            "version": distribution.version,
            "license": _metadata_license(distribution.metadata),
        }
    return result


def _overrides(path: pathlib.Path = OVERRIDES_PATH) -> dict[str, dict[str, str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseScanError("dependency license overrides are unreadable") from exc
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, dict) or document.get("schema_version") != 1:
        raise LicenseScanError("dependency license overrides schema is invalid")
    result: dict[str, dict[str, str]] = {}
    for identity, raw in packages.items():
        if not isinstance(identity, str) or not isinstance(raw, dict):
            raise LicenseScanError("dependency license override row is invalid")
        license_name = str(raw.get("license") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if not license_name or not reason:
            raise LicenseScanError("dependency license override is incomplete")
        result[identity.casefold()] = {"license": license_name, "reason": reason}
    return result


def python_lock_components(
    lock_path: pathlib.Path = ROOT / "uv.lock",
    *,
    installed: dict[str, dict[str, str]] | None = None,
    overrides: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    try:
        document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LicenseScanError("uv.lock is unreadable") from exc
    raw_packages = document.get("package")
    if not isinstance(raw_packages, list):
        raise LicenseScanError("uv.lock has no package list")
    installed_rows = installed if installed is not None else _installed_python()
    override_rows = overrides if overrides is not None else _overrides()
    components: list[dict[str, Any]] = []
    for raw in raw_packages:
        if not isinstance(raw, dict):
            raise LicenseScanError("uv.lock package row is malformed")
        name = str(raw.get("name") or "")
        version = str(raw.get("version") or "")
        if not name or not version or name == "communication-factory":
            continue
        installed_row = installed_rows.get(_canonical_name(name))
        identity = f"{name}@{version}".casefold()
        override = override_rows.get(identity)
        if installed_row is not None and installed_row.get("version") == version:
            license_name = installed_row.get("license", "")
            source = "installed_metadata"
        elif override is not None:
            license_name = override["license"]
            source = "version_bound_override"
        else:
            license_name = ""
            source = "missing"
        components.append(
            {
                "ecosystem": "python",
                "name": name,
                "version": version,
                "license": license_name,
                "license_source": source,
            }
        )
    return sorted(components, key=lambda item: (str(item["name"]), str(item["version"])))


def node_lock_components(
    lock_path: pathlib.Path = ROOT / "package-lock.json",
) -> list[dict[str, Any]]:
    try:
        document = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseScanError("package-lock.json is unreadable") from exc
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, dict):
        raise LicenseScanError("package-lock.json has no packages object")
    components: list[dict[str, Any]] = []
    for path, raw in packages.items():
        if not isinstance(path, str) or not isinstance(raw, dict) or "node_modules/" not in path:
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        if name == "@communication-factory/web" or raw.get("link") is True:
            continue
        components.append(
            {
                "ecosystem": "npm",
                "name": name,
                "version": str(raw.get("version") or ""),
                "license": str(raw.get("license") or "").strip(),
                "license_source": "package_lock_metadata",
            }
        )
    return sorted(components, key=lambda item: (str(item["name"]), str(item["version"])))


def _requirements(path: pathlib.Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = REQUIREMENT_RE.match(line)
        if match:
            result[_canonical_name(match.group(1))] = match.group(2)
    if not result:
        raise LicenseScanError(f"{path.name} has no pinned requirements")
    return result


def _runtime_distributions() -> dict[str, dict[str, str]]:
    program = (
        "import importlib.metadata as m,json;"
        "rows=[];"
        "[(rows.append({'name':d.metadata.get('Name') or '',"
        "'version':d.version,'expression':d.metadata.get('License-Expression') or '',"
        "'license':d.metadata.get('License') or '',"
        "'classifiers':d.metadata.get_all('Classifier',[])})) for d in m.distributions()];"
        "print(json.dumps(rows))"
    )
    process = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            "communication-factory/ouroboros:v6.61.4",
            "-c",
            program,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise LicenseScanError("pinned Ouroboros image metadata is unavailable; run make build")
    try:
        rows = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise LicenseScanError("pinned Ouroboros image metadata is malformed") from exc
    result: dict[str, dict[str, str]] = {}
    if not isinstance(rows, list):
        raise LicenseScanError("pinned Ouroboros image metadata is not a list")
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        metadata = _DistributionMetadata(raw)
        result[_canonical_name(name)] = {
            "name": name,
            "version": str(raw.get("version") or ""),
            "license": _metadata_license(metadata),
        }
    return result


class _DistributionMetadata:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def get(self, key: str, default: Any = None) -> Any:
        mapping = {
            "License-Expression": self._raw.get("expression"),
            "License": self._raw.get("license"),
        }
        return mapping.get(key, default)

    def get_all(self, key: str, default: Any = None) -> Any:
        return self._raw.get("classifiers", default) if key == "Classifier" else default


def ouroboros_components(
    lock_path: pathlib.Path = ROOT / "ouroboros" / "requirements.lock",
    *,
    installed: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    required = _requirements(lock_path)
    available = installed if installed is not None else _runtime_distributions()
    components: list[dict[str, Any]] = []
    for canonical, version in sorted(required.items()):
        row = available.get(canonical)
        components.append(
            {
                "ecosystem": "ouroboros-python",
                "name": row.get("name", canonical) if row else canonical,
                "version": version,
                "license": row.get("license", "") if row else "",
                "license_source": "pinned_image_metadata" if row else "missing",
                "installed_version_matches": bool(row and row.get("version") == version),
            }
        )
    components.append(
        {
            "ecosystem": "ouroboros-source",
            "name": "ouroboros",
            "version": "v6.61.4",
            "license": "MIT (upstream declaration; tag license file missing)",
            "license_source": "ouroboros.lock_and_THIRD_PARTY_NOTICES",
            "installed_version_matches": True,
        }
    )
    return components


def _license_allowed(value: str) -> bool:
    normalized = value.upper().strip()
    if not normalized:
        return False
    if any(marker in normalized for marker in PROHIBITED_LICENSE_MARKERS):
        return False
    return not ("GPL" in normalized and " OR " not in normalized)


def app_lock_failures(
    components: list[dict[str, Any]],
    lock_path: pathlib.Path = ROOT / "apps" / "requirements.lock",
) -> list[dict[str, str]]:
    uv_versions = {
        _canonical_name(str(item["name"])): str(item["version"])
        for item in components
        if item.get("ecosystem") == "python"
    }
    failures: list[dict[str, str]] = []
    for name, version in sorted(_requirements(lock_path).items()):
        if name not in uv_versions:
            failures.append(
                {
                    "ecosystem": "app-python",
                    "name": name,
                    "reason": "app_lock_missing_from_uv_lock",
                }
            )
        elif uv_versions[name] != version:
            failures.append(
                {
                    "ecosystem": "app-python",
                    "name": name,
                    "reason": "app_lock_version_mismatch",
                }
            )
    return failures


def run_license_scan() -> dict[str, Any]:
    python_components = python_lock_components()
    components = [
        *python_components,
        *node_lock_components(),
        *ouroboros_components(),
    ]
    failures: list[dict[str, str]] = []
    for item in components:
        if not _license_allowed(str(item.get("license") or "")):
            failures.append(
                {
                    "ecosystem": str(item["ecosystem"]),
                    "name": str(item["name"]),
                    "reason": "missing_or_prohibited_license",
                }
            )
        if item.get("installed_version_matches") is False:
            failures.append(
                {
                    "ecosystem": str(item["ecosystem"]),
                    "name": str(item["name"]),
                    "reason": "pinned_image_version_mismatch",
                }
            )
    failures.extend(app_lock_failures(python_components))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if not failures else "FAIL",
        "component_count": len(components),
        "failure_count": len(failures),
        "failures": failures,
        "components": components,
        "input_files": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": _sha256(path),
            }
            for path in INPUT_PATHS
        ],
        "lockfiles": [
            "uv.lock",
            "package-lock.json",
            "apps/requirements.lock",
            "ouroboros/requirements.lock",
        ],
    }


def _write_outputs(report: dict[str, Any]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    rows = sorted(
        report["components"],
        key=lambda item: (str(item["ecosystem"]), str(item["name"]), str(item["version"])),
    )
    NOTICES_PATH.write_text(
        "# Generated dependency inventory\n\n"
        "This machine-generated list supplements the repository's curated notices.\n\n"
        + "\n".join(
            f"- `{item['ecosystem']}:{item['name']}@{item['version']}` — {item['license']}"
            for item in rows
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    try:
        report = run_license_scan()
        _write_outputs(report)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"license-scan: FAIL: {exc}", file=sys.stderr)
        return 1
    if report["status"] != "PASS":
        print(
            f"license-scan: FAIL components={report['component_count']} "
            f"findings={report['failure_count']}",
            file=sys.stderr,
        )
        return 1
    print(f"license-scan: PASS components={report['component_count']} locks=4 prohibited=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
