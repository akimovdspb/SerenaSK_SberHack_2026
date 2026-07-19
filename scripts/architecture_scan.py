from __future__ import annotations

import ast
import pathlib
import sys

from scripts.compose_contract import PROVIDER_ENV_NAMES, load_rendered_compose

ROOT = pathlib.Path(__file__).resolve().parents[1]
BANNED_PROVIDER_MODULES = {
    "anthropic",
    "google.generativeai",
    "litellm",
    "openai",
    "openrouter",
}
BANNED_PROVIDER_FRAGMENTS = {
    "api.anthropic.com",
    "api.openai.com",
    "openrouter.ai/api",
    "/v1/chat/completions",
    "/v1/responses",
}


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def scan_backend(root: pathlib.Path) -> list[str]:
    errors: list[str] = []
    app_root = root / "apps" / "api" / "app"
    for path in sorted(app_root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"backend source could not be parsed: {path.name}: {type(exc).__name__}")
            continue
        imports = _import_roots(tree)
        for imported in imports:
            if any(
                imported == banned or imported.startswith(f"{banned}.")
                for banned in BANNED_PROVIDER_MODULES
            ):
                errors.append(f"backend imports direct provider module in {path.name}")
        lowered = source.lower()
        if any(fragment in lowered for fragment in BANNED_PROVIDER_FRAGMENTS):
            errors.append(f"backend contains a direct provider endpoint in {path.name}")
        if any(name in source for name in PROVIDER_ENV_NAMES):
            errors.append(f"backend references a provider credential variable in {path.name}")

    requirements = (root / "apps" / "requirements.lock").read_text(encoding="utf-8")
    for banned in BANNED_PROVIDER_MODULES:
        package = banned.split(".", 1)[0].replace("_", "-")
        if f"{package}==" in requirements.lower():
            errors.append(f"backend dependency lock includes provider SDK {package}")

    compose = load_rendered_compose()
    app = (compose.get("services") or {}).get("app") or {}
    app_environment = app.get("environment") or {}
    if set(app_environment) & PROVIDER_ENV_NAMES:
        errors.append("app Compose service receives a provider credential variable")
    return errors


def main() -> int:
    try:
        errors = scan_backend(ROOT)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"architecture-scan: FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"architecture-scan: FAIL: {error}", file=sys.stderr)
        return 1
    print(
        "architecture-scan: PASS backend_llm=false provider_sdk=false "
        "provider_credentials=ouroboros_only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
