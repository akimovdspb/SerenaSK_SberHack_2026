from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


class ReleaseIdentityError(RuntimeError):
    pass


def _git(args: list[str], *, root: pathlib.Path = ROOT) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise ReleaseIdentityError("Git release identity check failed")
    return process.stdout.strip()


def frozen_git_identity(
    *,
    root: pathlib.Path = ROOT,
    required_branch: str = "codex/p0-autonomous",
) -> tuple[str, str]:
    commit = _git(["rev-parse", "HEAD"], root=root)
    branch = _git(["branch", "--show-current"], root=root)
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseIdentityError("Git release commit identity is invalid")
    if branch != required_branch:
        raise ReleaseIdentityError(f"release operation requires {required_branch}")
    if _git(["status", "--porcelain=v1"], root=root):
        raise ReleaseIdentityError("release operation requires a clean frozen commit")
    return commit, branch
