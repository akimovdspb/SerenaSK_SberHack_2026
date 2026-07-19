import type { ExecutionMode } from "../types";

export type StatusTone = "success" | "danger" | "warning" | "active" | "neutral";

export type StatusPresentation = {
  label: string;
  tone: StatusTone;
  raw: string;
  known: boolean;
};

// Исчерпывающий presentation-маппинг доменных значений, которые уже отдаёт API.
// Значения перечислены по контрактам: campaign/package (§10.1), rule (§10.2),
// run terminal states, QA findings, evaluation slice и diagnostics read model.
// Неизвестное значение сознательно получает нейтральный тон и сырую подпись.
const STATUS_MAP: Record<string, { label: string; tone: StatusTone }> = {
  // Кампания и пакет
  DRAFT: { label: "Черновик", tone: "neutral" },
  NEEDS_INPUT: { label: "Нужны данные", tone: "warning" },
  READY: { label: "Готов", tone: "success" },
  QUEUED: { label: "В очереди", tone: "active" },
  RUNNING: { label: "В работе", tone: "active" },
  CANCEL_REQUESTED: { label: "Отменяется", tone: "active" },
  REVIEW_REQUIRED: { label: "Нужна проверка", tone: "warning" },
  APPROVABLE: { label: "Можно утверждать", tone: "success" },
  BLOCKED: { label: "Заблокирован", tone: "danger" },
  NOT_APPLICABLE: { label: "Не применимо", tone: "neutral" },
  FAILED: { label: "Ошибка", tone: "danger" },
  CANCELLED: { label: "Отменён", tone: "neutral" },
  APPROVED: { label: "Утверждён", tone: "success" },
  ACCEPTED_WITH_WARNING: { label: "Принят с замечанием", tone: "warning" },
  EXPORTED: { label: "Экспортирован", tone: "success" },
  // Терминальные статусы run
  COMPLETED: { label: "Завершён", tone: "success" },
  COMPLETED_FALLBACK: { label: "Завершён резервным шаблоном", tone: "warning" },
  // Жизненный цикл правила
  PROPOSED: { label: "Предложено", tone: "active" },
  VALIDATION_FAILED: { label: "Не прошло проверку", tone: "danger" },
  READY_FOR_APPROVAL: { label: "Готово к решению", tone: "active" },
  REJECTED: { label: "Отклонено", tone: "danger" },
  ROLLED_BACK: { label: "Откачено", tone: "neutral" },
  // QA findings
  BLOCKER: { label: "Блокирующая ошибка", tone: "danger" },
  WARNING: { label: "Предупреждение", tone: "warning" },
  INFO: { label: "Справочно", tone: "neutral" },
  OPEN: { label: "Открыто", tone: "danger" },
  FIXED: { label: "Исправлено", tone: "success" },
  ACCEPTED: { label: "Принято", tone: "success" },
  RECHECKED: { label: "Перепроверено", tone: "success" },
  // Evaluation slice
  FROZEN: { label: "Заморожен", tone: "success" },
  NOT_FROZEN: { label: "Не заморожен", tone: "warning" },
  WAITING_FOR_OPERATOR: { label: "Ожидает оператора", tone: "warning" },
  COMPLETE: { label: "Завершено", tone: "success" },
  // Diagnostics: компоненты, очередь и admission
  DEGRADED: { label: "Требует внимания", tone: "warning" },
  ISOLATED: { label: "Изолирован", tone: "success" },
  IDLE: { label: "Очередь пуста", tone: "neutral" },
  ACTIVE: { label: "Есть активные", tone: "active" },
  // admission_state: CLOSED означает «fail-closed контракт зафиксирован» (норма),
  // OPEN — «контракт не подтверждён, генерация заблокирована».
  CLOSED: { label: "Контракт зафиксирован", tone: "success" },
  EMPTY: { label: "Не запускался", tone: "neutral" },
};

// admission_state=OPEN конфликтует по ключу с finding OPEN, поэтому admission
// оформляется отдельным явным представлением на вызывающей стороне.
export const ADMISSION_OPEN: StatusPresentation = {
  label: "Контракт не подтверждён",
  tone: "danger",
  raw: "OPEN",
  known: true,
};

export function presentStatus(value: string | null): StatusPresentation {
  const raw = value ?? "EMPTY";
  const found = STATUS_MAP[raw];
  if (found) return { ...found, raw, known: true };
  return { label: raw, tone: "neutral", raw, known: false };
}

// Режимы исполнения: русская подпись первична, точный идентификатор — вторичная
// monospace-подпись. Различия живой генерации, повтора и шаблона никогда не скрываются.
export const MODE_PRESENTATION: Record<ExecutionMode, { label: string }> = {
  live_ouroboros: { label: "Живая генерация · Ouroboros" },
  deterministic_template: { label: "Резервный шаблон" },
  replay: { label: "Повтор · сохранённый результат" },
  validation_only: { label: "Только проверка" },
  mock: { label: "Тестовый режим" },
};
