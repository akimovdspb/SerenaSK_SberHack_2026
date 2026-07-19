from __future__ import annotations

from scripts.doctor import redacted_contract_summary


def test_doctor_contract_summary_is_redacted_and_reports_patch_blocker() -> None:
    summary = redacted_contract_summary(
        {
            "runtime": {"tag": "v6.61.4", "commit": "a" * 40},
            "skill": {"ready": True, "activation_mode": "adapter_injected"},
            "tools": {
                "post_deny_tool_names": [
                    "mcp_factory__cf_context_get",
                    "mcp_factory__cf_draft_save",
                ]
            },
            "secret": "must-not-project",
        }
    )

    assert summary["strict_provider_tools_ready"] is False
    assert summary["release_blocker"] == "CF-RP-001"
    assert "secret" not in summary
