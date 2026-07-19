from __future__ import annotations

import copy
import functools
import hashlib
import importlib
import json
import pathlib
from collections.abc import Sequence
from typing import Any

DECISION_ID = "CF-RP-001"
TARGET_TOOL_NAMES = (
    "mcp_factory__cf_context_get",
    "mcp_factory__cf_draft_save",
)
UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "allOf",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "if",
        "not",
        "oneOf",
        "patternProperties",
        "then",
    }
)
SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)
SUPPORTED_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
SUPPORTED_FORMATS = frozenset(
    {"date", "date-time", "duration", "email", "hostname", "ipv4", "ipv6", "time", "uuid"}
)
MAX_OBJECT_PROPERTIES = 5_000
MAX_OBJECT_DEPTH = 10
MAX_SCHEMA_STRING_LENGTH = 120_000
MAX_ENUM_VALUES = 1_000
MAX_LARGE_ENUM_STRING_LENGTH = 15_000


class StrictToolAdapterError(RuntimeError):
    """The provider schema cannot be made strict within CF-RP-001."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def adapter_source_hash(path: pathlib.Path | None = None) -> str:
    source = path or pathlib.Path(__file__)
    return hashlib.sha256(source.read_bytes()).hexdigest()


def tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function")
    return str(function.get("name") or "").strip() if isinstance(function, dict) else ""


def _normalize_schema_node(node: Any) -> Any:
    if isinstance(node, list):
        return [_normalize_schema_node(item) for item in node]
    if not isinstance(node, dict):
        return copy.deepcopy(node)

    if "oneOf" in node and "anyOf" in node:
        raise StrictToolAdapterError("schema node contains both oneOf and anyOf")

    normalized: dict[str, Any] = {}
    for key, value in node.items():
        if key in {"default", "discriminator"}:
            continue
        normalized_key = "anyOf" if key == "oneOf" else key
        normalized[normalized_key] = _normalize_schema_node(value)

    if "$ref" in normalized and len(normalized) > 1:
        siblings = set(normalized) - {"$ref"}
        if not siblings.issubset({"description", "title"}):
            raise StrictToolAdapterError(
                f"$ref has unsupported sibling keywords: {','.join(sorted(siblings))}"
            )
        reference = normalized.pop("$ref")
        normalized["anyOf"] = [{"$ref": reference}]

    properties = normalized.get("properties")
    if normalized.get("type") == "object" or isinstance(properties, dict):
        normalized["additionalProperties"] = False
        normalized["required"] = list(properties) if isinstance(properties, dict) else []
    return normalized


def _local_ref_target(root: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    current: Any = root
    for raw_token in ref[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current if isinstance(current, dict) else None


def strict_schema_metrics(schema: dict[str, Any]) -> dict[str, int]:
    property_count = 0
    enum_value_count = 0
    schema_string_length = 0
    largest_enum_string_length = 0

    def collect(node: Any) -> None:
        nonlocal property_count
        nonlocal enum_value_count
        nonlocal schema_string_length
        nonlocal largest_enum_string_length
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            property_count += len(properties)
            schema_string_length += sum(len(str(name)) for name in properties)
            for child in properties.values():
                collect(child)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            schema_string_length += sum(len(str(name)) for name in definitions)
            for child in definitions.values():
                collect(child)
        enum = node.get("enum")
        if isinstance(enum, list):
            enum_value_count += len(enum)
            enum_length = sum(len(value) for value in enum if isinstance(value, str))
            schema_string_length += enum_length
            largest_enum_string_length = max(largest_enum_string_length, enum_length)
        const = node.get("const")
        if isinstance(const, str):
            schema_string_length += len(const)
        items = node.get("items")
        if isinstance(items, dict):
            collect(items)
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            for child in any_of:
                collect(child)

    def object_depth(node: Any, depth: int, ref_stack: frozenset[str]) -> int:
        if not isinstance(node, dict):
            return depth
        current_depth = depth + int(
            node.get("type") == "object" or isinstance(node.get("properties"), dict)
        )
        maximum = current_depth
        ref = node.get("$ref")
        if isinstance(ref, str) and ref not in ref_stack:
            target = _local_ref_target(schema, ref)
            if target is not None:
                maximum = max(
                    maximum,
                    object_depth(target, current_depth, ref_stack | {ref}),
                )
        properties = node.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                maximum = max(maximum, object_depth(child, current_depth, ref_stack))
        items = node.get("items")
        if isinstance(items, dict):
            maximum = max(maximum, object_depth(items, current_depth, ref_stack))
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            for child in any_of:
                maximum = max(maximum, object_depth(child, current_depth, ref_stack))
        return maximum

    collect(schema)
    max_depth = object_depth(schema, 0, frozenset())
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        max_depth = max(
            [max_depth, *(object_depth(child, 0, frozenset()) for child in definitions.values())]
        )
    return {
        "enum_value_count": enum_value_count,
        "largest_enum_string_length": largest_enum_string_length,
        "max_object_depth": max_depth,
        "object_property_count": property_count,
        "schema_string_length": schema_string_length,
    }


def strict_parameter_schema_issues(schema: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    def add(path: str, code: str, detail: str) -> None:
        issues.append(f"{path}:{code}:{detail}")

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            add(path, "SCHEMA_NODE_INVALID", type(node).__name__)
            return

        for key in sorted(set(node) - SUPPORTED_SCHEMA_KEYS):
            add(path, "UNSUPPORTED_KEYWORD", key)
        for key in sorted(UNSUPPORTED_SCHEMA_KEYS.intersection(node)):
            add(path, "UNSUPPORTED_KEYWORD", key)
        if "discriminator" in node:
            add(path, "UNSUPPORTED_KEYWORD", "discriminator")
        if "default" in node:
            add(path, "DEFAULT_PRESENT", "default")
        if not any(key in node for key in ("type", "$ref", "anyOf", "enum", "const")):
            add(path, "UNCONSTRAINED_VALUE", "missing supported type constraint")

        ref = node.get("$ref")
        if ref is not None and (not isinstance(ref, str) or _local_ref_target(schema, ref) is None):
            add(path, "UNRESOLVED_REF", repr(ref))
        if ref is not None and set(node) != {"$ref"}:
            add(path, "REF_SIBLING_KEYWORDS", ",".join(sorted(set(node) - {"$ref"})))

        raw_type = node.get("type")
        type_names = raw_type if isinstance(raw_type, list) else [raw_type]
        if raw_type is not None and (
            not type_names
            or any(not isinstance(name, str) or name not in SUPPORTED_TYPES for name in type_names)
            or len(type_names) != len(set(type_names))
        ):
            add(path, "TYPE_INVALID", repr(raw_type))
        raw_format = node.get("format")
        if raw_format is not None and raw_format not in SUPPORTED_FORMATS:
            add(path, "FORMAT_UNSUPPORTED", repr(raw_format))

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
            if (
                not isinstance(required, list)
                or len(required) != len(set(required))
                or set(required) != set(property_names)
            ):
                add(path, "REQUIRED_SET_MISMATCH", ",".join(property_names))

        any_of = node.get("anyOf")
        if any_of is not None and (not isinstance(any_of, list) or not any_of):
            add(path, "ANY_OF_INVALID", repr(any_of))

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
        if items is not None:
            walk(items, f"{path}/items")
        if isinstance(any_of, list):
            for index, child in enumerate(any_of):
                walk(child, f"{path}/anyOf/{index}")

    if schema.get("type") != "object":
        add("$", "ROOT_NOT_OBJECT", repr(schema.get("type")))
    if "anyOf" in schema:
        add("$", "ROOT_ANY_OF_UNSUPPORTED", "anyOf")
    walk(schema, "$")
    metrics = strict_schema_metrics(schema)
    if metrics["object_property_count"] > MAX_OBJECT_PROPERTIES:
        add("$", "PROPERTY_LIMIT_EXCEEDED", str(metrics["object_property_count"]))
    if metrics["max_object_depth"] > MAX_OBJECT_DEPTH:
        add("$", "DEPTH_LIMIT_EXCEEDED", str(metrics["max_object_depth"]))
    if metrics["schema_string_length"] > MAX_SCHEMA_STRING_LENGTH:
        add("$", "STRING_LIMIT_EXCEEDED", str(metrics["schema_string_length"]))
    if metrics["enum_value_count"] > MAX_ENUM_VALUES:
        add("$", "ENUM_LIMIT_EXCEEDED", str(metrics["enum_value_count"]))
    if metrics["largest_enum_string_length"] > MAX_LARGE_ENUM_STRING_LENGTH:
        add(
            "$",
            "LARGE_ENUM_STRING_LIMIT_EXCEEDED",
            str(metrics["largest_enum_string_length"]),
        )
    return sorted(set(issues))


def normalize_parameter_schema(tool: str, schema: dict[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(schema)
    normalized = _normalize_schema_node(schema)
    if schema != source:
        raise StrictToolAdapterError(f"source schema mutated while adapting {tool}")
    if not isinstance(normalized, dict):
        raise StrictToolAdapterError(f"parameter schema is not an object for {tool}")
    issues = strict_parameter_schema_issues(normalized)
    if issues:
        raise StrictToolAdapterError(f"strict schema rejected for {tool}: {'; '.join(issues)}")
    return normalized


def strict_function_tool_issues(tool: dict[str, Any]) -> list[str]:
    function = tool.get("function")
    if not isinstance(function, dict):
        return ["$/function:FUNCTION_INVALID:missing function object"]
    issues: list[str] = []
    if function.get("strict") is not True:
        issues.append("$/function:FUNCTION_NOT_STRICT:strict is not true")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        issues.append("$/function/parameters:PARAMETERS_INVALID:missing object schema")
    else:
        issues.extend(strict_parameter_schema_issues(parameters))
    return sorted(set(issues))


def adapt_provider_tools(
    tools: Sequence[dict[str, Any]],
    *,
    require_targets: bool = True,
) -> list[dict[str, Any]]:
    source_snapshot = copy.deepcopy(list(tools))
    names = [tool_name(tool) for tool in tools]
    target_counts = {name: names.count(name) for name in TARGET_TOOL_NAMES}
    if any(count > 1 for count in target_counts.values()):
        raise StrictToolAdapterError("factory provider tool set contains a duplicate target")
    if require_targets and any(count != 1 for count in target_counts.values()):
        missing = [name for name, count in target_counts.items() if count != 1]
        raise StrictToolAdapterError(
            f"factory provider tool set is incomplete: {','.join(missing)}"
        )

    adapted: list[dict[str, Any]] = []
    for source_tool in tools:
        name = tool_name(source_tool)
        if name not in TARGET_TOOL_NAMES:
            adapted.append(source_tool)
            continue
        target = copy.deepcopy(source_tool)
        function = target.get("function")
        if not isinstance(function, dict):
            raise StrictToolAdapterError(f"function schema is missing for {name}")
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            raise StrictToolAdapterError(f"parameter schema is missing for {name}")
        function["parameters"] = normalize_parameter_schema(name, parameters)
        function["strict"] = True
        issues = strict_function_tool_issues(target)
        if issues:
            raise StrictToolAdapterError(
                f"strict function rejected for {name}: {'; '.join(issues)}"
            )
        adapted.append(target)

    if list(tools) != source_snapshot:
        raise StrictToolAdapterError("source provider tool list was mutated")
    for before, after in zip(tools, adapted, strict=True):
        if tool_name(before) not in TARGET_TOOL_NAMES and canonical_json(before) != canonical_json(
            after
        ):
            raise StrictToolAdapterError("non-target provider schema changed")
    return adapted


def adapter_metadata() -> dict[str, Any]:
    return {
        "active": True,
        "decision_id": DECISION_ID,
        "adapter_hash": adapter_source_hash(),
        "target_tool_names": list(TARGET_TOOL_NAMES),
    }


def install_strict_tool_adapter(registry_class: type[Any] | None = None) -> dict[str, Any]:
    if registry_class is None:
        registry_module: Any = importlib.import_module("ouroboros.tools.registry")
        registry_class = registry_module.ToolRegistry
    if getattr(registry_class, "_communication_factory_strict_adapter", "") == DECISION_ID:
        return adapter_metadata()

    original_schemas = getattr(registry_class, "schemas", None)
    original_get_schema = getattr(registry_class, "get_schema_by_name", None)
    if not callable(original_schemas) or not callable(original_get_schema):
        raise StrictToolAdapterError("pinned registry surface is incompatible with CF-RP-001")

    @functools.wraps(original_schemas)
    def strict_schemas(self: Any, core_only: bool = False) -> list[dict[str, Any]]:
        discovered = original_schemas(self, core_only=core_only)
        if not isinstance(discovered, list) or not all(
            isinstance(item, dict) for item in discovered
        ):
            raise StrictToolAdapterError("pinned registry returned an invalid schema list")
        return adapt_provider_tools(discovered, require_targets=not core_only)

    @functools.wraps(original_get_schema)
    def strict_get_schema(self: Any, name: str) -> dict[str, Any] | None:
        discovered = original_get_schema(self, name)
        if name not in TARGET_TOOL_NAMES:
            return discovered
        if not isinstance(discovered, dict):
            raise StrictToolAdapterError(f"required factory provider tool is absent: {name}")
        return adapt_provider_tools([discovered], require_targets=False)[0]

    registry_class.schemas = strict_schemas
    registry_class.get_schema_by_name = strict_get_schema
    registry_class._communication_factory_strict_adapter = DECISION_ID
    return adapter_metadata()
