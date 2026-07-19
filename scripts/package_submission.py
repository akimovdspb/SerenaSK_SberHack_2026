from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from typing import Any

from scripts.evaluation import review_packet_case_ids
from scripts.security_scan import scan_generated_artifacts

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPERATOR_ROOT = ROOT / "artifacts" / "operator"
SUBMISSION_ROOT = ROOT / "artifacts" / "submission"
VERIFY_REPORT = ROOT / "runtime" / "verification" / "submission" / "latest.json"
RUBRIC_KEYS = {
    "clarity",
    "natural_russian",
    "brief_fit",
    "personalization",
    "persuasion_without_pressure",
    "non_template_quality",
    "channel_consistency",
    "demo_readiness",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
OPERATOR_DOCUMENT_NAMES = (
    "qualitative-reviews.json",
    "approvals.json",
    "signoffs.json",
    "rehearsals.json",
    "video.json",
    "links.json",
)


class SubmissionError(RuntimeError):
    pass


def _load(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubmissionError(f"{label} is missing or malformed") from exc
    if not isinstance(value, dict):
        raise SubmissionError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise SubmissionError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SubmissionError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SubmissionError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _human_id(value: Any, label: str, *, allow_fixture: bool) -> str:
    identity = str(value or "")
    if not IDENTIFIER_RE.fullmatch(identity):
        raise SubmissionError(f"{label} identity is invalid")
    forbidden = ("test", "fixture", "codex", "agent", "ai_")
    if not allow_fixture and identity.casefold().startswith(forbidden):
        raise SubmissionError(f"{label} must be a real human identity")
    return identity


def _https_url(value: Any, label: str, *, allow_fixture: bool) -> str:
    url = str(value or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise SubmissionError(f"{label} must be an HTTPS URL without embedded credentials")
    if (
        not allow_fixture
        and parsed.hostname
        and parsed.hostname.endswith((".test", ".invalid", ".local"))
    ):
        raise SubmissionError(f"{label} is not externally reachable")
    return url


def _fixture_documents() -> dict[str, dict[str, Any]]:
    now = "2026-07-11T00:00:00+00:00"
    evaluation_id = "fixture-evaluation"
    reviews = []
    for ordinal, case_id in enumerate(review_packet_case_ids(), start=1):
        reviews.append(
            {
                "case_id": case_id,
                "reviewer_id": f"fixture_reviewer_{ordinal}",
                "reviewer_role": "human",
                "completed_at": now,
                "scores": {key: 4 for key in sorted(RUBRIC_KEYS)},
                "comments": "Dry-run fixture only; not a human submission record.",
                "packet_sha256": f"{ordinal:x}".zfill(64),
            }
        )
    return {
        "qualitative-reviews.json": {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "status": "COMPLETE",
            "test_fixture": True,
            "reviews": reviews,
        },
        "approvals.json": {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "test_fixture": True,
            "rule": {
                "actor_id": "fixture_rule_owner",
                "actor_role": "human",
                "test_only": False,
                "decision": "APPROVED",
                "rule_version_id": "rulev_fixture",
                "artifact_hash": "a" * 64,
                "approved_at": now,
            },
            "package": {
                "actor_id": "fixture_package_owner",
                "actor_role": "human",
                "test_only": False,
                "decision": "APPROVED",
                "package_id": "package_fixture",
                "artifact_hash": "b" * 64,
                "approved_at": now,
            },
        },
        "signoffs.json": {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "test_fixture": True,
            "signoffs": [
                {
                    "signer_id": "fixture_signer_one",
                    "accepted": True,
                    "signed_at": now,
                    "comment": "Dry-run fixture one.",
                },
                {
                    "signer_id": "fixture_signer_two",
                    "accepted": True,
                    "signed_at": now,
                    "comment": "Dry-run fixture two.",
                },
            ],
        },
        "rehearsals.json": {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "test_fixture": True,
            "rehearsals": [
                {
                    "operator_id": "fixture_rehearsal_one",
                    "started_at": now,
                    "duration_seconds": 166,
                    "notes": "Dry-run fixture one.",
                },
                {
                    "operator_id": "fixture_rehearsal_two",
                    "started_at": now,
                    "duration_seconds": 169,
                    "notes": "Dry-run fixture two.",
                },
            ],
        },
        "video.json": {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "test_fixture": True,
            "kind": "link",
            "video_ref": "https://fixture.example/video",
            "duration_seconds": 168,
            "audio_track": True,
            "width": 1920,
            "height": 1080,
            "readability_checked": True,
            "verified_at": now,
        },
        "links.json": {
            "schema_version": 1,
            "evaluation_id": evaluation_id,
            "test_fixture": True,
            "repository_url": "https://fixture.example/repository",
            "demo_url": "https://fixture.example/video",
        },
    }


def validate_operator_documents(
    documents: dict[str, dict[str, Any]],
    *,
    expected_evaluation_id: str,
    allow_fixture: bool = False,
) -> dict[str, Any]:
    required = set(OPERATOR_DOCUMENT_NAMES)
    if set(documents) != required:
        raise SubmissionError("operator artifact inventory is incomplete")
    for name, document in documents.items():
        if document.get("schema_version") != 1:
            raise SubmissionError(f"{name} schema version is invalid")
        if document.get("evaluation_id") != expected_evaluation_id:
            raise SubmissionError(f"{name} evaluation identity does not match")
        is_fixture = document.get("test_fixture") is True
        if allow_fixture != is_fixture:
            raise SubmissionError(f"{name} fixture status is not allowed in this gate")

    qualitative = documents["qualitative-reviews.json"]
    reviews = qualitative.get("reviews")
    if qualitative.get("status") != "COMPLETE" or not isinstance(reviews, list):
        raise SubmissionError("qualitative reviews are incomplete")
    if [row.get("case_id") for row in reviews if isinstance(row, dict)] != list(
        review_packet_case_ids()
    ):
        raise SubmissionError("qualitative reviews do not cover the six preselected packets")
    reviewer_ids: set[str] = set()
    for raw in reviews:
        if not isinstance(raw, dict):
            raise SubmissionError("qualitative review row is malformed")
        reviewer_ids.add(_human_id(raw.get("reviewer_id"), "reviewer", allow_fixture=allow_fixture))
        if raw.get("reviewer_role") != "human":
            raise SubmissionError("qualitative reviewer role must be human")
        _timestamp(raw.get("completed_at"), "qualitative review")
        scores = raw.get("scores")
        if (
            not isinstance(scores, dict)
            or set(scores) != RUBRIC_KEYS
            or any(
                not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5
                for value in scores.values()
            )
        ):
            raise SubmissionError("qualitative review scores are incomplete")
        if not str(raw.get("comments") or "").strip():
            raise SubmissionError("qualitative review comments are required")
        if not SHA256_RE.fullmatch(str(raw.get("packet_sha256") or "")):
            raise SubmissionError("qualitative packet hash is invalid")

    approvals = documents["approvals.json"]
    approval_actors: set[str] = set()
    for label, identity_field in (("rule", "rule_version_id"), ("package", "package_id")):
        raw = approvals.get(label)
        if not isinstance(raw, dict):
            raise SubmissionError(f"{label} approval is missing")
        approval_actors.add(
            _human_id(raw.get("actor_id"), f"{label} approver", allow_fixture=allow_fixture)
        )
        if (
            raw.get("actor_role") != "human"
            or raw.get("test_only") is not False
            or raw.get("decision") != "APPROVED"
            or not IDENTIFIER_RE.fullmatch(str(raw.get(identity_field) or ""))
            or not SHA256_RE.fullmatch(str(raw.get("artifact_hash") or ""))
        ):
            raise SubmissionError(f"{label} approval is not a real exact-artifact approval")
        _timestamp(raw.get("approved_at"), f"{label} approval")

    signoffs = documents["signoffs.json"].get("signoffs")
    if not isinstance(signoffs, list) or len(signoffs) != 2:
        raise SubmissionError("exactly two independent sign-offs are required")
    signer_ids: set[str] = set()
    for raw in signoffs:
        if not isinstance(raw, dict) or raw.get("accepted") is not True:
            raise SubmissionError("sign-off acceptance is incomplete")
        signer_ids.add(_human_id(raw.get("signer_id"), "signer", allow_fixture=allow_fixture))
        _timestamp(raw.get("signed_at"), "sign-off")
        if not str(raw.get("comment") or "").strip():
            raise SubmissionError("sign-off comment is required")
    if len(signer_ids) != 2:
        raise SubmissionError("sign-offs must have two distinct human identities")

    rehearsals = documents["rehearsals.json"].get("rehearsals")
    if not isinstance(rehearsals, list) or len(rehearsals) < 2:
        raise SubmissionError("at least two timed rehearsals are required")
    for raw in rehearsals:
        if not isinstance(raw, dict):
            raise SubmissionError("rehearsal row is malformed")
        _human_id(raw.get("operator_id"), "rehearsal operator", allow_fixture=allow_fixture)
        _timestamp(raw.get("started_at"), "rehearsal")
        duration = raw.get("duration_seconds")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not 165 <= duration <= 170
        ):
            raise SubmissionError("rehearsal duration must be 165-170 seconds")
        if not str(raw.get("notes") or "").strip():
            raise SubmissionError("rehearsal notes are required")

    video = documents["video.json"]
    duration = video.get("duration_seconds")
    if (
        video.get("kind") not in {"file", "link"}
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not 0 < float(duration) < 180
        or video.get("audio_track") is not True
        or int(video.get("width") or 0) < 1920
        or int(video.get("height") or 0) < 1080
        or video.get("readability_checked") is not True
    ):
        raise SubmissionError("video metadata does not satisfy the voiced <180s 1080p gate")
    _timestamp(video.get("verified_at"), "video")

    links = documents["links.json"]
    repository_url = _https_url(
        links.get("repository_url"), "repository URL", allow_fixture=allow_fixture
    )
    demo_url = _https_url(links.get("demo_url"), "demo URL", allow_fixture=allow_fixture)
    if video.get("kind") == "link" and video.get("video_ref") != demo_url:
        raise SubmissionError("video link and submission demo URL differ")
    if video.get("kind") == "file" and not IDENTIFIER_RE.fullmatch(
        pathlib.Path(str(video.get("video_ref") or "")).stem
    ):
        raise SubmissionError("video filename is invalid")
    return {
        "status": "PASS",
        "evaluation_id": expected_evaluation_id,
        "review_count": len(reviews),
        "reviewer_count": len(reviewer_ids),
        "approval_actor_count": len(approval_actors),
        "signoff_count": len(signer_ids),
        "rehearsal_count": len(rehearsals),
        "video_duration_seconds": float(duration),
        "repository_url": repository_url,
        "demo_url": demo_url,
        "test_fixture": allow_fixture,
    }


def validate_operator_evidence_bindings(
    documents: dict[str, dict[str, Any]],
    evidence_root: pathlib.Path,
    *,
    expected_evaluation_id: str,
) -> dict[str, Any]:
    from scripts.evidence import validate_evidence_directory

    manifest = validate_evidence_directory(evidence_root)
    if manifest.get("evaluation_id") != expected_evaluation_id:
        raise SubmissionError("operator records and implementation evidence identity differ")
    qualitative = _load(evidence_root / "qualitative-review.json", "evidence review index")
    raw_packets = qualitative.get("packets")
    if not isinstance(raw_packets, list):
        raise SubmissionError("evidence review packet index is malformed")
    packets = {str(row.get("case_id")): row for row in raw_packets if isinstance(row, dict)}
    reviews = documents["qualitative-reviews.json"].get("reviews")
    if not isinstance(reviews, list):
        raise SubmissionError("qualitative reviews are incomplete")
    for review in reviews:
        if not isinstance(review, dict):
            raise SubmissionError("qualitative review row is malformed")
        packet = packets.get(str(review.get("case_id") or ""))
        if packet is None or review.get("packet_sha256") != packet.get("packet_sha256"):
            raise SubmissionError("qualitative review is not bound to its frozen packet")

    raw_targets = manifest.get("submission_approval_targets")
    if not isinstance(raw_targets, dict):
        raise SubmissionError("evidence approval targets are missing")
    approvals = documents["approvals.json"]
    for label, identity_field in (("rule", "rule_version_id"), ("package", "package_id")):
        target = raw_targets.get(label)
        approval = approvals.get(label)
        if (
            not isinstance(target, dict)
            or not isinstance(approval, dict)
            or approval.get(identity_field) != target.get(identity_field)
            or approval.get("artifact_hash") != target.get("artifact_hash")
        ):
            raise SubmissionError(f"{label} approval is not bound to frozen evidence")
    return {
        "status": "PASS",
        "evaluation_id": expected_evaluation_id,
        "packet_binding_count": len(reviews),
        "approval_binding_count": 2,
    }


def validate_operator_artifacts(
    root: pathlib.Path,
    *,
    expected_evaluation_id: str,
    evidence_root: pathlib.Path,
) -> dict[str, Any]:
    documents = {name: _load(root / name, name) for name in OPERATOR_DOCUMENT_NAMES}
    result = validate_operator_documents(
        documents,
        expected_evaluation_id=expected_evaluation_id,
        allow_fixture=False,
    )
    binding = validate_operator_evidence_bindings(
        documents,
        evidence_root,
        expected_evaluation_id=expected_evaluation_id,
    )
    video = documents["video.json"]
    for label, url in (
        ("repository", str(result["repository_url"])),
        ("demo", str(result["demo_url"])),
    ):
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "cf-release-check/1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status >= 400:
                    raise SubmissionError(f"{label} link is unavailable")
        except (OSError, urllib.error.URLError) as exc:
            raise SubmissionError(f"{label} link is unavailable") from exc
    if video.get("kind") == "file":
        candidate = root / pathlib.PurePath(str(video["video_ref"]))
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise SubmissionError("video path escapes operator artifact root") from exc
        if not candidate.is_file() or candidate.stat().st_size < 1024:
            raise SubmissionError("local demo video is missing or empty")
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of",
                "json",
                str(candidate),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        try:
            metadata = json.loads(probe.stdout)
        except json.JSONDecodeError as exc:
            raise SubmissionError("local demo video probe is malformed") from exc
        streams = metadata.get("streams") if isinstance(metadata, dict) else None
        raw_format = metadata.get("format") if isinstance(metadata, dict) else None
        if (
            probe.returncode != 0
            or not isinstance(streams, list)
            or not isinstance(raw_format, dict)
        ):
            raise SubmissionError("local demo video could not be inspected")
        try:
            measured_duration = float(str(raw_format.get("duration") or ""))
        except (TypeError, ValueError) as exc:
            raise SubmissionError("local demo video duration is unavailable") from exc
        video_streams = [row for row in streams if row.get("codec_type") == "video"]
        if (
            not video_streams
            or not any(row.get("codec_type") == "audio" for row in streams)
            or max(int(row.get("width") or 0) for row in video_streams) < 1920
            or max(int(row.get("height") or 0) for row in video_streams) < 1080
            or not 0 < measured_duration < 180
            or abs(measured_duration - float(result["video_duration_seconds"])) > 1.0
        ):
            raise SubmissionError("local demo video probe differs from approved metadata")
    return {**result, **binding}


def validate_pipeline_readiness() -> dict[str, Any]:
    documents = _fixture_documents()
    result = validate_operator_documents(
        documents,
        expected_evaluation_id="fixture-evaluation",
        allow_fixture=True,
    )
    return {
        "status": "PASS",
        "schema_version": 1,
        "dry_run_fixture": True,
        "review_count": result["review_count"],
        "signoff_count": result["signoff_count"],
        "rehearsal_count": result["rehearsal_count"],
        "real_package_requires_verify_submission": True,
    }


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zip_tree(source: pathlib.Path, destination: pathlib.Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, (2026, 7, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def build_submission(submission_id: str) -> pathlib.Path:
    if not IDENTIFIER_RE.fullmatch(submission_id):
        raise SubmissionError("SUBMISSION_ID is invalid")
    verification = _load(VERIFY_REPORT, "submission verification")
    if verification.get("status") != "PASS" or verification.get("submission_id") != submission_id:
        raise SubmissionError("green matching verify-submission report is required")
    evaluation_id = str(verification.get("evaluation_id") or "")
    implementation = _load(
        ROOT / "runtime" / "verification" / "implementation" / "latest.json",
        "implementation verification",
    )
    evidence_root = ROOT / str(implementation.get("evidence_path") or "")
    from scripts.evidence import validate_evidence_directory

    manifest = validate_evidence_directory(evidence_root)
    if manifest.get("evaluation_id") != evaluation_id:
        raise SubmissionError("implementation evidence identity differs from submission")
    operator_root = OPERATOR_ROOT / submission_id
    operator = validate_operator_artifacts(
        operator_root,
        expected_evaluation_id=evaluation_id,
        evidence_root=evidence_root,
    )
    destination = SUBMISSION_ROOT / submission_id
    if destination.exists():
        raise SubmissionError("submission directory already exists")
    SUBMISSION_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".submission-", dir=SUBMISSION_ROOT))
    try:
        shutil.copy2(evidence_root / "report.pdf", temporary / "communication_factory_results.pdf")
        shutil.copy2(evidence_root / "report.jpg", temporary / "communication_factory_results.jpg")
        shutil.copy2(
            evidence_root / "report.html", temporary / "communication_factory_results.html"
        )
        _zip_tree(evidence_root, temporary / "evidence.zip")
        video = _load(operator_root / "video.json", "video metadata")
        if video["kind"] == "file":
            shutil.copy2(operator_root / str(video["video_ref"]), temporary / "demo.mp4")
        else:
            (temporary / "demo_link.txt").write_text(operator["demo_url"] + "\n", encoding="utf-8")
        (temporary / "repository_link.txt").write_text(
            operator["repository_url"] + "\n", encoding="utf-8"
        )
        commit = str(implementation.get("git_commit") or "")
        (temporary / "source_commit.txt").write_text(commit + "\n", encoding="utf-8")
        records_root = temporary / "operator-records"
        records_root.mkdir()
        for name in OPERATOR_DOCUMENT_NAMES:
            shutil.copy2(operator_root / name, records_root / name)
        (temporary / "README_FIRST.md").write_text(
            "# Communication Factory submission\n\n"
            "Synthetic-only/no-send prototype: pinned Ouroboros creates typed SMS/e-mail through "
            "two private MCP tools; deterministic QA and humans control approval/export.\n\n"
            f"Repository: {operator['repository_url']}\n\n"
            f"Demo: {operator['demo_url']}\n\n"
            f"Commit: `{commit}`\n\n"
            "Startup: clone repository; `make init`; `make up`; open "
            "`http://127.0.0.1:8080` and select B01. Credentials are delivered separately.\n\n"
            "Reports are the `communication_factory_results.*` files; full immutable evidence is "
            "`evidence.zip`. P1 channels are not implemented. Nothing is sent externally.\n",
            encoding="utf-8",
        )
        package_manifest = {
            "schema_version": 1,
            "status": "SUBMISSION_READY",
            "submission_id": submission_id,
            "evaluation_id": evaluation_id,
            "source_commit": commit,
            "created_at": datetime.now(UTC).isoformat(),
            "synthetic": True,
            "no_send": True,
            "p1_implemented": False,
            "human_records": operator,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(package_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        findings = scan_generated_artifacts([temporary])
        if findings:
            raise SubmissionError("submission artifact security scan failed")
        files = sorted(
            item
            for item in temporary.rglob("*")
            if item.is_file() and item.name != "checksums.sha256"
        )
        (temporary / "checksums.sha256").write_text(
            "".join(
                f"{_sha256(path)}  {path.relative_to(temporary).as_posix()}\n" for path in files
            ),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            report = validate_pipeline_readiness()
            print(
                "package-submission: DRY-RUN PASS "
                f"reviews={report['review_count']} signoffs={report['signoff_count']}"
            )
            return 0
        submission_id = str(os.environ.get("SUBMISSION_ID") or "").strip()
        if not submission_id:
            raise SubmissionError("SUBMISSION_ID is required after real human gates")
        destination = build_submission(submission_id)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"package-submission: FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"package-submission: PASS path={destination.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
