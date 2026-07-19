from __future__ import annotations

import pathlib

import pytest

from scripts import release_identity


def test_release_identity_requires_expected_branch_and_clean_commit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): "a" * 40,
        ("branch", "--show-current"): "codex/p0-autonomous",
        ("status", "--porcelain=v1"): "",
    }
    monkeypatch.setattr(
        release_identity,
        "_git",
        lambda args, *, root: values[tuple(args)],
    )

    assert release_identity.frozen_git_identity(root=tmp_path) == (
        "a" * 40,
        "codex/p0-autonomous",
    )

    values[("status", "--porcelain=v1")] = " M scripts/example.py"
    with pytest.raises(release_identity.ReleaseIdentityError, match="clean frozen"):
        release_identity.frozen_git_identity(root=tmp_path)

    values[("status", "--porcelain=v1")] = ""
    values[("branch", "--show-current")] = "main"
    with pytest.raises(release_identity.ReleaseIdentityError, match="codex/p0-autonomous"):
        release_identity.frozen_git_identity(root=tmp_path)
