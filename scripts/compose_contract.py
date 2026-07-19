from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any

from provider_profiles import CANONICAL_PROFILE_NAME, ProviderProfile, provider_profile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROVIDER_ENV_NAMES = {
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
}


def load_rendered_compose() -> dict[str, Any]:
    process = subprocess.run(
        ["docker", "compose", "--profile", "tools", "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            "docker compose config failed; output withheld because it may contain secrets"
        )
    parsed = json.loads(process.stdout)
    if not isinstance(parsed, dict):
        raise RuntimeError("docker compose config did not return an object")
    return parsed


def _service_networks(service: dict[str, Any]) -> set[str]:
    value = service.get("networks") or {}
    if isinstance(value, dict):
        return set(value)
    if isinstance(value, list):
        return {str(item) for item in value}
    return set()


def _environment_keys(service: dict[str, Any]) -> set[str]:
    value = service.get("environment") or {}
    if isinstance(value, dict):
        return set(value)
    return set()


def _mounts(service: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in service.get("volumes") or [] if isinstance(item, dict)]


def validate_compose(
    config: dict[str, Any],
    *,
    selected_profile: ProviderProfile | None = None,
) -> list[str]:
    errors: list[str] = []
    profile = selected_profile or provider_profile(CANONICAL_PROFILE_NAME)
    services = config.get("services") or {}
    required = {"gateway", "app", "ouroboros", "contract-probe"}
    if set(services) != required:
        errors.append("service set must be gateway/app/ouroboros/contract-probe")
        return errors

    gateway = services["gateway"]
    app = services["app"]
    ouroboros = services["ouroboros"]
    probe = services["contract-probe"]
    expected_runtime_policies = {
        "gateway": ("unless-stopped", 0.5, 256 * 1024 * 1024, 128),
        "app": ("unless-stopped", 1.0, 768 * 1024 * 1024, 256),
        "ouroboros": ("unless-stopped", 2.5, 4 * 1024 * 1024 * 1024, 512),
        "contract-probe": ("no", 1.0, 1024 * 1024 * 1024, 256),
    }
    for name, (restart, cpus, memory, pids) in expected_runtime_policies.items():
        service = services[name]
        logging = service.get("logging") or {}
        logging_options = logging.get("options") or {}
        if service.get("restart") != restart:
            errors.append(f"{name} restart policy is not canonical")
        if (
            float(service.get("cpus") or 0) != cpus
            or int(service.get("mem_limit") or 0) != memory
            or int(service.get("pids_limit") or 0) != pids
        ):
            errors.append(f"{name} resource limits are not canonical")
        if logging.get("driver") != "json-file" or logging_options != {
            "max-file": "3",
            "max-size": "10m",
        }:
            errors.append(f"{name} log rotation is not canonical")
    ports = gateway.get("ports") or []
    if len(ports) != 1:
        errors.append("gateway must publish exactly one port")
    else:
        port = ports[0]
        if not isinstance(port, dict) or (
            str(port.get("host_ip")) != "127.0.0.1"
            or int(port.get("target") or 0) != 8080
            or str(port.get("published")) != "8080"
        ):
            errors.append("gateway port must be 127.0.0.1:8080 -> 8080")
    for name in ("app", "ouroboros", "contract-probe"):
        if services[name].get("ports"):
            errors.append(f"{name} must not publish host ports")

    expected_networks = {
        "gateway": {"ingress", "edge"},
        "app": {"edge", "factory"},
        "ouroboros": {"factory", "provider_egress"},
        "contract-probe": {"factory"},
    }
    for name, expected in expected_networks.items():
        if _service_networks(services[name]) != expected:
            errors.append(f"{name} network set is not canonical")
    networks = config.get("networks") or {}
    for name in ("edge", "factory"):
        if not bool((networks.get(name) or {}).get("internal")):
            errors.append(f"{name} network must be internal")
    if bool((networks.get("provider_egress") or {}).get("internal")):
        errors.append("provider_egress must permit Ouroboros provider access")
    if bool((networks.get("ingress") or {}).get("internal")):
        errors.append("ingress must permit the gateway host-port binding")

    for name, service in services.items():
        env_keys = _environment_keys(service)
        leaked = env_keys & PROVIDER_ENV_NAMES
        if leaked:
            errors.append(f"{name} must not receive provider key values through Compose env")
        if name == "gateway" and "MCP_SHARED_TOKEN" in env_keys:
            errors.append("gateway must not receive the MCP token")
    ouroboros_environment = ouroboros.get("environment") or {}
    review_max_tokens = str(ouroboros_environment.get("OUROBOROS_REVIEW_MAX_TOKENS") or "")
    if not review_max_tokens.isdigit() or not 8_192 <= int(review_max_tokens) <= 65_536:
        errors.append("Ouroboros review output ceiling must stay within its official bounds")
    if profile.secret_file_env not in _environment_keys(ouroboros):
        errors.append("Ouroboros must receive only the non-secret key mount path")
    elif ouroboros_environment.get(profile.secret_file_env) != profile.secret_container_path:
        errors.append("Ouroboros provider key file path does not match the selected profile")
    if ouroboros.get("init") is not True:
        errors.append("Ouroboros must run behind the container init process")
    app_groups = [str(item) for item in app.get("group_add") or []]
    if len(app_groups) != 1 or not app_groups[0].isdigit():
        errors.append("app must receive only the contract-lock host group")
    ouroboros_groups = [str(item) for item in ouroboros.get("group_add") or []]
    ledger_gid = str(ouroboros_environment.get("CF_REQUEST_LEDGER_GID") or "")
    if len(ouroboros_groups) != 1 or not ledger_gid.isdigit() or ouroboros_groups[0] != ledger_gid:
        errors.append("Ouroboros ledger group must match its sole supplemental host group")

    key_mount_owners: list[str] = []
    for name, service in services.items():
        for mount in _mounts(service):
            if mount.get("target") == profile.secret_container_path:
                key_mount_owners.append(name)
                if mount.get("source") != profile.default_secret_host_path or not mount.get(
                    "read_only"
                ):
                    errors.append("provider key mount source/mode is not canonical")
    if key_mount_owners != ["ouroboros"]:
        errors.append("only Ouroboros may mount the provider key")

    def source_for_target(service: dict[str, Any], target: str) -> pathlib.Path | None:
        for mount in _mounts(service):
            if mount.get("target") == target:
                return pathlib.Path(str(mount.get("source") or "")).resolve()
        return None

    app_skill = source_for_target(app, "/skills/communication_factory")
    ouroboros_skills = source_for_target(ouroboros, "/skills")
    probe_skills = source_for_target(probe, "/skills")
    if (
        app_skill is None
        or ouroboros_skills is None
        or app_skill != ouroboros_skills / "communication_factory"
        or probe_skills != ouroboros_skills
    ):
        errors.append("skill payload mounts must resolve to the same host directory")

    if probe.get("profiles") != ["tools"]:
        errors.append("contract-probe must remain opt-in under the tools profile")
    if ouroboros.get("image") != probe.get("image"):
        errors.append("contract-probe and Ouroboros must use the same image reference")
    return errors


def validate_static_files() -> list[str]:
    errors: list[str] = []
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    required_ignored = {
        "OPENAI_API_KEY.txt",
        "OPENROUTER_API_KEY.txt",
        ".env",
        "secrets/",
        "private_sources/",
        "runtime/",
    }
    for pattern in required_ignored:
        if pattern not in dockerignore:
            errors.append(f".dockerignore is missing {pattern}")

    example: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            example[key] = value
    for key in (
        "APP_ACCESS_USERNAME",
        "APP_ACCESS_PASSWORD_HASH",
        "MCP_SHARED_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OUROBOROS_NETWORK_PASSWORD",
    ):
        if example.get(key) != "":
            errors.append(f"secret-bearing example value {key} must be empty")

    caddyfile = (ROOT / "gateway" / "Caddyfile").read_text(encoding="utf-8")
    if "reverse_proxy app:8000" not in caddyfile:
        errors.append("gateway must proxy public API only to app")
    if "reverse_proxy ouroboros" in caddyfile or "reverse_proxy app:8000/internal" in caddyfile:
        errors.append("gateway must not route private runtime surfaces")
    if "/internal/*" not in caddyfile or "/api/mcp*" not in caddyfile:
        errors.append("gateway must explicitly reject private paths")
    return errors


def main() -> int:
    try:
        errors = validate_compose(load_rendered_compose()) + validate_static_files()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"compose-contract: FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"compose-contract: FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "compose-contract: PASS published=127.0.0.1:8080 "
        "private=app,ouroboros,mcp provider_key_recipient=ouroboros"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
