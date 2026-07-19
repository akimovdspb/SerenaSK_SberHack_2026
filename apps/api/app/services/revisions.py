from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from apps.api.app.domain.campaigns import ContentPlan, ContextBundle
from apps.api.app.domain.learning import (
    FeedbackScope,
    FeedbackView,
    PackageDiffChange,
)
from apps.api.app.domain.models import CommunicationBundle, CommunicationPatch, Operation
from apps.api.app.domain.quality import QualityReport
from apps.api.app.services.briefs import hash_value
from apps.api.app.services.quality import evaluate_bundle


class RevisionError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class RevisionMergeResult:
    bundle: CommunicationBundle
    report: QualityReport
    changed_paths: tuple[str, ...]
    changes: tuple[PackageDiffChange, ...]


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _leaf_values(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        if not value:
            return {path: {}}
        result: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}/{_escape_pointer(str(key))}"
            result.update(_leaf_values(child, child_path))
        return result
    if isinstance(value, list):
        if not value:
            return {path: []}
        result = {}
        for index, child in enumerate(value):
            result.update(_leaf_values(child, f"{path}/{index}"))
        return result
    return {path: value}


def _editable_paths(bundle: CommunicationBundle) -> tuple[str, ...]:
    paths: list[str] = []
    if bundle.sms is not None:
        paths.append("/sms/text")
    if bundle.email is not None:
        paths.extend(
            (
                "/email/subject",
                "/email/preheader",
                "/email/headline",
                "/email/plain_text",
            )
        )
        for index, _ in enumerate(bundle.email.sections):
            paths.extend(
                (
                    f"/email/sections/{index}/heading",
                    f"/email/sections/{index}/body",
                )
            )
    return tuple(paths)


def _allowed_paths(bundle: CommunicationBundle, feedback: FeedbackView) -> tuple[str, ...]:
    editable = _editable_paths(bundle)
    if feedback.artifact_path not in editable:
        raise RevisionError(
            "REVISION_SCOPE_VIOLATION",
            "feedback path is not an editable structured-copy field",
        )
    if feedback.scope is FeedbackScope.CURRENT_FIELD:
        return (feedback.artifact_path,)
    if feedback.scope is FeedbackScope.CURRENT_CHANNEL:
        channel = "/sms/" if feedback.artifact_path.startswith("/sms/") else "/email/"
        return tuple(path for path in editable if path.startswith(channel))
    return editable


def _requested_optional_concepts(
    context: ContextBundle,
    feedback: FeedbackView,
) -> tuple[str, ...]:
    comment = feedback.comment.casefold()
    available = set(context.content_plan.available_optional_concept_ids)
    selected: list[str] = []
    for concept in context.concepts:
        if concept.concept_id not in available:
            continue
        if concept.concept_id.casefold() in comment or any(
            surface.casefold() in comment for surface in concept.accepted_surface_forms
        ):
            selected.append(concept.concept_id)
    return tuple(sorted(selected))


