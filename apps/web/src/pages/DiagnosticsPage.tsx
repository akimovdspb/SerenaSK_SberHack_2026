import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Boxes,
  CircleCheckBig,
  Database,
  KeyRound,
  Network,
  RefreshCw,
  ShieldCheck,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { useState } from "react";

import { apiGet, apiMutation } from "../api";
import {
  AdmissionBadge,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  HashValue,
  LoadingState,
  StatusBadge,
  Notice,
} from "../components";
import { formatDate } from "../format";
import type { DemoResetResult, Diagnostics, PublicConfig } from "../types";

const componentIcons = {
  app: Activity,
  database: Database,
  ouroboros_contract: Boxes,
  mcp: Network,
  skill: ShieldCheck,
  provider: KeyRound,
};

export function DiagnosticsPage() {
  const queryClient = useQueryClient();
  const [resetOpen, setResetOpen] = useState(false);
  const [resetResult, setResetResult] = useState<DemoResetResult | null>(null);
  const diagnostics = useQuery({
    queryKey: ["diagnostics"],
    queryFn: () => apiGet<Diagnostics>("/api/v1/diagnostics"),
    refetchInterval: 10_000,
  });
  const publicConfig = useQuery({
    queryKey: ["public-config"],
    queryFn: () => apiGet<PublicConfig>("/api/v1/config/public"),
    staleTime: 60_000,
  });
  const resetDemo = useMutation({
    mutationFn: () =>
      apiMutation<DemoResetResult>("/api/v1/admin/demo-reset", {
        confirmation: "СБРОСИТЬ ДЕМО",
      }),
    onSuccess: (result) => {
      setResetResult(result);
      setResetOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      void queryClient.invalidateQueries({ queryKey: ["diagnostics"] });
    },
  });

  if (diagnostics.isPending) return <LoadingState label="Проверяем публичный контракт исполнения" />;
  if (diagnostics.isError)
    return <ErrorState error={diagnostics.error} retry={() => void diagnostics.refetch()} />;

  const data = diagnostics.data;
  const degraded = data.components.filter((component) => component.status === "DEGRADED");
  const contourReady = data.admission_state === "CLOSED" && degraded.length === 0;
  return (
    <div className="page page-diagnostics">
      <section className="page-heading compact-heading">
        <div>
          <p className="eyebrow">Диагностика · только публичные данные</p>
          <h1>Граница исполнения</h1>
          <p className="page-lede">
            Только публичные сведения о готовности и хэши. Здесь нет ключей, внутренних настроек,
            текста системной инструкции или ответов провайдера.
          </p>
        </div>
        <button
          className="button button-secondary"
          onClick={() => void diagnostics.refetch()}
          type="button"
        >
          <RefreshCw size={17} /> Обновить
        </button>
      </section>

      <section
        aria-live="polite"
        className={`verdict-card ${contourReady ? "verdict-ready" : "verdict-blocked"}`}
      >
        {contourReady ? <CircleCheckBig size={22} /> : <TriangleAlert size={22} />}
        <div>
          <strong>
            {contourReady
              ? "Контур исполнения готов: контракт зафиксирован, компоненты в норме."
              : "Контур исполнения не готов."}
          </strong>
          <p>
            {contourReady
              ? "Полная готовность к демо дополнительно требует зафиксированных доказательств и свежей канарейки — их проверяет команда make demo-check."
              : degraded.length
                ? `Внимания требуют: ${degraded.map((component) => component.label).join(", ")}.`
                : "Контракт исполнения не подтверждён: генерация заблокирована до повторной подготовки контура."}
          </p>
        </div>
      </section>

      <section className="diagnostics-overview" aria-label="Готовность компонентов">
        {data.components.map((component) => {
          const Icon = componentIcons[component.component_id as keyof typeof componentIcons] ?? Activity;
          return (
            <article className="diagnostic-card" key={component.component_id}>
              <div className="diagnostic-icon">
                <Icon size={20} />
              </div>
              <div>
                <div className="diagnostic-title">
                  <h2>{component.label}</h2>
                  <StatusBadge value={component.status} subtle />
                </div>
                <p>{component.detail}</p>
              </div>
            </article>
          );
        })}
      </section>

      <div className="diagnostics-layout">
        <section className="content-card contract-card">
          <div className="section-heading-row">
            <div>
              <p className="eyebrow">Закреплённый контракт</p>
              <h2>Среда исполнения и инструкция</h2>
            </div>
            <AdmissionBadge state={data.admission_state} />
          </div>
          <dl className="detail-list">
            <div>
              <dt>Версия Ouroboros</dt>
              <dd>{data.runtime_tag ?? "Контракт недоступен"}</dd>
            </div>
            <div>
              <dt>Коммит</dt>
              <dd><HashValue value={data.runtime_commit} /></dd>
            </div>
            <div>
              <dt>Инструкция агента</dt>
              <dd><HashValue value={data.skill_hash} /></dd>
            </div>
            <div>
              <dt>Системная инструкция</dt>
              <dd><HashValue value={data.prompt_hash} /></dd>
            </div>
            <div>
              <dt>Состав инструментов</dt>
              <dd><HashValue value={data.tool_inventory_hash} /></dd>
            </div>
            <div>
              <dt>Контракт зафиксирован</dt>
              <dd>{formatDate(data.contract_generated_at)}</dd>
            </div>
          </dl>
        </section>

        <section className="content-card tools-card">
          <div className="section-heading-row">
            <div>
              <p className="eyebrow">Изоляция MCP</p>
              <h2>Разрешённые инструменты</h2>
            </div>
            <span className="count-pill">{data.discovered_tools.length}</span>
          </div>
          {data.discovered_tools.length ? (
            <ol className="tool-list">
              {data.discovered_tools.map((tool, index) => (
                <li key={tool}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <code>{tool}</code>
                </li>
              ))}
            </ol>
          ) : (
            <EmptyState title="Контракт инструментов не подтверждён">
              Допуск закрыт по безопасному принципу; интерфейс не может запустить обращение к провайдеру.
            </EmptyState>
          )}
        </section>

        <section className="content-card queue-card">
          <div className="section-heading-row">
            <div>
              <p className="eyebrow">Очередь · восстановление</p>
              <h2>Операционный статус</h2>
            </div>
            <StatusBadge value={data.queue_state} />
          </div>
          <div className="queue-count">
            <strong>{data.active_run_count}</strong>
            <span>активных запусков</span>
          </div>
          {data.latest_errors.length ? (
            <div className="error-list">
              {data.latest_errors.map((error) => (
                <article key={error.run_id}>
                  <div><code>{error.run_id}</code><span>{formatDate(error.created_at)}</span></div>
                  <StatusBadge value={error.status} subtle />
                  <p>{error.reason_code ?? "Причина не опубликована"}</p>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState title="Нет последних ошибок завершения">
              Восстановление не требуется. Состояние очереди взято из доменной базы данных.
            </EmptyState>
          )}
        </section>
      </div>

      {publicConfig.data?.demo_reset_enabled ? (
        <section className="content-card demo-admin-card">
          <div>
            <p className="eyebrow">Служебное действие · только демо</p>
            <h2>Начать показ с чистого состояния</h2>
            <p>
              Удаляются только кампании, версии, тестовые и человеческие решения и ZIP-экспорты этого стенда.
              Контракт исполнения, инструкция агента, ключ провайдера и доказательства не затрагиваются. При активном
              запуске сброс будет отклонён.
            </p>
          </div>
          <button className="button button-danger" onClick={() => setResetOpen(true)} type="button">
            <Trash2 size={17} /> Сбросить демо-состояние
          </button>
        </section>
      ) : null}
      {resetResult ? (
        <Notice tone="success" title="Демо-состояние очищено">
          Каталог B01–B15 сохранён; наблюдавшихся кейсов: {resetResult.observed_case_count};
          вызовов провайдера при сбросе: {resetResult.provider_calls}.
        </Notice>
      ) : null}
      {resetDemo.isError ? (
        <div className="inline-error" role="alert">Сброс не выполнен: {resetDemo.error.message}</div>
      ) : null}

      <p className="diagnostics-footnote">
        Снимок обновлён {formatDate(data.generated_at)} · <code>public_config_only=true</code>
      </p>
      <ConfirmDialog
        busy={resetDemo.isPending}
        confirmation="СБРОСИТЬ ДЕМО"
        confirmLabel="Очистить изменяемые данные"
        danger
        description="Действие необратимо для текущих демо-кампаний. Оно ничего не отправляет и не очищает память или доказательства Ouroboros."
        onCancel={() => setResetOpen(false)}
        onConfirm={() => resetDemo.mutate()}
        open={resetOpen}
        requireTyping
        title="Сбросить изменяемое демо-состояние?"
      />
    </div>
  );
}
