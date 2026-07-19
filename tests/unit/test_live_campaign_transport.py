from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from apps.api.app import live_campaign_transport
from apps.api.app.domain.campaigns import ContextBundle
from apps.api.app.domain.workflow import PackageView


def _objects(
    *,
    url: str,
    normalized_value: object,
    encoding: str = "UCS-2",
    sms_text: str = "Пульс 🚀",
) -> tuple[ContextBundle, PackageView]:
    context = SimpleNamespace(brief_snapshot=SimpleNamespace(cta_url=url))
    evidence = SimpleNamespace(
        normalized_value=normalized_value,
        claim_type=SimpleNamespace(value="url"),
    )
    metrics = SimpleNamespace(encoding=encoding, code_units=8, characters=7, segments=1)
    package = SimpleNamespace(
        bundle=SimpleNamespace(
            claim_evidence=[evidence],
            sms=SimpleNamespace(text=sms_text),
        ),
        quality_report=SimpleNamespace(sms_metrics=metrics),
    )
    return cast(ContextBundle, context), cast(PackageView, package)


def test_representative_pilot_checks_are_case_specific_and_fail_closed() -> None:
    context, package = _objects(
        url="https://term.example.test/",
        normalized_value={"value": 14, "unit": "day"},
    )
    assert live_campaign_transport._case_specific_checks(
        "B04", context=context, package=package
    ) == {"duration_evidence_present": True}

    url = "https://pulse.example.test/go?utm_source=cf&utm_medium=email&utm_campaign=b07"
    context, package = _objects(url=url, normalized_value=url)
    assert all(
        live_campaign_transport._case_specific_checks(
            "B07", context=context, package=package
        ).values()
    )
    assert all(
        live_campaign_transport._case_specific_checks(
            "B08", context=context, package=package
        ).values()
    )

    context, package = _objects(url=url, normalized_value="https://wrong.example.test/")
    assert (
        live_campaign_transport._case_specific_checks("B07", context=context, package=package)[
            "allowed_url_evidence_exact"
        ]
        is False
    )
    assert (
        live_campaign_transport._case_specific_checks(
            "B01",
            context=context,
            package=package,
        )["injection_ignored"]
        is True
    )
    with pytest.raises(ValueError, match="unsupported"):
        live_campaign_transport._case_specific_checks(
            cast(Any, "B03"), context=context, package=package
        )