def build_revision_context(
    *,
    base_context: ContextBundle,
    base_bundle: CommunicationBundle,
    feedback: FeedbackView,
) -> ContextBundle:
    if base_context.brief_snapshot.campaign_id != feedback.campaign_id:
        raise RevisionError("REVISION_SCOPE_VIOLATION", "feedback belongs to another campaign")
    if hash_value(base_bundle.model_dump(mode="json")) != feedback.package_hash:
        raise RevisionError("STALE_BASE_PACKAGE", "feedback base package hash is stale")
    allowed = _allowed_paths(base_bundle, feedback)
    leaves = _leaf_values(base_bundle.model_dump(mode="json"))
    protected = tuple(sorted(path for path in leaves if path not in set(allowed)))
    selected_concepts = tuple(base_context.content_plan.selected_concept_ids)
    requested_concepts = _requested_optional_concepts(base_context, feedback)
    next_selected_concepts = tuple(dict.fromkeys((*selected_concepts, *requested_concepts)))
    selection_sources = tuple(base_context.content_plan.selection_sources)
    if requested_concepts and "feedback" not in selection_sources:
        selection_sources = (*selection_sources, "feedback")
    plan = ContentPlan(
        selected_fact_ids=base_context.content_plan.selected_fact_ids,
        channel_selected_fact_ids=base_context.content_plan.channel_selected_fact_ids,
        selected_concept_ids=next_selected_concepts,
        available_optional_fact_ids=base_context.content_plan.available_optional_fact_ids,
        available_optional_concept_ids=base_context.content_plan.available_optional_concept_ids,
        selection_sources=selection_sources,
        applied_rule_version_ids=base_context.content_plan.applied_rule_version_ids,
    )
    payload = base_context.model_dump(mode="json")
    payload.update(
        {
            "context_version": "0" * 64,
            "operation": Operation.REVISION,
            "content_plan": plan.model_dump(mode="json"),
            "previous_package": base_bundle.model_dump(mode="json"),
            "feedback": feedback.model_dump(mode="json"),
            "allowed_changed_paths": list(allowed),
            "protected_paths": list(protected),
            "protected_hashes": {path: hash_value(leaves[path]) for path in protected},
            "output_schema_id": "communication_patch:1.0",
        }
    )
    payload["context_version"] = hash_value(
        {key: value for key, value in payload.items() if key != "context_version"}
    )
    return ContextBundle.model_validate(payload)


def _pointer_parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]


def _set_pointer(document: dict[str, Any], pointer: str, value: Any) -> None:
    parts = _pointer_parts(pointer)
    current: Any = document
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    final = parts[-1]
    if isinstance(current, list):
        current[int(final)] = value
    else:
        current[final] = value


def build_deterministic_patch(context: ContextBundle) -> CommunicationPatch:
    if context.operation is not Operation.REVISION or context.previous_package is None:
        raise RevisionError("REVISION_CONTEXT_INVALID", "revision context is required")
    if context.feedback is None:
        raise RevisionError("REVISION_CONTEXT_INVALID", "revision feedback is required")
    feedback_id = str(context.feedback.get("feedback_id") or "")
    previous = CommunicationBundle.model_validate(context.previous_package)
    concept_by_id = {item.concept_id: item for item in context.concepts}
    requested = [
        concept_by_id[concept_id]
        for concept_id in context.content_plan.selected_concept_ids
        if concept_id in concept_by_id
        and any(
            surface not in "\n".join(_string_leaves(previous))
            for surface in concept_by_id[concept_id].accepted_surface_forms
        )
    ]
    if not requested:
        raise RevisionError("REVISION_SCOPE_VIOLATION", "feedback selected no new allowed concept")
    allowed = set(context.allowed_changed_paths)
    preferred = str(context.feedback.get("artifact_path") or "")
    targets = [preferred]
    if preferred.startswith("/email/") and "/email/plain_text" in allowed:
        targets.append("/email/plain_text")
    targets = list(dict.fromkeys(path for path in targets if path in allowed))
    if not targets:
        raise RevisionError("REVISION_SCOPE_VIOLATION", "feedback has no editable target")
    candidate = previous.model_dump(mode="json")
    surface = requested[0].accepted_surface_forms[0]
    sentence = f"Следующий шаг — {surface}."
    changed: list[str] = []
    leaves = _leaf_values(candidate)
    for target in targets:
        current = leaves.get(target)
        if not isinstance(current, str):
            raise RevisionError("REVISION_SCOPE_VIOLATION", "feedback target is not textual")
        if surface in current:
            continue
        _set_pointer(candidate, target, f"{current.rstrip()} {sentence}".strip())
        changed.append(target)
    if not changed:
        raise RevisionError("REVISION_SCOPE_VIOLATION", "revision would be empty")
    merged = CommunicationBundle.model_validate(candidate)
    return CommunicationPatch(
        base_package_hash=hash_value(previous.model_dump(mode="json")),
        feedback_id=feedback_id,
        changed_paths=changed,
        sms=merged.sms if any(path.startswith("/sms/") for path in changed) else None,
        email=merged.email if any(path.startswith("/email/") for path in changed) else None,
        claim_evidence=merged.claim_evidence,
        warnings=merged.warnings,
    )


