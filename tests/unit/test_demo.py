from __future__ import annotations

import pytest

from scripts.demo import DemoError, run_canary


def test_demo_canary_requires_separate_opt_in_and_rejects_host_provider_key() -> None:
    with pytest.raises(DemoError, match="ALLOW_DEMO_CANARY"):
        run_canary({})

    with pytest.raises(DemoError, match="must not receive"):
        run_canary(
            {
                "ALLOW_DEMO_CANARY": "true",
                "DEMO_CANARY_ID": "canary-fixture-new",
                "OPENAI_API_KEY": "not-a-real-key",
            }
        )
