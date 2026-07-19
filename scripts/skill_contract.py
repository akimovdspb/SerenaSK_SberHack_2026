from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys
from dataclasses import dataclass

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "ouroboros" / "skills" / "communication_factory" / "SKILL.md"
PROJECTION_PATH = ROOT / "prompts" / "communication_factory.ru.md"
MARKER = "COMMUNICATION_FACTORY_CONTRACT_V1"
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class SkillContract:
    manifest: dict[str, object]
    body: str
    body_bytes: bytes
    prompt_hash: str
    skill_file_hash: str


def normalize_projection(body: str) -> bytes:
    return body.strip().encode("utf-8") + b"\n"


def load_contract() -> SkillContract:
    source = SKILL_PATH.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(source.lstrip("\ufeff"))
    if match is None:
        raise ValueError("SKILL.md must contain YAML frontmatter and a body")
    manifest_raw = yaml.safe_load(match.group(1)) or {}
    if not isinstance(manifest_raw, dict):
        raise ValueError("skill frontmatter must be a mapping")
    body = match.group(2).strip()
    body_bytes = normalize_projection(body)
    return SkillContract(
        manifest=manifest_raw,
        body=body,
        body_bytes=body_bytes,
        prompt_hash=hashlib.sha256(body_bytes).hexdigest(),
        skill_file_hash=hashlib.sha256(SKILL_PATH.read_bytes()).hexdigest(),
    )


def validate_contract(*, write_projection: bool = False) -> SkillContract:
    contract = load_contract()
    required_manifest = {
        "name": "communication_factory",
        "type": "instruction",
        "version": "1.0.0",
    }
    for key, expected in required_manifest.items():
        if contract.manifest.get(key) != expected:
            raise ValueError(f"manifest.{key} must equal {expected!r}")
    if contract.body.splitlines()[0] != MARKER:
        raise ValueError("the exact contract marker must be the first body line")
    if write_projection:
        PROJECTION_PATH.write_bytes(contract.body_bytes)
    if not PROJECTION_PATH.is_file():
        raise ValueError("generated prompt projection is missing")
    if PROJECTION_PATH.read_bytes() != contract.body_bytes:
        raise ValueError("generated prompt projection is not byte-equal to the skill body")
    return contract


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the single-source skill prompt")
    parser.add_argument("--write", action="store_true", help="regenerate the projection")
    args = parser.parse_args()
    try:
        contract = validate_contract(write_projection=args.write)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"skill-contract: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "skill-contract: PASS "
        f"name={contract.manifest['name']} version={contract.manifest['version']} "
        f"activation=adapter_injected prompt_sha256={contract.prompt_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
