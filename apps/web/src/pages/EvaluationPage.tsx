import { useQuery } from "@tanstack/react-query";
import { ExternalLink, FileCheck2, Gauge, RefreshCw, UsersRound } from "lucide-react";
import { useState } from "react";

import { apiGet } from "../api";
import {
  ActionLink,
  EmptyState,
  ErrorState,
  LoadingState,
  MetricCard,
  ModeBadge,
  Notice,
  StatusBadge,
  TabPanel,
  Tabs,
} from "../components";
import { formatLatency, formatMoney, humanStatus } from "../format";
import { presentCaseTitle } from "../presentation/labels";
import type { EvaluationRun, EvaluationSummary, ExecutionMode } from "../types";

type EvaluationTab = "business" | "chaos";

export function EvaluationPage() {
  const [tab, setTab] = useState<EvaluationTab>("business");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const summaries = useQuery({
    queryKey: ["evaluation-runs"],
    queryFn: () => apiGet<EvaluationSummary[]>("/api/v1/evaluation/runs"),
  });
  const effectiveSelectedId =
    selectedId ??
    summaries.data?.find((item) => item.frozen)?.evaluation_id ??
    "current_development_slice";
  const selected = useQuery({
    queryKey: ["evaluation-run", effectiveSelectedId],
    queryFn: () => apiGet<EvaluationRun>(`/api/v1/evaluation/runs/${effectiveSelectedId}`),
  });

  if (summaries.isPending || selected.isPending)
    return <LoadingState label="Собираем данные оценки" />;
  if (summaries.isError)
    return <ErrorState error={summaries.error} retry={() => void summaries.refetch()} />;
  if (selected.isError)
    return <ErrorState error={selected.error} retry={() => void selected.refetch()} />;

  const run = selected.data;
  const cases = tab === "business" ? run.business_cases : run.chaos_cases;

  return (
    <div className="page page-evaluation">
      <section className="page-heading compact-heading">
        <div>
          <p className="eyebrow">Оценка · доказательства</p>
          <h1>Измерения без подмены</h1>
          <p className="page-lede">
            Живая генерация, резервный шаблон, сохранённый повтор и проверка без генерации
            учитываются раздельно. Незафиксированный срез отделён от доказательств релиза.
          </p>
        </div>
        <div className="evaluation-toolbar">
          <label className="field evaluation-selector">
            <span>Срез</span>
            <select
              value={effectiveSelectedId}
              onChange={(event) => setSelectedId(event.target.value)}
            >
              {summaries.data.map((item) => (
                <option value={item.evaluation_id} key={item.evaluation_id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <button
            aria-label="Обновить срез"
            className="icon-button"
            onClick={() => {
              void summaries.refetch();
              void selected.refetch();
            }}
            type="button"
          >
            <RefreshCw size={17} />
          </button>
        </div>
      </section>

      {!run.frozen ? (
        <Notice tone="warning" title="Срез не заморожен">
          Это честный инженерный предпросмотр текущей базы данных, а не доказательство живой
          оценки. Этот экран не запускает обращения к провайдеру.
        </Notice>
      ) : null}

      <section className="metrics-grid evaluation-metrics" aria-label="Метрики оценки">
        <MetricCard
          label="Статус среза"
          value={humanStatus(run.status)}
          note={run.evaluation_id}
        />
        <MetricCard
          label="Покрытие корзины"
          value={`${run.metrics.observed_case_count}/${run.metrics.target_business_case_count}`}
          note="измерено / цель"
        />
        <MetricCard
          label="Живые кейсы"
          value={String(run.metrics.live_case_count)}
          note="требование релиза: не менее 10"
        />
        <MetricCard
          label="p50 / p95"
          value={`${formatLatency(run.metrics.p50_latency_ms)} · ${formatLatency(run.metrics.p95_latency_ms)}`}
          note={`максимум ${formatLatency(run.metrics.max_latency_ms)}`}
        />
        <MetricCard
          label="Сбои / таймауты"
          value={`${run.metrics.crash_count} / ${run.metrics.timeout_count}`}
          note="срез живых запусков"
        />
        <MetricCard
          label="Стоимость"
          value={formatMoney(run.metrics.provider_cost_usd)}
          note={`${run.metrics.provider_tokens.toLocaleString("ru-RU")} токенов`}
        />
      </section>

      <div className="evaluation-layout">
        <section className="content-card evaluation-cases">
          <div className="section-toolbar">
            <Tabs
              idBase="evaluation"
              label="Группы кейсов"
              options={[
                { id: "business", label: "Бизнес-кейсы", count: run.business_cases.length },
                { id: "chaos", label: "Проверки сбоев", count: run.chaos_cases.length },
              ]}
              value={tab}
              onChange={setTab}
            />
            <StatusBadge value={run.status} />
          </div>
          <TabPanel activeTab={tab} idBase="evaluation">
            {cases.length ? (
              <div className="evaluation-case-list">
                {cases.map((item) => (
                  <article key={item.case.case_id}>
                    <div>
                      <strong>{item.case.case_id}</strong>
                      <p>{presentCaseTitle(item.case.case_id, item.case.title)}</p>
                    </div>
                    <StatusBadge value={item.actual_status} subtle />
                    <ModeBadge mode={item.execution_mode} />
                    <span>{item.qa_score === null ? "Качество —" : `Качество ${item.qa_score}`}</span>
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState title="Группа пока пуста">
                Ни одного фактически сохранённого кейса этого типа в текущем срезе.
              </EmptyState>
            )}
          </TabPanel>
        </section>

        <aside className="evaluation-sidebar">
          <section className="content-card compact-card">
            <div className="card-title-row">
              <Gauge size={18} />
              <h2>Режимы</h2>
            </div>
            <div className="mode-count-list">
              {Object.entries(run.mode_counts).length ? (
                Object.entries(run.mode_counts).map(([mode, count]) => (
                  <div key={mode}>
                    <ModeBadge mode={mode as ExecutionMode} />
                    <strong>{count}</strong>
                  </div>
                ))
              ) : (
                <p className="muted">Нет выполненных режимов.</p>
              )}
            </div>
          </section>

          <section className="content-card compact-card">
            <div className="card-title-row">
              <UsersRound size={18} />
              <h2>Качественное ревью</h2>
            </div>
            <StatusBadge value={run.qualitative_review_status} />
            <p className="muted">
              Формы готовит система, но оценки и подписи остаются действием реальных рецензентов.
            </p>
          </section>

          <section className="content-card compact-card">
            <div className="card-title-row">
              <FileCheck2 size={18} />
              <h2>Отчёты</h2>
            </div>
            <div className="report-links">
              {run.report_links.map((link) => (
                <ActionLink href={link.href} key={link.href}>
                  {link.label} · {link.format.toUpperCase()}
                </ActionLink>
              ))}
            </div>
            <p className="tiny-note">
              <ExternalLink size={13} /> Контрольные суммы появятся только у зафиксированных артефактов.
            </p>
          </section>
        </aside>
      </div>

      <section className="measurement-legend" aria-label="Типы утверждений">
        <div>
          <strong>Измерено</strong>
          <span>Из сохранённых запусков и комплектов</span>
        </div>
        <div>
          <strong>Допущение</strong>
          <span>Лимит владельца, а не баланс провайдера</span>
        </div>
        <div>
          <strong>Гипотеза</strong>
          <span>Не считается результатом до проверки</span>
        </div>
      </section>
    </div>
  );
}
