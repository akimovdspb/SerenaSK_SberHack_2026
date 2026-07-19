from __future__ import annotations

import copy
import importlib.util
import pathlib
from typing import Any

import pytest

from scripts.runtime_patch_assessment import raw_provider_tools

ADAPTER_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "runtime" / "strict_tool_adapter.py"
)


def _load_adapter() -> Any:
    spec = importlib.util.spec_from_file_location("cf_test_strict_tool_adapter", ADAPTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load strict tool adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _by_name(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {tool["function"]["name"]: tool for tool in tools}


def _object_nodes(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_object_nodes(item))
    elif isinstance(node, dict):
        if node.get("type") == "object" or isinstance(node.get("properties"), dict):
            found.append(node)
        for value in node.values():
            found.extend(_object_nodes(value))
    return found


def _schema_nodes(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_schema_nodes(item))
    elif isinstance(node, dict):
        found.append(node)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for child in definitions.values():
                found.extend(_schema_nodes(child))
        properties = node.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                found.extend(_schema_nodes(child))
        items = node.get("items")
        if isinstance(items, dict):
            found.extend(_schema_nodes(items))
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            found.extend(_schema_nodes(any_of))
    return found


def test_normalization_is_deep_copied_required_nullable_and_ref_safe() -> None:
    adapter = _load_adapter()
    raw = raw_provider_tools()
    snapshot = copy.deepcopy(raw)

    adapted = adapter.adapt_provider_tools(raw)
    by_name = _by_name(adapted)

    assert raw == snapshot
    assert set(by_name) == set(adapter.TARGET_TOOL_NAMES)
    assert all(tool["function"]["strict"] is True for tool in adapted)
    for tool in adapted:
        parameters = tool["function"]["parameters"]
        assert adapter.strict_parameter_schema_issues(parameters) == []
        metrics = adapter.strict_schema_metrics(parameters)
        assert metrics["object_property_count"] <= adapter.MAX_OBJECT_PROPERTIES
        assert metrics["max_object_depth"] <= adapter.MAX_OBJECT_DEPTH
        assert metrics["schema_string_length"] <= adapter.MAX_SCHEMA_STRING_LENGTH
        assert metrics["enum_value_count"] <= adapter.MAX_ENUM_VALUES
        assert all(
            node.get("additionalProperties") is False
            and set(node.get("required") or []) == set((node.get("properties") or {}).keys())
            for node in _object_nodes(parameters)
        )

    context = by_name["mcp_factory__cf_context_get"]["function"]["parameters"]
    context_version = context["properties"]["context_version"]
    assert "context_version" in context["required"]
    assert "default" not in context_version
    assert {variant.get("type") for variant in context_version["anyOf"]} == {
        "string",
        "null",
    }

    raw_draft = snapshot[1]["function"]["parameters"]["properties"]["draft"]
    strict_draft = by_name["mcp_factory__cf_draft_save"]["function"]["parameters"]["properties"][
        "draft"
    ]
    assert "oneOf" in raw_draft and "discriminator" in raw_draft
    assert "oneOf" not in strict_draft and "discriminator" not in strict_draft
    assert [row["$ref"] for row in strict_draft["anyOf"]] == [
        row["$ref"] for row in raw_draft["oneOf"]
    ]
    parameters = by_name["mcp_factory__cf_draft_save"]["function"]["parameters"]
    channel = parameters["$defs"]["ClaimEvidence"]["properties"]["channel"]
    assert "$ref" not in channel
    assert channel["anyOf"] == [{"$ref": "#/$defs/Channel"}]
    assert "description" in channel
    assert all(set(node) == {"$ref"} for node in _schema_nodes(parameters) if "$ref" in node)


def test_adapter_changes_only_targets_and_fails_closed_on_invalid_inventory() -> None:
    adapter = _load_adapter()
    raw = raw_provider_tools()
    non_target = {
        "type": "function",
        "function": {
            "name": "unchanged_builtin",
            "description": "unchanged",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    }
    combined = [non_target, *raw]
    adapted = adapter.adapt_provider_tools(combined)

    assert adapter.canonical_json(adapted[0]) == adapter.canonical_json(non_target)
    with pytest.raises(adapter.StrictToolAdapterError, match="incomplete"):
        adapter.adapt_provider_tools(raw[:1])

    incompatible = copy.deepcopy(raw)
    incompatible[0]["function"]["parameters"]["properties"]["campaign_id"]["not"] = {"type": "null"}
    with pytest.raises(adapter.StrictToolAdapterError, match="UNSUPPORTED_KEYWORD"):
        adapter.adapt_provider_tools(incompatible)

    invalid_ref_sibling = copy.deepcopy(raw)
    invalid_ref_sibling[0]["function"]["parameters"]["properties"]["operation"]["minLength"] = 1
    with pytest.raises(adapter.StrictToolAdapterError, match="unsupported sibling"):
        adapter.adapt_provider_tools(invalid_ref_sibling)

    unconstrained = copy.deepcopy(raw)
    unconstrained[0]["function"]["parameters"]["properties"]["campaign_id"] = {}
    with pytest.raises(adapter.StrictToolAdapterError, match="UNCONSTRAINED_VALUE"):
        adapter.adapt_provider_tools(unconstrained)


def test_adapter_closes_object_schemas_removed_by_mcp_transport() -> None:
    adapter = _load_adapter()
    transported = raw_provider_tools()
    for tool in transported:
        tool["function"]["parameters"].pop("additionalProperties")
    snapshot = copy.deepcopy(transported)

    adapted = adapter.adapt_provider_tools(transported)

    assert transported == snapshot
    assert all(tool["function"]["parameters"]["additionalProperties"] is False for tool in adapted)


def test_adapter_rejects_official_subset_size_and_depth_limit_breaches() -> None:
    adapter = _load_adapter()
    too_deep: dict[str, Any] = {"type": "string"}
    for index in range(adapter.MAX_OBJECT_DEPTH + 1):
        too_deep = {
            "type": "object",
            "properties": {f"level_{index}": too_deep},
        }
    with pytest.raises(adapter.StrictToolAdapterError, match="DEPTH_LIMIT_EXCEEDED"):
        adapter.normalize_parameter_schema("deep_fixture", too_deep)

    too_many_enum_values = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "enum": [str(index) for index in range(adapter.MAX_ENUM_VALUES + 1)],
            }
        },
    }
    with pytest.raises(adapter.StrictToolAdapterError, match="ENUM_LIMIT_EXCEEDED"):
        adapter.normalize_parameter_schema("enum_fixture", too_many_enum_values)


def test_registry_installation_strictifies_full_discovery_before_returning() -> None:
    adapter = _load_adapter()
    raw = raw_provider_tools()

    class FakeRegistry:
        def __init__(self, tools: list[dict[str, Any]]) -> None:
            self.tools = tools

        def schemas(self, core_only: bool = False) -> list[dict[str, Any]]:
            del core_only
            return copy.deepcopy(self.tools)

        def get_schema_by_name(self, name: str) -> dict[str, Any] | None:
            return next(
                (copy.deepcopy(tool) for tool in self.tools if tool["function"]["name"] == name),
                None,
            )

    metadata = adapter.install_strict_tool_adapter(FakeRegistry)
    registry = FakeRegistry(raw)

    assert metadata["decision_id"] == "CF-RP-001"
    assert all(tool["function"]["strict"] is True for tool in registry.schemas())
    assert registry.get_schema_by_name("mcp_factory__cf_context_get")["function"]["strict"] is True
    assert adapter.install_strict_tool_adapter(FakeRegistry) == metadata

    registry.tools = raw[:1]
    with pytest.raises(adapter.StrictToolAdapterError, match="incomplete"):
        registry.schemas()
