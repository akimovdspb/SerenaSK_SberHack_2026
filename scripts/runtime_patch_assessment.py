from __future__ import annotations

import importlib.util
import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any

from apps.api.app.domain.models import ContextGetRequest, DraftSaveRequest
from apps.api.app.ouroboros_client import ALLOWED_PROVIDER_TOOLS

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "ouroboros" / "runtime" / "strict_tool_adapter.py"


@dataclass(frozen=True, order=True)
class StrictSchemaIssue:
    tool_name: str
    path: str
    code: str
    detail: str


UNSUPPORTED_COMPOSITION_KEYS = frozenset(
    {
        "allOf",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "if",
        "not",
        "oneOf",
        "then",
    }
)


def _is_constrained_schema(schema: dict[str, Any]) -> bool:
    return any(key in schema for key in ("type", "$ref", "anyOf", "enum", "const"))


def assess_parameter_schema(tool_name: str, schema: dict[str, Any]) -> list[StrictSchemaIssue]:
    issues: list[StrictSchemaIssue] = []

    def add(path: str, code: str, detail: str) -> None:
        issues.append(StrictSchemaIssue(tool_name, path, code, detail))

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            add(path, "SCHEMA_NODE_INVALID", "schema node is not an object")
            return

        for key in sorted(UNSUPPORTED_COMPOSITION_KEYS.intersection(node)):
            add(path, "UNSUPPORTED_KEYWORD", key)
        if "discriminator" in node:
            add(path, "UNSUPPORTED_KEYWORD", "discriminator")
        if not _is_constrained_schema(node):
            add(path, "UNCONSTRAINED_VALUE", "no supported type, ref, anyOf, enum or const")

        properties = node.get("properties")
        if node.get("type") == "object" or isinstance(properties, dict):
            if node.get("additionalProperties") is not False:
                add(
                    path,
                    "ADDITIONAL_PROPERTIES_NOT_FALSE",
                    repr(node.get("additionalProperties", "<missing>")),
                )
            property_names = list(properties) if isinstance(properties, dict) else []
            required = node.get("required")
            required_names = set(required) if isinstance(required, list) else set()
            missing = [name for name in property_names if name not in required_names]
            if missing:
                add(path, "PROPERTIES_NOT_REQUIRED", ",".join(missing))

        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for name, child in definitions.items():
                walk(child, f"{path}/$defs/{name}")
        legacy_definitions = node.get("definitions")
        if isinstance(legacy_definitions, dict):
            for name, child in legacy_definitions.items():
                walk(child, f"{path}/definitions/{name}")
        if isinstance(properties, dict):
            for name, child in properties.items():
                walk(child, f"{path}/properties/{name}")
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}/items")
        for keyword in ("anyOf", "oneOf", "allOf"):
            variants = node.get(keyword)
            if isinstance(variants, list):
                for index, child in enumerate(variants):
                    walk(child, f"{path}/{keyword}/{index}")

    if schema.get("type") != "object":
        add("$", "ROOT_NOT_OBJECT", repr(schema.get("type")))
    walk(schema, "$")
    return sorted(set(issues))


def assess_function_tool(tool: dict[str, Any]) -> list[StrictSchemaIssue]:
    function = tool.get("function")
    if not isinstance(function, dict):
        return [StrictSchemaIssue("<unknown>", "$", "FUNCTION_INVALID", "missing function")]
    tool_name = str(function.get("name") or "<unknown>")
    issues: list[StrictSchemaIssue] = []
    if function.get("strict") is not True:
        issues.append(
            StrictSchemaIssue(tool_name, "$/function", "FUNCTION_NOT_STRICT", "strict is not true")
        )
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        issues.append(
            StrictSchemaIssue(
                tool_name,
                "$/function/parameters",
                "PARAMETERS_INVALID",
                "missing parameter schema",
            )
        )
    else:
        issues.extend(assess_parameter_schema(tool_name, parameters))
    return sorted(set(issues))


def raw_provider_tools() -> list[dict[str, Any]]:
    parameter_schemas = (
        ContextGetRequest.model_json_schema(),
        DraftSaveRequest.model_json_schema(),
    )
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": parameter_schema,
            },
        }
        for name, parameter_schema in zip(ALLOWED_PROVIDER_TOOLS, parameter_schemas, strict=True)
    ]


def _load_adapter() -> Any:
    spec = importlib.util.spec_from_file_location("cf_strict_tool_adapter", ADAPTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load CF-RP-001 strict tool adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_provider_tools() -> list[dict[str, Any]]:
    adapter = _load_adapter()
    return list(adapter.adapt_provider_tools(raw_provider_tools()))


def raw_assessment() -> list[StrictSchemaIssue]:
    return sorted(issue for tool in raw_provider_tools() for issue in assess_function_tool(tool))


def current_assessment() -> list[StrictSchemaIssue]:
    return sorted(
        issue for tool in current_provider_tools() for issue in assess_function_tool(tool)
    )


def main() -> int:
    adapter = _load_adapter()
    before = raw_assessment()
    issues = current_assessment()
    print(
        json.dumps(
            {
                "schema_version": 1,
                "decision": "CF-RP-001_ACTIVE",
                "adapter_hash": adapter.adapter_source_hash(),
                "provider_tool_names": ALLOWED_PROVIDER_TOOLS,
                "strict_compatible": not issues,
                "pre_adapter_issue_count": len(before),
                "issue_count": len(issues),
                "issues": [asdict(issue) for issue in issues],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
