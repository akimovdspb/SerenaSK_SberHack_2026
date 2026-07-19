type Presentation = {
  label: string;
  raw: string;
  known: boolean;
};

function present(value: string | null | undefined, labels: Record<string, string>): Presentation {
  const raw = value ?? "";
  const label = labels[raw];
  return label ? { label, raw, known: true } : { label: raw || "—", raw, known: false };
}

const OPERATION_LABELS = {
  initial: "Первичная генерация",
  revision: "Доработка",
  rule_proposal: "Предложение правила",
};

const FACT_KIND_LABELS = {
  concept: "Понятие",
  duration: "Срок",
  text: "Текст",
  url: "Ссылка",
};

const RULE_TYPE_LABELS = {
  require_concept_id: "Обязательное понятие",
};

const RISK_LABELS = {
  low: "Низкий",
  medium: "Средний",
  high: "Высокий",
};

const TEST_KIND_LABELS = {
  target: "Целевой",
  regression: "Регрессионный",
  out_of_scope: "Вне области",
};

const DISABLED_REASON_LABELS: Record<string, string> = {
  PACKAGE_UNAVAILABLE: "Комплект ещё не сохранён.",
  STALE_PACKAGE: "Открыта не текущая версия комплекта.",
  STALE_CONTEXT: "Контекст изменился; откройте свежую версию после завершения операции.",
  PENDING_FEEDBACK: "Сначала завершите сохранённую доработку.",
  QA_BLOCKER: "Сначала устраните блокирующие замечания качества.",
  PACKAGE_NOT_APPROVED: "Сначала утвердите текущую версию комплекта.",
};

const CHANNEL_LABELS = {
  email: "Письмо",
  sms: "СМС",
};

const CASE_TITLES: Record<string, string> = {
  B01: "Рост команды: выплаты сотрудникам и самозанятым",
  B02: "Полный обычный бриф без сохранённого правила",
  B03: "Следующая кампания с утверждённым правилом",
  B04: "Разрешённый срок из факт-карточки",
  B05: "Неподтверждённые «99%» и «мгновенно» остаются непроверенными заметками",
  B06: "Обязательная маркировка синтетических данных",
  B07: "Ссылка использует только разрешённый HTTPS-домен и UTM-метки",
  B08: "Кириллица и эмодзи дают точные метрики UCS-2",
  B09: "СМС запрещены, письмо разрешено политикой согласий",
  B10: "Письмо запрещено, СМС разрешены политикой согласий",
  B11: "Оба канала запрещены политикой согласий",
  B12: "Продукт уже подключён",
  B13: "Отсутствует критический факт",
  B14: "Внедрение инструкции в заметках не меняет план содержания",
  B15: "Точечное замечание создаёт только разрешённую доработку",
};

export const presentOperation = (value: string) => present(value, OPERATION_LABELS);
export const presentFactKind = (value: string) => present(value, FACT_KIND_LABELS);
export const presentRuleType = (value: string) => present(value, RULE_TYPE_LABELS);
export const presentRisk = (value: string) => present(value, RISK_LABELS);
export const presentTestKind = (value: string) => present(value, TEST_KIND_LABELS);
export const presentDisabledReason = (value: string | null) => {
  const raw = value ?? "";
  const label = DISABLED_REASON_LABELS[raw];
  return label
    ? { label, raw, known: true }
    : { label: "Действие недоступно на этом этапе.", raw, known: false };
};
export const presentChannel = (value: string | null) => present(value, CHANNEL_LABELS);
export const presentCaseTitle = (caseId: string, fallback: string) =>
  CASE_TITLES[caseId] ?? fallback;
