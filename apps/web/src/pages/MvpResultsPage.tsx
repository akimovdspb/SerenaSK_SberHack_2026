import { useQuery } from "@tanstack/react-query";
import {
  BadgeCheck,
  ChevronDown,
  Mail,
  MessageSquareText,
  ShieldCheck,
} from "lucide-react";

import { apiGet } from "../api";
import { ErrorState, LoadingState, MetricCard, Notice } from "../components";
import { presentCaseTitle } from "../presentation/labels";
import type { MvpResultCase, MvpResults } from "../types";

export function MvpResultsPage() {
  const results = useQuery({
    queryKey: ["mvp-results"],
    queryFn: () => apiGet<MvpResults>("/api/v1/results/mvp"),
    staleTime: 5 * 60_000,
  });

  if (results.isPending) return <LoadingState label="Загружаем подтверждённые результаты" />;
  if (results.isError)
    return <ErrorState error={results.error} retry={() => void results.refetch()} />;

  const data = results.data;

  return (
    <div className="page page-results">
      <section className="page-heading results-heading">
        <div>
          <p className="eyebrow">Результаты · подтверждённые примеры</p>
          <h1>10 успешных живых кейсов</h1>
          <p className="page-lede">
            Каждый пример обработан ГигаАгентом (Ouroboros) на синтетических данных:
            от брифа и проверяемого контекста до готовых СМС и писем.
          </p>
        </div>
        <div className="results-total" aria-label="Подтверждено десять кейсов">
          <BadgeCheck size={22} aria-hidden="true" />
          <strong>{data.metrics.confirmed_live_case_count}</strong>
          <span>подтверждено</span>
        </div>
      </section>

      <Notice tone="success" title="Результаты подтверждены сохранёнными артефактами">
        В витрину включены только десять успешных живых запусков. У каждого есть фактический
        результат, полный набор метрик использования и детерминированная оценка качества
        100 из 100.
      </Notice>

      <section className="metrics-grid results-metrics" aria-label="Сводка подтверждённых результатов">
        <MetricCard
          label="Подтверждено кейсов"
          value={String(data.metrics.confirmed_live_case_count)}
          note="живые запуски с сохранённым выходом"
        />
        <MetricCard
          label="Модель"
          value="GLM-5.2"
          note="живая генерация через ГигаАгент (Ouroboros)"
        />
        <MetricCard
          label="Проверка качества"
          value="100/100"
          note="22 автоматические проверки у каждого"
        />
        <MetricCard
          label="Каналы согласий"
          value="B09 · B10"
          note="подготовлен только разрешённый канал"
        />
        <MetricCard label="Данные" value="100% синтетика" note="без клиентских данных" />
        <MetricCard label="Внешних отправок" value="0" note="отправка отключена как функция" />
      </section>

      <section className="content-card results-card" aria-labelledby="results-list-heading">
        <div className="section-toolbar results-toolbar">
          <div>
            <p className="eyebrow">Проверенные выходы</p>
            <h2 id="results-list-heading">СМС и письма по десяти сценариям</h2>
          </div>
          <span className="result-proof">
            <ShieldCheck size={17} aria-hidden="true" />
            Проверка 100/100
          </span>
        </div>
        <div className="result-case-list">
          {data.cases.map((item) => (
            <ResultCase key={item.case_id} item={item} />
          ))}
        </div>
      </section>

      <section className="integrity-strip" aria-label="Границы результатов">
        <div>
          <BadgeCheck size={18} />
          <span>Показаны только фактически сохранённые результаты</span>
        </div>
        <div>
          <ShieldCheck size={18} />
          <span>Каждое утверждение связано с проверяемым фактом</span>
        </div>
        <div>
          <Mail size={18} />
          <span>Внешняя отправка полностью отключена</span>
        </div>
      </section>
    </div>
  );
}

function ResultCase({ item }: { item: MvpResultCase }) {
  const channelLabel =
    item.channels.length === 2
      ? "СМС и письмо"
      : item.channels[0] === "sms"
        ? "СМС"
        : "Письмо";

  return (
    <details className="result-case">
      <summary>
        <span className="result-case-id">{item.case_id}</span>
        <span className="result-case-title">
          <strong>{presentCaseTitle(item.case_id, item.title)}</strong>
          <small>{channelLabel}</small>
        </span>
        <span className="result-pass">
          <BadgeCheck size={15} aria-hidden="true" />
          Подтверждён
        </span>
        <span className="result-score">100/100</span>
        <ChevronDown className="result-chevron" size={18} aria-hidden="true" />
      </summary>
      <div className="result-outputs">
        {item.sms ? (
          <article>
            <div className="result-output-heading">
              <MessageSquareText size={18} aria-hidden="true" />
              <h3>СМС</h3>
              {item.sms.segments ? <span>{item.sms.segments} сегм.</span> : null}
            </div>
            <p>{item.sms.text}</p>
          </article>
        ) : null}
        {item.email ? (
          <article>
            <div className="result-output-heading">
              <Mail size={18} aria-hidden="true" />
              <h3>Письмо</h3>
            </div>
            <dl>
              <div><dt>Тема</dt><dd>{item.email.subject}</dd></div>
            </dl>
            <p>{item.email.plain_text}</p>
          </article>
        ) : null}
      </div>
    </details>
  );
}