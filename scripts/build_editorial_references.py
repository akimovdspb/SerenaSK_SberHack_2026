# ruff: noqa: RUF001 -- Russian editorial fixtures intentionally contain Cyrillic text.
from __future__ import annotations

import json
import pathlib
import subprocess
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "cbcdfc2286e62004ce417173bd1bf0a72f3938e8"
LABEL = "EDITORIAL_REFERENCE_NOT_LIVE_NOT_RELEASE_EVIDENCE"
REPORT_ROOT = "reports/demo-copy-v2-20260717-06/cases"
ATTEMPTS = {
    "DQ01": "demo-copy-v2-expansion-v2-20260717-06-dq01-a2.json",
    "DQ03": "demo-copy-v2-expansion-v2-20260717-06-dq03-a1.json",
    "DQ06": "demo-copy-v2-expansion-v2-20260717-06-dq06-a2.json",
    "DQ07": "demo-copy-v2-expansion-v2-20260717-06-dq07-a1.json",
    "DQ09": "demo-copy-v2-expansion-v2-20260717-06-dq09-a1.json",
    "DQ11": "demo-copy-v2-expansion-v2-20260717-06-dq11-a1.json",
    "DQ12": "demo-copy-v2-expansion-v2-20260717-06-dq12-a1.json",
}
SOURCE_LABEL = "Синтетическая факт-карточка редакционного примера"


def _fact(
    label: str,
    text: str,
    kind: str,
    value: Any,
    *forms: str,
) -> dict[str, Any]:
    return {
        "label": label,
        "canonical_text": text,
        "kind": kind,
        "source_label": SOURCE_LABEL,
        "normalized_value": value,
        "allowed_surface_forms": list(forms),
    }


CUSTOM_FACTS: dict[str, list[dict[str, Any]]] = {
    "DQ03": [
        _fact(
            "Представительские расходы",
            "Представительские расходы видны в отдельной категории.",
            "text",
            "separate_expense_category",
        ),
        _fact(
            "Стоимость обслуживания",
            "Обслуживание стоит 1 490 ₽ в месяц.",
            "money",
            {"value": 1490, "unit": "RUB/month"},
            "Обслуживание — 1 490 ₽ в месяц.",
        ),
        _fact(
            "Дополнительные карты",
            "К основной карте можно выпустить до 5 дополнительных карт.",
            "number",
            5,
        ),
        _fact(
            "Уведомления",
            "Уведомления об операциях доступны в приложении.",
            "text",
            "operation_notifications_in_app",
        ),
    ],
    "DQ06": [
        _fact(
            "Кредитный лимит",
            "Кредитный лимит — до 700 000 ₽.",
            "money",
            {"value": 700000, "unit": "RUB"},
        ),
        _fact(
            "Льготный период",
            "Льготный период — до 45 дней.",
            "duration",
            {"value": 45, "unit": "day"},
        ),
        _fact(
            "Контроль расходов",
            "Расходы по карте видны в личном кабинете.",
            "text",
            "expenses_in_personal_account",
        ),
        _fact(
            "Решение банка",
            "Лимит и льготный период зависят от решения банка.",
            "condition",
            "subject_to_bank_decision",
        ),
    ],
    "DQ07": [
        _fact("Ставка", "Ставка составляет 1,4%.", "percentage", 1.4),
        _fact(
            "Условие по обороту",
            "Ставка действует при обороте от 300 000 ₽ в месяц.",
            "money",
            {"value": 300000, "unit": "RUB/month"},
        ),
        _fact(
            "Подключение терминала",
            "Терминал подключается к кассовому рабочему месту.",
            "text",
            "terminal_to_cashier_workspace",
        ),
        _fact(
            "Статусы оплат",
            "Статусы оплат и возвратов доступны в личном кабинете.",
            "text",
            "payment_and_refund_statuses",
        ),
        _fact(
            "Сводка продаж",
            "Сводка помогает сопоставлять продажи по торговым точкам.",
            "text",
            "sales_by_location_summary",
        ),
    ],
    "DQ09": [
        _fact("Промо-ставка", "Промо-ставка составляет 0,9%.", "percentage", 0.9),
        _fact(
            "Мобильная страница",
            "Страница оплаты адаптируется под экран смартфона.",
            "text",
            "mobile_adaptive_checkout",
        ),
        _fact(
            "Промо-период",
            "Промо-ставка действует первые 3 месяца.",
            "duration",
            {"value": 3, "unit": "month"},
        ),
        _fact(
            "Условие по обороту",
            "Промо-ставка действует при обороте до 1 000 000 ₽ в месяц.",
            "money",
            {"value": 1000000, "unit": "RUB/month"},
        ),
        _fact(
            "Тариф после промо",
            "После промо-периода тариф определяется условиями договора.",
            "condition",
            "contract_terms_after_promo",
        ),
        _fact(
            "Статусы платежей",
            "Статусы платежей и возвратов доступны в личном кабинете.",
            "text",
            "payment_and_refund_statuses",
        ),
    ],
    "DQ11": [
        _fact(
            "Периодичность отчёта",
            "Новый отчёт выходит 1 раз в месяц.",
            "number",
            1,
        ),
        _fact(
            "Отраслевые показатели",
            "В обзоре собраны 12 отраслевых показателей.",
            "number",
            12,
        ),
        _fact(
            "Пояснения динамики",
            "К показателям добавлены краткие пояснения динамики.",
            "text",
            "metric_dynamics_explanations",
        ),
        _fact(
            "Архив обзоров",
            "Архив обзоров доступен в личном кабинете.",
            "text",
            "reports_archive_in_personal_account",
        ),
    ],
    "DQ12": [
        _fact(
            "Количество счетов",
            "В панели можно объединить до 20 счетов.",
            "number",
            20,
        ),
        _fact(
            "Статус и ответственный",
            "Для каждого счёта видны текущий статус и ответственный.",
            "text",
            "account_status_and_owner",
        ),
        _fact(
            "Фильтры",
            "Фильтры помогают отделять счета, требующие внимания.",
            "text",
            "attention_filters",
        ),
        _fact(
            "Права просмотра",
            "Права просмотра задаются для рабочих ролей.",
            "text",
            "role_based_view_permissions",
        ),
    ],
}