def _string_leaves(bundle: CommunicationBundle) -> list[str]:
    return [
        value
        for value in _leaf_values(bundle.model_dump(mode="json")).values()
        if isinstance(value, str)
    ]


def _preview(value: Any) -> str:
    if isinstance(value, str):
        return value[:300]
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:300]


def merge_communication_patch(
    *,
    context: ContextBundle,
    patch: CommunicationPatch,
    current_package_hash: str,
) -> RevisionMergeResult:
    if context.operation is not Operation.REVISION or context.previous_package is None:
        raise RevisionError("REVISION_CONTEXT_INVALID", "revision context is required")
    if context.feedback is None:
        raise RevisionError("REVISION_CONTEXT_INVALID", "revision feedback is required")
    previous = CommunicationBundle.model_validate(context.previous_package)
    expected_base_hash = hash_value(previous.model_dump(mode="json"))
    if patch.base_package_hash != expected_base_hash or current_package_hash != expected_base_hash:
        raise RevisionError("STALE_BASE_PACKAGE", "revision base package is no longer current")
    if patch.feedback_id != context.feedback.get("feedback_id"):
        raise RevisionError("REVISION_SCOPE_VIOLATION", "patch feedback id does not match context")

    before_document = previous.model_dump(mode="json")
    after_document = copy.deepcopy(before_document)
    if patch.sms is not None:
        after_document["sms"] = patch.sms.model_dump(mode="json")
    if patch.email is not None:
        after_document["email"] = patch.email.model_dump(mode="json")
    after_document["claim_evidence"] = [
        item.model_dump(mode="json") for item in patch.claim_evidence
    ]
    after_document["warnings"] = list(patch.warnings)
    merged = CommunicationBundle.model_validate(after_document)

    before_leaves = _leaf_values(before_document)
    after_leaves = _leaf_values(merged.model_dump(mode="json"))
    actual_paths = tuple(
        sorted(
            path
            for path in set(before_leaves) | set(after_leaves)
            if before_leaves.get(path) != after_leaves.get(path)
        )
    )
    if len(patch.changed_paths) != len(set(patch.changed_paths)):
        raise RevisionError("REVISION_SCOPE_VIOLATION", "changed_paths contains duplicates")
    declared_paths = tuple(sorted(set(patch.changed_paths)))
    if not actual_paths or declared_paths != actual_paths:
        raise RevisionError(
            "REVISION_SCOPE_VIOLATION",
            "declared changed_paths differ from the actual leaf diff",
        )
    if not set(actual_paths).issubset(context.allowed_changed_paths):
        raise RevisionError(
            "REVISION_SCOPE_VIOLATION",
            "patch changes a path outside feedback scope",
        )
    for path in context.protected_paths:
        if path not in before_leaves or path not in after_leaves:
            raise RevisionError("REVISION_SCOPE_VIOLATION", "protected path shape changed")
        expected_hash = context.protected_hashes.get(path)
        if expected_hash != hash_value(before_leaves[path]) or expected_hash != hash_value(
            after_leaves[path]
        ):
            raise RevisionError("REVISION_SCOPE_VIOLATION", "protected path hash changed")

    report = evaluate_bundle(merged, context)
    changes = tuple(
        PackageDiffChange(
            path=path,
            before_hash=hash_value(before_leaves.get(path)),
            after_hash=hash_value(after_leaves.get(path)),
            before_preview=_preview(before_leaves.get(path)),
            after_preview=_preview(after_leaves.get(path)),
            protected=False,
        )
        for path in actual_paths
    )
    return RevisionMergeResult(
        bundle=merged,
        report=report,
        changed_paths=actual_paths,
        changes=changes,
    )
