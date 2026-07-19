from __future__ import annotations

from apps.api.app.ouroboros_client import OuroborosTaskAdapter
from apps.api.app.settings import Settings


def main() -> int:
    settings = Settings()
    adapter = OuroborosTaskAdapter(
        base_url=settings.OUROBOROS_BASE_URL,
        lock_path=settings.CONTRACT_LOCK_PATH,
        skill_path=settings.SKILL_PATH,
        expected_identity_kind=settings.RUNTIME_CONTRACT_IDENTITY_KIND,
        expected_runtime_identity=settings.RUNTIME_CONTRACT_IDENTITY,
    )
    try:
        adapter.admit()
    finally:
        adapter.close()
    print("railway-admission: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