def _source_json(path: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "show", f"{SOURCE_COMMIT}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"source is not an object: {path}")
    return value


def _reference(case_id: str, source: dict[str, Any]) -> dict[str, Any]:
    fixture = source["fixture"]
    draft = source["save"]["draft"]
    if source["case_id"] != case_id or draft["case_id"] != case_id:
        raise RuntimeError(f"case identity mismatch: {case_id}")
    persona = (
        ("segment_payroll", "trigger_team_growth")
        if case_id == "DQ01"
        else ("segment_growth", "trigger_planning")
    )
    brief = {
        "name": f"{fixture['product_name']}: {fixture['main_message']}",
        "objective": fixture["main_message"],
        "product_id": "synthetic_payroll" if case_id == "DQ01" else None,
        "segment_id": persona[0],
        "trigger_id": persona[1],
        "channels": fixture["channels"],
        "cta_label": fixture["cta_label"],
        "cta_url": fixture["cta_url"],
        "tone": fixture["touch_history"]["desired_tone"],
        "offer_period": None,
        "notes": (
            f"Сценарий: {fixture['business_trigger']} "
            f"Задача: {fixture['audience']['current_task']} "
            f"Проблема: {fixture['client_problem']}"
        ),
        "synthetic": True,
    }
    custom_product = None
    if case_id != "DQ01":
        custom_product = {
            "exact_name": fixture["product_name"],
            "cta_label": fixture["cta_label"],
            "cta_url": fixture["cta_url"],
            "facts": CUSTOM_FACTS[case_id],
        }
    return {
        "reference_id": f"editorial_{case_id.lower()}",
        "title": f"{fixture['product_name']} · {fixture['audience']['role']}",
        "description": fixture["client_need"],
        "label": LABEL,
        "brief": brief,
        "custom_product": custom_product,
        "saved_draft": draft,
    }


def main() -> None:
    references = [
        _reference(case_id, _source_json(f"{REPORT_ROOT}/{filename}"))
        for case_id, filename in ATTEMPTS.items()
    ]
    output = {
        "schema_version": 1,
        "label": LABEL,
        "source_commit": SOURCE_COMMIT,
        "references": references,
    }
    target = ROOT / "data" / "editorial" / "copy_quality_references.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {target.relative_to(ROOT)} with {len(references)} references")


if __name__ == "__main__":
    main()
