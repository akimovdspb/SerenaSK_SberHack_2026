# ruff: noqa: E501, RUF001
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

EVALUATION_ID = "p0-glm-continuation-basket-20260715-03"
EXECUTED_APP_COMMIT = "0bdebf68f3e03039b425d3dae0483f26e9d143b1"
CODE_BASE_COMMIT = "6010d93300ddbfa87c19edf6e6d688e19198a0ff"
EXPECTED_CASES = tuple(f"B{index:02d}" for index in range(1, 16))
EXPECTED_FAILURES = ("B05", "B08")
FORBIDDEN_PATTERNS = (
    re.compile(r"\b(?:campaign|task|run|package|project)_[0-9a-f]{24,}\b"),
    re.compile(r"\bgen-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bcf_provider_[0-9a-f]{16,}\b"),
    re.compile(r"\b(?:sk|ghp|github_pat)-?[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{12,}\b", re.IGNORECASE),
)


class ExportError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ExportError(f"expected JSON object: {path}")
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_source(source: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    report_path = source / "report.json"
    checksums_path = source / "checksums.sha256"
    failed_path = source / "FAILED.json"
    report = _read_json(report_path)
    attempt = _read_json(source / "attempt.json")
    failed = _read_json(failed_path)

    if report.get("evaluation_id") != EVALUATION_ID:
        raise ExportError("unexpected evaluation_id")
    if report.get("app_commit") != EXECUTED_APP_COMMIT:
        raise ExportError("unexpected executed app commit")
    if report.get("status") != "FAIL" or report.get("release_targets_passed") is not False:
        raise ExportError("Basket-03 must remain failed/non-evidence")
    if report.get("business_case_count") != 15 or report.get("passed_case_count") != 13:
        raise ExportError("expected the honest 13/15 Basket-03 result")
    if sorted(report.get("release_blockers") or []) != ["B05_FAILED", "B08_FAILED"]:
        raise ExportError("unexpected Basket-03 release blockers")
    if attempt.get("provider") != "openrouter" or attempt.get("model") != "z-ai/glm-5.2":
        raise ExportError("unexpected provider/model route")
    if failed.get("status") != "FAILED":
        raise ExportError("missing failed-run marker")
    if _sha256(report_path) != failed.get("report_sha256"):
        raise ExportError("source report checksum does not match FAILED.json")
    if _sha256(checksums_path) != failed.get("checksums_sha256"):
        raise ExportError("source checksum manifest does not match FAILED.json")

    for line in checksums_path.read_text(encoding="ascii").splitlines():
        expected, relative = line.split("  ", 1)
        candidate = source / relative
        if not candidate.is_file() or _sha256(candidate) != expected:
            raise ExportError(f"source checksum mismatch: {relative}")
    return report, attempt, failed


def _select_email(bundle: dict[str, Any]) -> dict[str, Any] | None:
    email = bundle.get("email")
    if not isinstance(email, dict):
        return None
    return {
        "subject": email.get("subject"),
        "preheader": email.get("preheader"),
        "headline": email.get("headline"),
        "sections": email.get("sections") or [],
        "plain_text": email.get("plain_text"),
        "cta_label": email.get("cta_label"),
        "cta_url": email.get("cta_url"),
        "disclaimer_ids": email.get("disclaimer_ids") or [],
        "fact_refs": email.get("fact_refs") or [],
        "personalization_refs": email.get("personalization_refs") or [],
    }


def _select_sms(bundle: dict[str, Any]) -> dict[str, Any] | None:
    sms = bundle.get("sms")
    if not isinstance(sms, dict):
        return None
    return {
        "text": sms.get("text"),
        "cta_url": sms.get("cta_url"),
        "fact_refs": sms.get("fact_refs") or [],
        "personalization_refs": sms.get("personalization_refs") or [],
    }


def _select_claim_evidence(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    for item in bundle.get("claim_evidence") or []:
        if not isinstance(item, dict):
            continue
        selected.append(
            {
                "artifact_path": item.get("artifact_path"),
                "channel": item.get("channel"),
                "claim_id": item.get("claim_id"),
                "claim_type": item.get("claim_type"),
                "fact_id": item.get("fact_id"),
                "source_id": item.get("source_id"),
                "text_fragment": item.get("text_fragment"),
                "normalized_value": item.get("normalized_value"),
            }
        )
    return selected


def _build_case(source: Path, case_id: str) -> dict[str, Any]:
    outcome = _read_json(source / "cases" / case_id / "outcome.json")
    if outcome.get("case_id") != case_id:
        raise ExportError(f"case ID mismatch: {case_id}")

    input_data = _as_dict(outcome.get("input"))
    package = _as_dict(outcome.get("package"))
    bundle = _as_dict(package.get("bundle"))
    quality = _as_dict(package.get("quality_report"))
    run = _as_dict(outcome.get("run"))
    validation = _as_dict(outcome.get("validation"))
    metrics = _as_dict(outcome.get("metrics"))
    tool_receipts = run.get("tool_receipts") or []
    save_calls = sum(receipt == "mcp_factory__cf_draft_save" for receipt in tool_receipts)

    failure = None
    if case_id in EXPECTED_FAILURES:
        failure = {
            "reason_code": run.get("reason_code"),
            "run_status": run.get("status"),
            "draft_save_calls": save_calls,
            "package_qa_passed": quality.get("approvable") is True,
            "note": (
                "Кейс завершён как FAILED: после первого физического сохранения агент "
                "повторно вызвал cf_draft_save, и runtime вернул TOOL_SEQUENCE_INVALID. "
                "Сформированный пакет прошёл детерминированный QA, поэтому его фактический "
                "выход сохранён для анализа, но кейс считается неуспешным."
            ),
        }

    prompt_tokens = int(metrics.get("prompt_tokens") or 0)
    completion_tokens = int(metrics.get("completion_tokens") or 0)
    result = {
        "case_id": case_id,
        "title": input_data.get("title"),
        "synthetic": input_data.get("synthetic") is True,
        "passed": outcome.get("passed") is True,
        "mode": outcome.get("mode"),
        "input": {
            "brief": input_data.get("brief") or {},
            "expected": input_data.get("expected") or {},
        },
        "execution": {
            "expected_initial": outcome.get("expected_initial"),
            "actual_initial": outcome.get("actual_initial"),
            "expected_terminal": outcome.get("expected_terminal"),
            "actual_terminal": outcome.get("actual_terminal"),
            "expected_channels": outcome.get("expected_channels") or {},
            "actual_channels": outcome.get("actual_channels") or {},
            "run_status": run.get("status"),
            "reason_code": run.get("reason_code"),
            "package_version": package.get("package_version"),
        },
        "outputs": {
            "sms": _select_sms(bundle),
            "email": _select_email(bundle),
            "channel_suppressions": bundle.get("channel_suppressions") or [],
            "warnings": bundle.get("warnings") or [],
            "summary": bundle.get("summary"),
        },
        "qa": {
            "assertions": outcome.get("assertions") or {},
            "validation_status": validation.get("status"),
            "validation_blockers": validation.get("blockers") or [],
            "validation_questions": validation.get("questions") or [],
            "approvable": quality.get("approvable"),
            "deterministic_score": quality.get("deterministic_score"),
            "checked_ids": quality.get("checked_ids") or [],
            "checked_fact_ids": quality.get("checked_fact_ids") or [],
            "checked_policy_ids": quality.get("checked_policy_ids") or [],
            "findings": quality.get("findings") or [],
            "sms_metrics": quality.get("sms_metrics"),
            "claim_evidence": _select_claim_evidence(bundle),
        },
        "metrics": {
            "provider_calls": int(metrics.get("provider_calls") or 0),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cached_tokens": int(metrics.get("cached_tokens") or 0),
            "cache_write_tokens": int(metrics.get("cache_write_tokens") or 0),
            "cost_usd": float(metrics.get("cost_usd") or 0),
            "user_visible_terminal_ms": int(metrics.get("user_visible_terminal_ms") or 0),
            "workflow_elapsed_ms": int(metrics.get("workflow_elapsed_ms") or 0),
            "full_worker_occupancy_ms": int(metrics.get("full_worker_occupancy_ms") or 0),
            "usage_complete": metrics.get("usage_complete") is True,
        },
        "failure": failure,
    }
    if result["synthetic"] is not True:
        raise ExportError(f"non-synthetic case refused: {case_id}")
    return result


def build_report(source: Path) -> dict[str, Any]:
    report, attempt, failed = _verify_source(source)
    cases = [_build_case(source, case_id) for case_id in EXPECTED_CASES]
    failed_cases = [case["case_id"] for case in cases if not case["passed"]]
    if failed_cases != list(EXPECTED_FAILURES):
        raise ExportError(f"unexpected failed cases: {failed_cases}")

    case_tokens = sum(case["metrics"]["total_tokens"] for case in cases)
    case_cost = round(sum(case["metrics"]["cost_usd"] for case in cases), 8)
    usage = report.get("usage") or {}
    if case_tokens != usage.get("total_tokens") or case_cost != usage.get("total_cost_usd"):
        raise ExportError("per-case usage does not reconcile with the source report")

    return {
        "schema_version": 1,
        "report_type": "MVP_TESTING_REPORT",
        "canonical_release_evidence": False,
        "evidence_status": "FAILED_NON_EVIDENCE",
        "title": "Отчёт о тестировании MVP — Basket-03",
        "source": {
            "evaluation_id": EVALUATION_ID,
            "executed_app_commit": EXECUTED_APP_COMMIT,
            "recommended_code_base_commit": CODE_BASE_COMMIT,
            "raw_report_sha256": failed.get("report_sha256"),
            "raw_checksums_sha256": failed.get("checksums_sha256"),
            "provider": attempt.get("provider"),
            "model": attempt.get("model"),
            "provider_profile": report.get("provider_profile"),
            "execution_kind": report.get("execution_kind"),
            "generated_at": report.get("generated_at"),
        },
        "scope": {
            "synthetic_only": True,
            "no_send": report.get("no_send") is True,
            "anonymization": (
                "Экспорт содержит только синтетические брифы, фактические тексты каналов, "
                "агрегированные QA/usage/latency и разрешённые доменные идентификаторы. "
                "Runtime-, task-, project-, package-, provider-request-ID и сырые трассы исключены."
            ),
        },
        "result": {
            "status": "FAIL",
            "passed_cases": 13,
            "total_cases": 15,
            "live_passed_cases": 10,
            "live_total_cases": 12,
            "validation_passed_cases": 3,
            "validation_total_cases": 3,
            "failed_cases": list(EXPECTED_FAILURES),
            "release_targets_passed": False,
            "canonical_latency_passed": report.get("canonical_latency_passed") is True,
            "functional_quality_passed": report.get("functional_quality_passed") is True,
            "summary": (
                "Basket-03 дал 13 успешных исходов из 15. B05 и B08 явно сохранены "
                "как неуспешные; весь прогон остаётся failed/non-evidence и не является "
                "каноническим release evidence."
            ),
        },
        "aggregate_metrics": {
            "provider_calls": report.get("provider_calls"),
            "prompt_tokens": sum(case["metrics"]["prompt_tokens"] for case in cases),
            "completion_tokens": sum(case["metrics"]["completion_tokens"] for case in cases),
            "total_tokens": usage.get("total_tokens"),
            "total_cost_usd": usage.get("total_cost_usd"),
            "usage_complete": usage.get("usage_complete") is True,
            "latency_p50_ms": (report.get("latency") or {}).get("p50_ms"),
            "latency_p95_ms": (report.get("latency") or {}).get("p95_ms"),
            "latency_max_ms": (report.get("latency") or {}).get("max_ms"),
        },
        "cases": cases,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_csv(path: Path, cases: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "title",
        "passed",
        "mode",
        "actual_initial",
        "actual_terminal",
        "sms_status",
        "email_status",
        "qa_approvable",
        "qa_score",
        "provider_calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_usd",
        "user_visible_terminal_ms",
        "workflow_elapsed_ms",
        "run_status",
        "reason_code",
        "failure_note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for case in cases:
            execution = case["execution"]
            metrics = case["metrics"]
            writer.writerow(
                {
                    "case_id": case["case_id"],
                    "title": case["title"],
                    "passed": case["passed"],
                    "mode": case["mode"],
                    "actual_initial": execution["actual_initial"],
                    "actual_terminal": execution["actual_terminal"],
                    "sms_status": execution["actual_channels"].get("sms"),
                    "email_status": execution["actual_channels"].get("email"),
                    "qa_approvable": case["qa"]["approvable"],
                    "qa_score": case["qa"]["deterministic_score"],
                    "provider_calls": metrics["provider_calls"],
                    "prompt_tokens": metrics["prompt_tokens"],
                    "completion_tokens": metrics["completion_tokens"],
                    "total_tokens": metrics["total_tokens"],
                    "cost_usd": f"{metrics['cost_usd']:.8f}",
                    "user_visible_terminal_ms": metrics["user_visible_terminal_ms"],
                    "workflow_elapsed_ms": metrics["workflow_elapsed_ms"],
                    "run_status": execution["run_status"],
                    "reason_code": execution["reason_code"],
                    "failure_note": (case["failure"] or {}).get("note"),
                }
            )


def _safe(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return html.escape(str(value))


def _render_html(report: dict[str, Any]) -> str:
    metrics = report["aggregate_metrics"]
    summary_rows = []
    case_sections = []
    for case in report["cases"]:
        execution = case["execution"]
        case_metrics = case["metrics"]
        status = "PASS" if case["passed"] else "FAIL"
        summary_rows.append(
            "<tr>"
            f"<td><strong>{_safe(case['case_id'])}</strong></td>"
            f"<td>{_safe(case['title'])}</td>"
            f"<td><span class='status {status.lower()}'>{status}</span></td>"
            f"<td>{_safe(case['mode'])}</td>"
            f"<td>{_safe(execution['actual_terminal'])}</td>"
            f"<td>{case_metrics['provider_calls']}</td>"
            f"<td>{case_metrics['total_tokens']:,}</td>"
            f"<td>{case_metrics['user_visible_terminal_ms'] / 1000:.1f} s</td>"
            "</tr>"
        )

        brief = case["input"]["brief"]
        sms = case["outputs"]["sms"]
        email = case["outputs"]["email"]
        sms_html = (
            f"<p>{_safe(sms.get('text'))}</p>"
            f"<div class='meta'>CTA: {_safe(sms.get('cta_url'))}</div>"
            if sms
            else f"<p class='muted'>SMS: {_safe(execution['actual_channels'].get('sms'))}</p>"
        )
        email_html = (
            f"<div class='meta'>Тема: <strong>{_safe(email.get('subject'))}</strong></div>"
            f"<div class='meta'>Прехедер: {_safe(email.get('preheader'))}</div>"
            f"<p>{_safe(email.get('plain_text'))}</p>"
            f"<div class='meta'>CTA: {_safe(email.get('cta_label'))} · {_safe(email.get('cta_url'))}</div>"
            if email
            else f"<p class='muted'>E-mail: {_safe(execution['actual_channels'].get('email'))}</p>"
        )
        assertions = (
            ", ".join(
                f"{key}={str(value).lower()}" for key, value in case["qa"]["assertions"].items()
            )
            or "—"
        )
        failure_html = ""
        if case["failure"]:
            failure_html = (
                "<div class='failure'><strong>Неуспешный кейс.</strong> "
                f"{_safe(case['failure']['note'])}</div>"
            )
        case_sections.append(
            f"""
            <section class="case {status.lower()}">
              <div class="case-head">
                <div><span class="eyebrow">{_safe(case["case_id"])} · {_safe(case["mode"])}</span>
                <h2>{_safe(case["title"])}</h2></div>
                <span class="status {status.lower()}">{status}</span>
              </div>
              {failure_html}
              <div class="brief">
                <strong>Вход:</strong> {_safe(brief.get("objective"))}<br>
                <span class="meta">Продукт: {_safe(brief.get("product_id"))} · Сегмент: {_safe(brief.get("segment_id"))} · Тон: {_safe(brief.get("tone"))}</span><br>
                <span class="meta">Примечания: {_safe(brief.get("notes"))}</span>
              </div>
              <div class="channels">
                <article><h3>SMS · {_safe(execution["actual_channels"].get("sms"))}</h3>{sms_html}</article>
                <article><h3>E-mail · {_safe(execution["actual_channels"].get("email"))}</h3>{email_html}</article>
              </div>
              <div class="qa">
                <strong>QA:</strong> approvable={_safe(case["qa"]["approvable"])}; score={_safe(case["qa"]["deterministic_score"])}; findings={len(case["qa"]["findings"])}; assertions: {_safe(assertions)}<br>
                <span class="meta">Состояние: {_safe(execution["actual_initial"])} → {_safe(execution["actual_terminal"])}; provider calls: {case_metrics["provider_calls"]}; tokens: {case_metrics["total_tokens"]:,}; cost: ${case_metrics["cost_usd"]:.8f}; latency: {case_metrics["user_visible_terminal_ms"] / 1000:.1f} s.</span>
              </div>
            </section>
            """
        )

    rendered = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_safe(report["title"])}</title>
  <style>
    :root {{ --ink:#172033; --muted:#647086; --line:#d9dee8; --good:#177245; --bad:#b42318; --paper:#fff; --soft:#f5f7fa; --accent:#2156a5; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--soft); font:14px/1.45 "Segoe UI", Arial, sans-serif; letter-spacing:0; }}
    main {{ max-width:1160px; margin:0 auto; background:var(--paper); padding:38px 46px 64px; }}
    h1 {{ margin:7px 0 8px; font-size:34px; line-height:1.15; letter-spacing:0; }}
    h2 {{ margin:4px 0 0; font-size:20px; line-height:1.25; letter-spacing:0; }}
    h3 {{ margin:0 0 9px; font-size:14px; letter-spacing:0; }}
    p {{ margin:7px 0; }}
    .eyebrow {{ color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; }}
    .lead {{ color:var(--muted); max-width:900px; font-size:16px; }}
    .warning {{ margin:22px 0; padding:13px 16px; border-left:4px solid var(--bad); background:#fff3f1; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border:1px solid var(--line); margin:22px 0; }}
    .metric {{ padding:14px 16px; border-right:1px solid var(--line); }}
    .metric:last-child {{ border-right:0; }}
    .metric strong {{ display:block; font-size:23px; }}
    .metric span,.meta,.muted {{ color:var(--muted); }}
    table {{ width:100%; border-collapse:collapse; margin:20px 0 26px; font-size:12px; }}
    th,td {{ padding:8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ background:var(--soft); }}
    .status {{ display:inline-block; min-width:48px; padding:3px 7px; color:#fff; font-size:11px; font-weight:700; text-align:center; border-radius:3px; }}
    .status.pass {{ background:var(--good); }} .status.fail {{ background:var(--bad); }}
    .case {{ margin-top:26px; padding-top:24px; border-top:2px solid var(--line); }}
    .case-head {{ display:flex; gap:16px; align-items:flex-start; justify-content:space-between; }}
    .brief,.qa,.failure {{ margin:13px 0; padding:11px 13px; background:var(--soft); border-left:3px solid var(--accent); }}
    .failure {{ background:#fff3f1; border-left-color:var(--bad); }}
    .channels {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    article {{ border:1px solid var(--line); padding:13px; min-width:0; }}
    article p {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
    footer {{ margin-top:30px; padding-top:18px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; }}
    code {{ overflow-wrap:anywhere; }}
    @media (max-width:760px) {{ main {{ padding:24px 18px; }} .metrics,.channels {{ grid-template-columns:1fr; }} .metric {{ border-right:0; border-bottom:1px solid var(--line); }} table {{ display:block; overflow:auto; }} }}
    @media print {{ @page {{ size:A4; margin:11mm; }} body {{ background:#fff; font-size:10px; }} main {{ max-width:none; padding:0; }} h1 {{ font-size:25px; }} .case {{ break-before:page; margin-top:0; padding-top:0; border-top:0; }} .channels {{ gap:7px; }} article,.brief,.qa,.failure {{ padding:8px; }} a {{ color:inherit; text-decoration:none; }} }}
  </style>
</head>
<body><main>
  <span class="eyebrow">Synthetic-only · no-send · GLM-5.2 via OpenRouter</span>
  <h1>{_safe(report["title"])}</h1>
  <p class="lead">Обезличенный пакет фактических результатов. Код исполнения: <code>{EXECUTED_APP_COMMIT[:7]}</code>; рекомендуемая основа кода: <code>{CODE_BASE_COMMIT[:7]}</code>.</p>
  <div class="warning"><strong>Не является каноническим release evidence.</strong> Basket-03 формально имеет статус failed/non-evidence: пройдено 13 из 15, B05 и B08 сохранены как неуспешные.</div>
  <div class="metrics">
    <div class="metric"><strong>13 / 15</strong><span>успешных исходов</span></div>
    <div class="metric"><strong>10 / 12</strong><span>live Ouroboros</span></div>
    <div class="metric"><strong>{metrics["total_tokens"]:,}</strong><span>provider tokens</span></div>
    <div class="metric"><strong>${metrics["total_cost_usd"]:.4f}</strong><span>стоимость прогона</span></div>
  </div>
  <p>{_safe(report["scope"]["anonymization"])}</p>
  <table><thead><tr><th>Кейс</th><th>Сценарий</th><th>Итог</th><th>Режим</th><th>Terminal</th><th>Calls</th><th>Tokens</th><th>Latency</th></tr></thead><tbody>{"".join(summary_rows)}</tbody></table>
  {"".join(case_sections)}
  <footer>Evaluation ID: <code>{EVALUATION_ID}</code>. Сырые runtime-артефакты, provider payloads и технические идентификаторы в пакет не включены. Полные структурированные данные находятся в JSON и CSV рядом с этим отчётом.</footer>
</main></body></html>
"""
    return "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"


def _readme(report: dict[str, Any]) -> str:
    metrics = report["aggregate_metrics"]
    return f"""# Basket-03 — отчёт о тестировании MVP

Это обезличенный пакет фактического live-прогона `{EVALUATION_ID}`.

**Статус:** `FAILED_NON_EVIDENCE`. Пройдено **13/15** кейсов: **10/12** live Ouroboros и
**3/3** validation-only. `B05` и `B08` явно сохранены как неуспешные. Этот пакет можно
использовать как честный отчёт о тестировании MVP, но нельзя называть каноническим release evidence.

## Привязка

- рекомендуемая основа кода: `{CODE_BASE_COMMIT}`;
- commit, на котором физически выполнен Basket-03: `{EXECUTED_APP_COMMIT}`;
- provider/model: `openrouter` / `z-ai/glm-5.2`;
- usage: `{metrics["total_tokens"]}` токенов, `${metrics["total_cost_usd"]:.8f}`, `{metrics["provider_calls"]}` provider calls;
- исходный `report.json` SHA-256: `{report["source"]["raw_report_sha256"]}`.

## Содержимое

- `basket03-report.json` — полный обезличенный структурированный отчёт;
- `summary.csv` — плоская сводка по 15 кейсам;
- `cases/B01.json` … `cases/B15.json` — вход, фактические SMS/e-mail, QA, режим, latency и usage;
- `report.html` и `report.pdf` — компактное представление для просмотра;
- `checksums.sha256` — SHA-256 всех файлов пакета, кроме самого checksum manifest.

Сырые `runtime/`, transport payloads, provider-request/generation ID, task/project/package ID,
секреты и внутренние tool-трассы не публикуются. Все входные данные синтетические; отправки SMS
или e-mail не выполнялись.
"""


def _scan_generated(output: Path) -> None:
    for path in output.rglob("*"):
        if not path.is_file() or path.name == "report.pdf":
            continue
        text = path.read_text(encoding="utf-8-sig" if path.suffix == ".csv" else "utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                raise ExportError(f"forbidden runtime/secret-like token in {path.name}")


def refresh_checksums(output: Path) -> None:
    if not output.is_dir():
        raise ExportError(f"output directory does not exist: {output}")
    entries = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            entries.append(f"{_sha256(path)}  {path.relative_to(output).as_posix()}")
    (output / "checksums.sha256").write_text(
        "\n".join(entries) + "\n", encoding="ascii", newline="\n"
    )


def export_report(source: Path, output: Path) -> dict[str, Any]:
    report = build_report(source)
    output.mkdir(parents=True, exist_ok=True)
    cases_dir = output / "cases"
    cases_dir.mkdir(exist_ok=True)
    _write_json(output / "basket03-report.json", report)
    for case in report["cases"]:
        _write_json(cases_dir / f"{case['case_id']}.json", case)
    _write_csv(output / "summary.csv", report["cases"])
    (output / "report.html").write_text(_render_html(report), encoding="utf-8", newline="\n")
    (output / "README.md").write_text(_readme(report), encoding="utf-8", newline="\n")
    _scan_generated(output)
    refresh_checksums(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Export sanitized Basket-03 MVP report")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checksums-only", action="store_true")
    args = parser.parse_args()
    if args.checksums_only:
        refresh_checksums(args.output.resolve())
        return
    if args.source is None:
        parser.error("--source is required unless --checksums-only is used")
    report = export_report(args.source.resolve(), args.output.resolve())
    print(
        json.dumps(
            {
                "status": "PASS",
                "evaluation_id": report["source"]["evaluation_id"],
                "passed": report["result"]["passed_cases"],
                "total": report["result"]["total_cases"],
                "failed": report["result"]["failed_cases"],
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
