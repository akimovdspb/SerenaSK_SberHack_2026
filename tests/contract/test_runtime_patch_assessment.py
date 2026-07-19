from __future__ import annotations

import pytest

from scripts.runtime_patch_assessment import (
    assess_function_tool,
    current_assessment,
    current_provider_tools,
    raw_assessment,
)


@pytest.mark.contract
def test_approved_provider_projection_resolves_all_strict_mode_blockers() -> None:
    before = raw_assessment()
    issues = current_assessment()
    tools = current_provider_tools()
    before_keys = {(issue.tool_name, issue.path, issue.code) for issue in before}
    issue_keys = {(issue.tool_name, issue.path, issue.code) for issue in issues}

    assert len(before) == 10
    assert issues == []
    assert issue_keys == set()
    assert all(tool["function"]["strict"] is True for tool in tools)
    assert (
        sum(
            issue.tool_name == "mcp_factory__cf_draft_save"
            and issue.code == "PROPERTIES_NOT_REQUIRED"
            and issue.path.startswith("$/$defs/")
            for issue in before
        )
        == 4
    )
    assert (
        "mcp_factory__cf_context_get",
        "$/function",
        "FUNCTION_NOT_STRICT",
    ) in before_keys
    assert (
        "mcp_factory__cf_context_get",
        "$",
        "PROPERTIES_NOT_REQUIRED",
    ) in before_keys
    assert (
        "mcp_factory__cf_draft_save",
        "$/function",
        "FUNCTION_NOT_STRICT",
    ) in before_keys
    assert not any(
        issue.code == "UNCONSTRAINED_VALUE"
        and issue.path == "$/$defs/ClaimEvidence/properties/normalized_value"
        for issue in before
    )
    assert any(
        issue.tool_name == "mcp_factory__cf_draft_save"
        and issue.code == "UNSUPPORTED_KEYWORD"
        and issue.detail == "oneOf"
        for issue in before
    )
    assert any(
        issue.tool_name == "mcp_factory__cf_draft_save"
        and issue.code == "PROPERTIES_NOT_REQUIRED"
        and "email" in issue.detail
        and "sms" in issue.detail
        for issue in before
    )


@pytest.mark.contract
def test_strict_compatible_function_schema_has_no_assessment_issues() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "strict_fixture",
            "description": "fixture",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "required_text": {"type": "string"},
                    "nullable_text": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["required_text", "nullable_text"],
                "additionalProperties": False,
            },
        },
    }

    assert assess_function_tool(tool) == []
