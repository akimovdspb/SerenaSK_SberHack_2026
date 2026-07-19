import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Ban,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  ClipboardCheck,
  Download,
  FileDiff,
  FileText,
  MessageSquareText,
  Network,
  Play,
  RotateCcw,
  Send,
  ShieldAlert,
  Sparkles,
  SquarePen,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { apiGet, apiMutation } from "../api";
import {
  ConfirmDialog,
  DisabledReason,
  EmptyState,
  ErrorState,
  HashValue,
  LoadingState,
  ModeBadge,
  Notice,
  StatusBadge,
  TabPanel,
  Tabs,
} from "../components";
import { formatDate, shortHash } from "../format";
import {
  presentChannel,
  presentDisabledReason,
  presentFactKind,
  presentOperation,
  presentRisk,
  presentRuleType,
  presentTestKind,
} from "../presentation/labels";
import type {
  Context,
  Feedback,
  OperationPresentation,
  PackageDiff,
  PackageView,
  PublicConfig,
  RuleProposal,
  RuleVersion,
  Workspace,
} from "../types";

type WorkspaceTab = "email" | "sms" | "facts" | "claims" | "qa" | "diff" | "rule";
type ConfirmAction = "package" | "rule" | "rollback" | null;
type Command = { kind: string; path: string; body?: unknown; tab?: WorkspaceTab };

const activeRunStates = new Set(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]);

export function WorkspacePage({
  campaignId,
  navigate,
  publicConfig,
}: {
  campaignId: string;
  navigate: (path: string) => void;
  publicConfig: PublicConfig | undefined;
}) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<WorkspaceTab>("email");
  const [ctaLabel, setCtaLabel] = useState("");
  const [ctaUrl, setCtaUrl] = useState("");
  const [feedbackPath, setFeedbackPath] = useState("/email/sections/0/body");
  const [feedbackComment, setFeedbackComment] = useState("");
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);
  const [dismissedOperationRunId, setDismissedOperationRunId] = useState<string | null>(null);
  const [streamEvents, setStreamEvents] = useState<string[]>([]);
  const [streamNotice, setStreamNotice] = useState<string | null>(null);
  const seenStreamEvents = useRef(new Set<string>());
  const commandLock = useRef(false);

  const workspace = useQuery({
    queryKey: ["workspace", campaignId],
    queryFn: () => apiGet<Workspace>(`/api/v1/campaigns/${campaignId}/workspace`),
    refetchInterval: (query) =>
      query.state.data?.operation_state?.active ? 1_000 : false,
  });
  const command = useMutation({
    mutationFn: ({ path, body }: Command) => apiMutation<unknown>(path, body),
    onSuccess: (_, variables) => {
      setConfirmAction(null);
      if (variables.tab) setTab(variables.tab);
      void queryClient.invalidateQueries({ queryKey: ["workspace", campaignId] });
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onSettled: () => {
      commandLock.current = false;
    },
  });

  const operationState = workspace.data?.operation_state ?? null;
  const activeOperation = operationState?.active ? operationState : null;
  const activeRun = workspace.data?.runs.find(
    (run) => run.run_id === activeOperation?.run_id && activeRunStates.has(run.status),
  );
  useEffect(() => {
    if (!activeRun) return undefined;
    seenStreamEvents.current.clear();
    const source = new EventSource(`/api/v1/runs/${activeRun.run_id}/events`);
    source.onopen = () => {
      setStreamEvents([]);
      setStreamNotice("Получаем нормализованные события в реальном времени");
    };
    const eventNames = [
      "run.accepted",
      "run.started",
      "run.stage",
      "run.task_bound",
      "run.tool_started",
      "run.tool_completed",
      "run.qa_completed",
      "run.retry_scheduled",
      "run.terminal",
      "package.version_created",
      "rule.proposed",
      "rule.tested",
    ];
    const handler = (event: Event) => {
      const message = event as MessageEvent<string>;
      try {
        const payload = JSON.parse(message.data) as { event_id?: number; type?: string };
        const eventType = payload.type ?? event.type;
        const eventKey = `${payload.event_id ?? message.lastEventId}:${eventType}:${message.data}`;
        if (seenStreamEvents.current.has(eventKey)) return;
        seenStreamEvents.current.add(eventKey);
        setStreamEvents((current) => [...current.slice(-11), eventType]);
        if (eventType === "run.retry_scheduled") {
          setStreamNotice("Временный сбой — повторяем запрос (2 из 2)");
        }
        if (eventType === "run.terminal") {
          setStreamNotice("Получен итоговый статус; обновляем кампанию");
        }
        void queryClient.invalidateQueries({ queryKey: ["workspace", campaignId] });
      } catch {
        setStreamNotice("Получено нераспознанное безопасное событие");
      }
    };
    eventNames.forEach((name) => source.addEventListener(name, handler));
    source.onerror = () => {
      source.close();
      setStreamNotice("Поток событий прерван; состояние можно восстановить обновлением страницы");
      void queryClient.invalidateQueries({ queryKey: ["workspace", campaignId] });
    };
    return () => {
      eventNames.forEach((name) => source.removeEventListener(name, handler));
      source.close();
    };
  }, [activeRun, campaignId, queryClient]);

  if (workspace.isPending) return <LoadingState label="Открываем рабочее пространство кампании" />;
  if (workspace.isError)
    return <ErrorState error={workspace.error} retry={() => void workspace.refetch()} />;

  const data = workspace.data;
  const latestFeedback = data.feedback.at(-1);
  const latestDiff = data.diffs.at(-1);
  const latestProposal = data.rule_proposals.at(-1);
  const latestRule = data.rule_versions.at(-1);
  const latestApproval = data.approvals.find(
    (item) => item.package_id === data.package?.package_id,
  );
  const latestExport = data.exports.find((item) => item.package_id === data.package?.package_id);
  const executionMode = publicConfig?.default_execution_mode;
  const testOnly = publicConfig?.human_actions_test_only ?? true;
  const operationBusy = Boolean(activeOperation);
  const retryActive = operationState?.attempt_number === 2 && operationState.active;
  const retryCompleted =
    operationState?.attempt_number === 2 &&
    ["COMPLETED", "COMPLETED_FALLBACK"].includes(operationState.status);
  const operationVisible =
    operationState !== null &&
    !new Set(["COMPLETED", "COMPLETED_FALLBACK"]).has(operationState.status);
  const operationTab =
    operationState?.operation === "revision"
      ? "diff"
      : operationState?.operation === "rule_proposal"
        ? "rule"
        : null;
  const selectedTab =
    operationTab &&
    (operationBusy || dismissedOperationRunId !== operationState?.run_id)
      ? operationTab
      : tab;
  const selectTab = (nextTab: WorkspaceTab) => {
    if (!operationBusy && operationState?.run_id) {
      setDismissedOperationRunId(operationState.run_id);
    }
    setTab(nextTab);
  };

  const runCommand = (next: Command) => {
    if (commandLock.current || command.isPending || (operationBusy && next.kind !== "cancel")) {
      return;
    }
    commandLock.current = true;
    command.mutate(next);
  };
  const submitAnswers = () =>
    runCommand({
      kind: "answers",
      path: `/api/v1/campaigns/${campaignId}/answers`,
      body: { cta_label: ctaLabel, cta_url: ctaUrl },
    });
  const submitFeedback = () => {
    if (!data.package) return;
    runCommand({
      kind: "feedback",
      path: `/api/v1/packages/${data.package.package_id}/feedback`,
      body: {
        artifact_path: feedbackPath,
        comment: feedbackComment,
        scope: "CURRENT_CHANNEL",
        author_role: "editor",
      },
      tab: "diff",
    });
  };
  const cancelOperation = () => {
    if (!activeOperation || activeOperation.status === "CANCEL_REQUESTED") return;
    runCommand({
      kind: "cancel",
      path: `/api/v1/runs/${activeOperation.run_id}/cancel`,
    });
  };

  const confirmation = confirmationDetails(
    confirmAction,
    data,
    latestProposal,
    latestRule,
    testOnly,
  );

  return (
    <div className="page page-workspace">
      <section className="workspace-heading">
        <div>
          <button className="back-link" onClick={() => navigate("/")} type="button">
            <ArrowLeft size={15} /> Все кейсы
          </button>
          <div className="workspace-title-block">
            <p className="eyebrow">Рабочее пространство кампании</p>
            <div className="workspace-title-row">
              <h1>{data.campaign.draft.name ?? "Новая коммуникация"}</h1>
              <StatusBadge value={data.campaign.state} />
            </div>
          </div>
        </div>
        <div className="workspace-identifiers">
          <HashValue value={data.campaign.context_version} label="контекст" />
          <HashValue value={data.package?.package_hash ?? null} label="комплект" />
          <ModeBadge
            detailed
            mode={activeOperation?.mode ?? data.package?.mode ?? activeRun?.mode ?? null}
          />
        </div>
      </section>

      {command.isError ? (
        <div className="inline-error" role="alert">
          Действие не выполнено: {command.error.message}
        </div>
      ) : null}

      {retryActive || retryCompleted ? (
        <div className="retry-status" role="status">
          {retryActive
            ? "Временный сбой — повторяем запрос (2 из 2)"
            : "Временный сбой устранён — запрос выполнен со второй попытки"}
        </div>
      ) : null}

      <div className={`workspace-grid${operationBusy ? " has-active-operation" : ""}`}>
        <aside className="workspace-left" aria-label="Бриф и контекст">
          <WorkflowStepper data={data} />
          <BriefPanel
            data={data}
            busy={command.isPending || operationBusy}
            ctaLabel={ctaLabel}
            ctaUrl={ctaUrl}
            setCtaLabel={setCtaLabel}
            setCtaUrl={setCtaUrl}
            onAnswers={submitAnswers}
            onGenerate={() =>
              executionMode && runCommand({
                kind: "generate",
                path: `/api/v1/campaigns/${campaignId}/runs`,
                body: { mode: executionMode },
              })
            }
            onValidate={() =>
              runCommand({
                kind: "validate",
                path: `/api/v1/campaigns/${campaignId}/validate`,
              })
            }
            executionMode={executionMode}
          />
          <ContextSummary context={data.context} />
        </aside>

        <section aria-label="Артефакты и решения" className="workspace-center">
          {data.package ? (
            <>
              <div className="artifact-toolbar">
                <div>
                  <span>Комплект v{data.package.package_version}</span>
                  <small>{formatDate(data.package.created_at)}</small>
                </div>
                <Tabs
                  idBase="artifact"
                  label="Артефакты кампании"
                  options={[
                    { id: "email", label: "Письмо", group: "Каналы" },
                    { id: "sms", label: "СМС", group: "Каналы" },
                    { id: "facts", label: "Факты", count: data.context?.facts.length, group: "Доказательства" },
                    { id: "claims", label: "Привязки", count: data.package.bundle.claim_evidence.length, group: "Доказательства" },
                    { id: "qa", label: "Качество", count: data.package.quality_report.findings.length, group: "Доказательства" },
                    { id: "diff", label: "Изменения", count: latestDiff?.changed_paths.length, group: "Доработка" },
                    { id: "rule", label: "Правило", count: latestProposal?.tests.length, group: "Доработка" },
                  ]}
                  value={selectedTab}
                  onChange={selectTab}
                />
              </div>
              <TabPanel activeTab={selectedTab} idBase="artifact">
                {operationVisible && operationTab === selectedTab ? (
                  <OperationStatusCard
                    busy={command.isPending}
                    operation={operationState}
                    onCancel={cancelOperation}
                  />
                ) : (
                  <ArtifactContent
                    tab={selectedTab}
                    data={data}
                    feedbackPath={feedbackPath}
                    feedbackComment={feedbackComment}
                    latestFeedback={latestFeedback}
                    latestDiff={latestDiff}
                    latestProposal={latestProposal}
                    latestRule={latestRule}
                    busy={command.isPending || operationBusy || !executionMode}
                    testOnly={testOnly}
                    setFeedbackPath={setFeedbackPath}
                    setFeedbackComment={setFeedbackComment}
                    submitFeedback={submitFeedback}
                    onRevision={() =>
                      latestFeedback && executionMode &&
                      runCommand({
                        kind: "revision",
                        path: `/api/v1/packages/${latestFeedback.package_id}/revision`,
                        body: {
                          feedback_id: latestFeedback.feedback_id,
                          mode: executionMode,
                        },
                        tab: "diff",
                      })
                    }
                    onProposal={() =>
                      latestFeedback && executionMode &&
                      data.context &&
                      runCommand({
                        kind: "proposal",
                        path: `/api/v1/feedback/${latestFeedback.feedback_id}/rule-proposals`,
                        body: {
                          selected_scope: {
                            product_ids: [data.context.product.product_id],
                            channel: "email",
                            segment_ids: [],
                          },
                          mode: executionMode,
                        },
                        tab: "rule",
                      })
                    }
                    onApproveRule={() => setConfirmAction("rule")}
                    onRollback={() => setConfirmAction("rollback")}
                    onCreateCampaign={() => navigate("/campaigns/new")}
                  />
                )}
              </TabPanel>
              <ApprovalBar
                data={data}
                approval={latestApproval}
                exportRecord={latestExport}
                busy={command.isPending || operationBusy}
                testOnly={testOnly}
                onApprove={() => setConfirmAction("package")}
                onExport={() =>
                  runCommand({
                    kind: "export",
                    path: `/api/v1/packages/${data.package?.package_id ?? ""}/export`,
                  })
                }
              />
            </>
          ) : (
            operationVisible ? (
              <OperationStatusCard
                busy={command.isPending}
                operation={operationState}
                onCancel={cancelOperation}
              />
            ) : (
              <NoPackageState data={data} />
            )
          )}
        </section>

        <aside className="workspace-right" aria-label="Безопасная трасса">
          <TracePanel
            data={data}
            streamEvents={streamEvents}
            streamNotice={streamNotice}
            activeRunId={activeRun?.run_id}
          />
        </aside>
      </div>

      {confirmation ? (
        <ConfirmDialog
          open={confirmAction !== null}
          title={confirmation.title}
          description={confirmation.description}
          confirmation={confirmation.value}
          confirmLabel={confirmation.label}
          danger={confirmAction === "rollback"}
          busy={command.isPending}
          onCancel={() => setConfirmAction(null)}
          onConfirm={() => runCommand(confirmation.command)}
        />
      ) : null}
    </div>
  );
}

function OperationStatusCard({
  operation,
  busy,
  onCancel,
}: {
  operation: OperationPresentation;
  busy: boolean;
  onCancel: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!operation.active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [operation.active, operation.run_id]);

  if (!operation.active) {
    const cancelled = operation.status === "CANCELLED";
    return (
      <section className="operation-status operation-terminal" role="alert">
        <div className="operation-terminal-icon" aria-hidden="true">
          {cancelled ? <Ban size={24} /> : <XCircle size={24} />}
        </div>
        <div className="operation-copy">
          <p className="eyebrow">Итог операции</p>
          <h2>{operation.stage_label}</h2>
          <p>
            {cancelled
              ? "Сохранённый результат не был объявлен готовым. Можно продолжить с последнего подтверждённого состояния."
              : failureDescription(operation.reason_code)}
          </p>
          {!cancelled ? (
            <strong>Безопасно запустите операцию ещё раз после проверки доступности контура.</strong>
          ) : null}
          <div className="operation-technical">
            <code>{operation.run_id}</code>
            {operation.reason_code ? <code>{operation.reason_code}</code> : null}
          </div>
        </div>
      </section>
    );
  }

  const elapsedSeconds = Math.max(
    0,
    Math.floor((now - new Date(operation.elapsed_from).getTime()) / 1_000),
  );
  const cancelling = operation.status === "CANCEL_REQUESTED";
  return (
    <section
      aria-atomic="true"
      aria-live="polite"
      className="operation-status"
      role="status"
    >
      <div className="operation-visual" aria-hidden="true">
        <span className="operation-spinner" />
        <span className="operation-dots">•••</span>
      </div>
      <div className="operation-copy">
        <p className="eyebrow">Выполняется одна управляемая операция</p>
        <h2>{operation.title}</h2>
        <p className="operation-stage">{operation.stage_label}</p>
        <div className="operation-metadata">
          <span>Попытка {operation.attempt_number} из 2</span>
          <span>Прошло {formatElapsed(elapsedSeconds)}</span>
        </div>
        <p className="operation-result-hint">{operation.result_hint}</p>
        <code>{operation.run_id}</code>
      </div>
      <button
        aria-label={`Отмена операции: ${operation.title}`}
        className="button button-secondary operation-cancel danger-text"
        disabled={busy || cancelling}
        onClick={onCancel}
        type="button"
      >
        <Ban size={16} /> {cancelling ? "Отмена запрошена" : "Отмена"}
      </button>
    </section>
  );
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${String(seconds % 60).padStart(2, "0")} с`;
}

function failureDescription(reasonCode: string | null): string {
  if (reasonCode === "CONTROLLED_RETRY_EXHAUSTED") {
    return "Повторный запрос не выполнен. Автоматических попыток больше не будет; готовый результат не сохранён. Можно запустить новый запрос вручную.";
  }
  if (reasonCode === "DRAFT_NOT_PERSISTED") {
    return "Контур не подтвердил сохранение результата, поэтому интерфейс не показывает успех.";
  }
  return "Готовый результат не сохранён. Технический код оставлен ниже для безопасной диагностики.";
}

function WorkflowStepper({ data }: { data: Workspace }) {
  const packageReady = Boolean(data.package);
  const feedbackReady = data.diffs.length > 0;
  const ruleReady = data.rule_versions.some((item) => item.active);
  const steps = [
    { label: "Бриф", done: data.campaign.state !== "DRAFT" },
    { label: "Комплект", done: packageReady },
    { label: "Ревизия", done: feedbackReady },
    { label: "Правило", done: ruleReady },
  ];
  return (
    <section className="side-section workflow-stepper">
      <p className="side-label">Этапы</p>
      <ol>
        {steps.map((step, index) => (
          <li className={step.done ? "is-done" : ""} key={step.label}>
            <span>{step.done ? <Check size={13} /> : index + 1}</span>
            <strong>{step.label}</strong>
          </li>
        ))}
      </ol>
    </section>
  );
}

function BriefPanel({
  data,
  busy,
  ctaLabel,
  ctaUrl,
  setCtaLabel,
  setCtaUrl,
  onAnswers,
  onGenerate,
  onValidate,
  executionMode,
}: {
  data: Workspace;
  busy: boolean;
  ctaLabel: string;
  ctaUrl: string;
  setCtaLabel: (value: string) => void;
  setCtaUrl: (value: string) => void;
  onAnswers: () => void;
  onGenerate: () => void;
  onValidate: () => void;
  executionMode: "deterministic_template" | "live_ouroboros" | undefined;
}) {
  const campaign = data.campaign;
  const answersIncomplete = !ctaLabel || !ctaUrl;
  const latestOperation = data.operation_state?.operation ?? data.runs.at(-1)?.operation;
  return (
    <section className="side-section brief-panel">
      <div className="side-heading">
        <p className="side-label">Бриф · v{campaign.draft_version}</p>
        <HashValue value={campaign.draft.input_hash} />
      </div>
      <dl>
        <div><dt>Продукт</dt><dd>{campaign.draft.product_id ?? "—"}</dd></div>
        <div><dt>Сегмент</dt><dd>{campaign.draft.segment_id ?? "—"}</dd></div>
        <div><dt>Каналы</dt><dd>{campaign.draft.channels.join(" + ") || "—"}</dd></div>
        <div><dt>Тон</dt><dd>{campaign.draft.tone ?? "—"}</dd></div>
      </dl>

      {campaign.validation?.questions.length ? (
        <div className="question-block">
          <div className="question-title">
            <CircleDot size={15} />
            <strong>Нужны ответы · вызовов модели: 0</strong>
          </div>
          {campaign.validation.questions.map((question) => (
            <p key={question.question_id}>{question.message}</p>
          ))}
          <label className="field">
            <span>Подпись ссылки</span>
            <input
              value={ctaLabel}
              onChange={(event) => setCtaLabel(event.target.value)}
              placeholder="Собрать первый реестр"
            />
          </label>
          <label className="field">
            <span>Разрешённый URL</span>
            <input
              value={ctaUrl}
              onChange={(event) => setCtaUrl(event.target.value)}
              placeholder="https://pulse-pay.example.test/start"
              type="url"
            />
          </label>
          <button
            aria-describedby={answersIncomplete ? "answers-reason" : undefined}
            className="button button-primary button-block"
            disabled={busy || answersIncomplete}
            onClick={onAnswers}
            type="button"
          >
            Сохранить ответы <ArrowRight size={16} />
          </button>
          {answersIncomplete ? (
            <DisabledReason id="answers-reason">
              Кнопка станет доступна после заполнения подписи и разрешённого URL.
            </DisabledReason>
          ) : null}
        </div>
      ) : null}

      {campaign.state === "DRAFT" ? (
        <button className="button button-primary button-block" disabled={busy} onClick={onValidate} type="button">
          <ClipboardCheck size={17} /> Проверить бриф
        </button>
      ) : null}
      {(campaign.state === "READY" ||
        (["FAILED", "CANCELLED"].includes(campaign.state) &&
          latestOperation === "initial")) &&
      !data.package ? (
        <>
          <button
            aria-describedby={!executionMode ? "execution-mode-reason" : undefined}
            className="button button-primary button-block"
            disabled={busy || !executionMode}
            onClick={onGenerate}
            type="button"
          >
            <Play size={17} fill="currentColor" /> {executionMode === "live_ouroboros" ? "Создать через Ouroboros" : "Создать шаблонный комплект"}
          </button>
          {!executionMode ? (
            <DisabledReason id="execution-mode-reason">
              Загружаем безопасный профиль исполнения.
            </DisabledReason>
          ) : executionMode === "live_ouroboros" ? (
            <p className="execution-note">Живая генерация · операция ограничена по времени · итоговый успех или управляемый отказ будут показаны явно.</p>
          ) : null}
        </>
      ) : null}
      {campaign.validation?.blockers.length ? (
        <div className="blocker-list" role="alert">
          {campaign.validation.blockers.map((blocker) => (
            <span key={blocker}><XCircle size={14} /> {blocker}</span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function ContextSummary({ context }: { context: Context | null }) {
  if (!context) return null;
  return (
    <section className="side-section context-summary">
      <div className="side-heading">
        <p className="side-label">Контекст</p>
        <span className="source-chip" title="untrusted_data">Непроверенные данные</span>
      </div>
      <dl>
        <div><dt>Операция</dt><dd title={context.operation}>{presentOperation(context.operation).label}</dd></div>
        <div><dt>Выбрано фактов</dt><dd>{context.content_plan.selected_fact_ids.length}</dd></div>
        <div><dt>Концепты</dt><dd>{context.content_plan.selected_concept_ids.length}</dd></div>
        <div><dt>Правила</dt><dd>{context.content_plan.applied_rule_version_ids.length}</dd></div>
      </dl>
      {context.content_plan.applied_rule_version_ids.length ? (
        <div className="applied-rules" aria-label="Применённые версии правил">
          {context.content_plan.applied_rule_version_ids.map((ruleId) => (
            <code key={ruleId}>{ruleId}</code>
          ))}
        </div>
      ) : null}
      <details>
        <summary>Источники <ChevronDown size={14} /></summary>
        <ul className="source-list">
          {context.source_manifest.map((source) => (
            <li key={source.source_id}>
              <code>{source.source_id}</code><span>v{source.version}</span>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

function ArtifactContent({
  tab,
  data,
  feedbackPath,
  feedbackComment,
  latestFeedback,
  latestDiff,
  latestProposal,
  latestRule,
  busy,
  setFeedbackPath,
  setFeedbackComment,
  submitFeedback,
  onRevision,
  onProposal,
  onApproveRule,
  onRollback,
  onCreateCampaign,
  testOnly,
}: {
  tab: WorkspaceTab;
  data: Workspace;
  feedbackPath: string;
  feedbackComment: string;
  latestFeedback: Feedback | undefined;
  latestDiff: PackageDiff | undefined;
  latestProposal: RuleProposal | undefined;
  latestRule: RuleVersion | undefined;
  busy: boolean;
  setFeedbackPath: (value: string) => void;
  setFeedbackComment: (value: string) => void;
  submitFeedback: () => void;
  onRevision: () => void;
  onProposal: () => void;
  onApproveRule: () => void;
  onRollback: () => void;
  onCreateCampaign: () => void;
  testOnly: boolean;
}) {
  const packageView = data.package;
  if (!packageView) return null;
  if (tab === "email") return <EmailPreview packageView={packageView} />;
  if (tab === "sms") return <SmsPreview packageView={packageView} />;
  if (tab === "facts") return <FactsPanel context={data.context} />;
  if (tab === "claims") return <ClaimsPanel packageView={packageView} context={data.context} />;
  if (tab === "qa") return <QaPanel packageView={packageView} />;
  if (tab === "diff")
    return (
      <DiffPanel
        feedbackPath={feedbackPath}
        feedbackComment={feedbackComment}
        feedback={latestFeedback}
        diff={latestDiff}
        busy={busy}
        setFeedbackPath={setFeedbackPath}
        setFeedbackComment={setFeedbackComment}
        submitFeedback={submitFeedback}
        onRevision={onRevision}
        onProposal={onProposal}
      />
    );
  return (
    <RulePanel
      proposal={latestProposal}
      rule={latestRule}
      hasDiff={Boolean(latestDiff)}
      busy={busy}
      onApprove={onApproveRule}
      onRollback={onRollback}
      onCreateCampaign={onCreateCampaign}
      productId={data.context?.product.product_id}
      productName={data.context?.product.exact_name}
      valueLabel={
        data.context?.concepts.find(
          (concept) => concept.concept_id === latestProposal?.proposal.value,
        )?.accepted_surface_forms[0]
      }
      testOnly={testOnly}
    />
  );
}

function EmailPreview({ packageView }: { packageView: PackageView }) {
  const email = packageView.bundle.email;
  const suppression = packageView.bundle.channel_suppressions.find((item) => item.channel === "email");
  if (!email)
    return <SuppressedChannel channel="Письмо" reason={suppression?.reason ?? "Канал подавлен политикой"} />;
  return (
    <section className="artifact-panel email-artifact" aria-label="Безопасный предпросмотр письма">
      <div className="preview-meta">
        <div><span>Тема</span><strong>{email.subject}</strong></div>
        <div><span>Прехедер</span><p>{email.preheader}</p></div>
      </div>
      <div className="email-preview-frame">
        <div className="email-preview-top"><span>Безопасный предпросмотр · структура санитизирована</span></div>
        <article>
          <h2>{email.headline}</h2>
          {email.sections.map((section) => (
            <section key={section.section_id}>
              {section.heading ? <h3>{section.heading}</h3> : null}
              <p>{section.body}</p>
            </section>
          ))}
          <a className="email-cta" href={email.cta_url} onClick={(event) => event.preventDefault()}>
            {email.cta_label}
          </a>
          <small>Предпросмотр не выполняет переход и ничего не отправляет.</small>
        </article>
      </div>
    </section>
  );
}

function SmsPreview({ packageView }: { packageView: PackageView }) {
  const sms = packageView.bundle.sms;
  const metrics = packageView.quality_report.sms_metrics;
  const suppression = packageView.bundle.channel_suppressions.find((item) => item.channel === "sms");
  if (!sms)
    return <SuppressedChannel channel="СМС" reason={suppression?.reason ?? "Канал подавлен политикой"} />;
  return (
    <section className="artifact-panel sms-artifact">
      <div className="phone-preview">
        <div className="phone-speaker" />
        <div className="sms-bubble">{sms.text}</div>
        <span>Синтетический предпросмотр · отправка отключена</span>
      </div>
      <div className="sms-details">
        <p className="eyebrow">Метрики сообщения</p>
        <dl className="detail-list">
          <div><dt>Кодировка</dt><dd>{metrics?.encoding ?? "—"}</dd></div>
          <div><dt>Символы</dt><dd>{metrics?.characters ?? "—"}</dd></div>
          <div><dt>Единицы кодирования</dt><dd>{metrics?.code_units ?? "—"}</dd></div>
          <div><dt>Сегменты</dt><dd><strong>{metrics?.segments ?? "—"}</strong></dd></div>
          <div><dt>Домен ссылки</dt><dd>{new URL(sms.cta_url).host}</dd></div>
        </dl>
      </div>
    </section>
  );
}

function SuppressedChannel({ channel, reason }: { channel: string; reason: string }) {
  return (
    <div className="suppression-state" role="status">
      <Ban size={28} />
      <h2>{channel} подавлен</h2>
      <p>{reason}</p>
      <StatusBadge value="BLOCKED" subtle />
    </div>
  );
}

function FactsPanel({ context }: { context: Context | null }) {
  if (!context) return <EmptyState title="Контекст недоступен">Факты ещё не собирались.</EmptyState>;
  return (
    <section className="artifact-panel facts-panel">
      <div className="panel-heading"><div><p className="eyebrow">Факт-карточка</p><h2>{context.product.exact_name}</h2></div><HashValue value={context.context_version} /></div>
      <div className="fact-list">
        {context.facts.map((fact) => (
          <article className={context.content_plan.selected_fact_ids.includes(fact.fact_id) ? "is-selected" : ""} key={fact.fact_id}>
            <div><code>{fact.fact_id}</code><span title={fact.kind}>{presentFactKind(fact.kind).label}</span></div>
            <p>{fact.canonical_text}</p>
            <small>Источник: {fact.source_id}</small>
          </article>
        ))}
      </div>
    </section>
  );
}

function ClaimsPanel({ packageView, context }: { packageView: PackageView; context: Context | null }) {
  const facts = new Map(context?.facts.map((fact) => [fact.fact_id, fact]));
  return (
    <section className="artifact-panel claims-panel">
      <div className="panel-heading"><div><p className="eyebrow">Привязка к фактам</p><h2>Утверждение → факт → источник</h2></div><span className="count-pill">{packageView.bundle.claim_evidence.length}</span></div>
      <div className="table-scroll">
        <table className="data-table claims-table">
          <thead><tr><th>Фрагмент</th><th>Путь артефакта</th><th>Факт</th><th>Источник</th></tr></thead>
          <tbody>
            {packageView.bundle.claim_evidence.map((claim) => (
              <tr key={claim.claim_id}>
                <td><strong>«{claim.text_fragment}»</strong><span className="table-secondary" title={claim.claim_type}>{presentFactKind(claim.claim_type).label}</span></td>
                <td><code>{claim.artifact_path}</code></td>
                <td><code>{claim.fact_id}</code><span className="table-secondary">{facts.get(claim.fact_id)?.canonical_text}</span></td>
                <td><span className="source-chip">{claim.source_id}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function QaPanel({ packageView }: { packageView: PackageView }) {
  const report = packageView.quality_report;
  return (
    <section className="artifact-panel qa-panel">
      <div className="qa-score-card">
        <div><span>Детерминированная проверка</span><strong>{report.deterministic_score}<small>/100</small></strong></div>
        <StatusBadge value={report.approvable ? "APPROVABLE" : "BLOCKED"} />
        <p>проверок: {report.checked_ids.length} · замечаний: {report.findings.length} · вызовов модели: 0</p>
      </div>
      {report.findings.length ? (
        <div className="finding-list">
          {report.findings.map((finding) => (
            <article className={`finding finding-${finding.severity.toLowerCase()}`} key={finding.finding_id}>
              <div><StatusBadge value={finding.severity} subtle /><code>{finding.check_id}</code></div>
              <h3>{finding.recommendation}</h3>
              <p>{finding.path ?? finding.artifact}</p>
              {finding.actual ? <small>Фактически: {finding.actual}</small> : null}
            </article>
          ))}
        </div>
      ) : (
        <EmptyState title="Открытых замечаний нет">Проверены соответствие фактам, политики, ссылки, точные имена, персональные данные и границы отображения.</EmptyState>
      )}
    </section>
  );
}

function DiffPanel({
  feedbackPath,
  feedbackComment,
  feedback,
  diff,
  busy,
  setFeedbackPath,
  setFeedbackComment,
  submitFeedback,
  onRevision,
  onProposal,
}: {
  feedbackPath: string;
  feedbackComment: string;
  feedback: Feedback | undefined;
  diff: PackageDiff | undefined;
  busy: boolean;
  setFeedbackPath: (value: string) => void;
  setFeedbackComment: (value: string) => void;
  submitFeedback: () => void;
  onRevision: () => void;
  onProposal: () => void;
}) {
  if (!feedback) {
    const commentTooShort = feedbackComment.length < 5;
    return (
      <section className="artifact-panel feedback-panel">
        <div className="panel-heading"><div><p className="eyebrow">Точечное замечание</p><h2>Изменить ровно разрешённую область</h2></div><SquarePen size={22} /></div>
        <label className="field"><span>Путь артефакта</span><select value={feedbackPath} onChange={(event) => setFeedbackPath(event.target.value)}><option value="/email/sections/0/body">Письмо · первый текстовый блок</option><option value="/email/plain_text">Письмо · текстовая версия</option><option value="/email/subject">Письмо · тема</option></select></label>
        <label className="field"><span>Комментарий редактора</span><textarea value={feedbackComment} onChange={(event) => setFeedbackComment(event.target.value)} placeholder="Опишите, что изменить в тексте" rows={4} /></label>
        <Notice title="Замечание не запускает правило">Сначала сохраняется замечание, затем отдельная ревизия. Предложение правила — ещё одно явное действие.</Notice>
        <button
          aria-describedby={commentTooShort ? "feedback-reason" : undefined}
          className="button button-primary"
          disabled={busy || commentTooShort}
          onClick={submitFeedback}
          type="button"
        >
          <MessageSquareText size={17} /> Сохранить замечание
        </button>
        {commentTooShort ? (
          <DisabledReason id="feedback-reason">
            Кнопка станет доступна после комментария длиной от пяти символов.
          </DisabledReason>
        ) : null}
      </section>
    );
  }
  if (!diff) {
    return (
      <section className="artifact-panel feedback-panel">
        <div className="saved-feedback"><CheckCircle2 size={20} /><div><strong>Замечание сохранено</strong><p>{feedback.comment}</p><code>{feedback.artifact_path}</code></div></div>
        <Notice tone="warning" title="Текущий комплект больше нельзя утверждать">Доработка создаст новую версию; старый артефакт останется неизменяемым.</Notice>
        <button className="button button-primary" disabled={busy} onClick={onRevision} type="button"><FileDiff size={17} /> Создать точечную версию 2</button>
      </section>
    );
  }
  return (
    <section className="artifact-panel diff-panel">
      <div className="panel-heading"><div><p className="eyebrow">Неизменяемое сравнение</p><h2>v1 → v2</h2></div><StatusBadge value="COMPLETED" /></div>
      <div className="diff-summary"><span>{diff.changed_paths.length} изменено</span><span>{diff.protected_paths.length} защищено</span><span>Полная проверка качества повторена</span></div>
      <div className="diff-list">
        {diff.changes.map((change) => (
          <article key={change.path}>
            <code>{change.path}</code>
            <div className="diff-columns"><div><span>До</span><p>{change.before_preview}</p><HashValue value={change.before_hash} /></div><ArrowRight size={18} /><div><span>После</span><p>{change.after_preview}</p><HashValue value={change.after_hash} /></div></div>
          </article>
        ))}
      </div>
      <details className="protected-paths"><summary><ShieldAlert size={15} /> Защищённые пути ({diff.protected_paths.length})</summary><div>{diff.protected_paths.map((path) => <code key={path}>{path}</code>)}</div></details>
      <button className="button button-secondary" disabled={busy} onClick={onProposal} type="button"><Sparkles size={17} /> Предложить правило</button>
    </section>
  );
}

function RulePanel({
  proposal,
  rule,
  hasDiff,
  busy,
  onApprove,
  onRollback,
  onCreateCampaign,
  productId,
  productName,
  valueLabel,
  testOnly,
}: {
  proposal: RuleProposal | undefined;
  rule: RuleVersion | undefined;
  hasDiff: boolean;
  busy: boolean;
  onApprove: () => void;
  onRollback: () => void;
  onCreateCampaign: () => void;
  productId: string | undefined;
  productName: string | undefined;
  valueLabel: string | undefined;
  testOnly: boolean;
}) {
  if (!hasDiff)
    return <EmptyState title="Сначала нужна сохранённая доработка">Правило нельзя предложить только из комментария или намерения интерфейса.</EmptyState>;
  if (!proposal)
    return <div className="artifact-panel rule-empty"><Sparkles size={28} /><h2>Проект правила ещё не запущен</h2><p>Единственное действие «Предложить правило» находится во вкладке «Изменения». После запуска прогресс автоматически появится здесь; одобрение останется отдельным решением человека.</p></div>;
  const awaitingDecision = proposal.status === "READY_FOR_APPROVAL";
  return (
    <section className="artifact-panel rule-panel">
      <div className="panel-heading"><div><p className="eyebrow">Управляемое правило</p><h2 title={proposal.proposal.type}>{presentRuleType(proposal.proposal.type).label}</h2></div><StatusBadge value={rule?.status ?? proposal.status} /></div>
      <div className="rule-definition"><div><span>Значение</span><strong>{valueLabel ?? proposal.proposal.value}</strong>{valueLabel ? <code>{proposal.proposal.value}</code> : null}</div><div><span>Область</span><strong>{productName ?? "Выбранный продукт"}</strong><code>{productId ?? proposal.proposal.scope.product_ids.join(", ")}</code><small title={proposal.proposal.scope.channel ?? undefined}>Канал: {presentChannel(proposal.proposal.scope.channel).label}</small></div><div><span>Риск</span><strong title={proposal.proposal.risk}>{presentRisk(proposal.proposal.risk).label}</strong></div></div>
      <p className="rule-rationale">{proposal.proposal.rationale}</p>
      <div className="rule-tests">
        {proposal.tests.map((test) => (
          <article key={`${test.test_kind}-${test.case_id}`}><span className={`test-dot ${test.passed ? "passed" : "failed"}`}>{test.passed ? <Check size={13} /> : <XCircle size={13} />}</span><div><strong>{test.case_id}</strong><p>{test.detail}</p></div><span title={test.test_kind}>{presentTestKind(test.test_kind).label}</span></article>
        ))}
      </div>
      {!rule ? (
        <>
          <button
            aria-describedby={awaitingDecision ? undefined : "rule-approve-reason"}
            className="button button-primary"
            disabled={busy || !awaitingDecision}
            onClick={onApprove}
            type="button"
          >
            <CheckCircle2 size={17} /> {testOnly ? "Утвердить правило в тестовом режиме" : "Утвердить правило"}
          </button>
          {!awaitingDecision ? (
            <DisabledReason id="rule-approve-reason">
              Решение недоступно: предложение ещё не в состоянии «Готово к решению».
            </DisabledReason>
          ) : null}
        </>
      ) : null}
      {rule?.active ? <div className="rule-actions"><Notice tone="success" title={testOnly ? "Правило активно в тестовом режиме" : "Правило утверждено человеком и активно"}>Следующая подходящая кампания получит точный идентификатор версии правила.</Notice><button className="button button-primary" disabled={busy} onClick={onCreateCampaign} type="button">Создать новую кампанию <ArrowRight size={16} /></button><button className="button button-ghost danger-text" disabled={busy} onClick={onRollback} type="button"><RotateCcw size={15} /> Откатить</button></div> : null}
      {rule?.status === "ROLLED_BACK" ? <Notice tone="warning" title="Правило откачено">Историческая версия сохранена; будущие контексты её не применяют.</Notice> : null}
    </section>
  );
}

function ApprovalBar({
  data,
  approval,
  exportRecord,
  busy,
  onApprove,
  onExport,
  testOnly,
}: {
  data: Workspace;
  approval: Workspace["approvals"][number] | undefined;
  exportRecord: Workspace["exports"][number] | undefined;
  busy: boolean;
  onApprove: () => void;
  onExport: () => void;
  testOnly: boolean;
}) {
  const approveBlocked = !approval && !data.approval_eligible;
  const exportBlocked = !exportRecord && !data.export_eligible;
  const approvalReason = presentDisabledReason(data.approval_disabled_reason);
  const exportReason = presentDisabledReason(data.export_disabled_reason);
  return (
    <section className="approval-bar">
      <div className="no-send-lock"><Send size={18} /><div><strong>Утверждение не означает отправку</strong><span>В основном контуре нет подключений к сервисам отправки СМС, писем или CRM.</span></div></div>
      <div className="approval-actions">
        <div className="approval-actions-row">
          {approval ? (
            <span className="approval-record"><CheckCircle2 size={16} /> {approval.test_only ? "тестовое решение" : "решение человека"} · {shortHash(approval.approval_hash)}</span>
          ) : (
            <button
              aria-describedby={approveBlocked ? "approve-reason" : undefined}
              className="button button-primary"
              disabled={busy || !data.approval_eligible}
              onClick={onApprove}
              type="button"
            >
              {testOnly ? "Утвердить комплект в тестовом режиме" : "Утвердить комплект"}
            </button>
          )}
          {exportRecord ? (
            <a className="button button-secondary" href={`/api/v1/exports/${exportRecord.export_id}/download`}>
              <Download size={16} /> Скачать ZIP
            </a>
          ) : (
            <button
              aria-describedby={exportBlocked ? "export-reason" : undefined}
              className="button button-secondary"
              disabled={busy || !data.export_eligible}
              onClick={onExport}
              type="button"
            >
              <Download size={16} /> Экспорт
            </button>
          )}
        </div>
        {approveBlocked ? (
          <DisabledReason id="approve-reason">
            {approvalReason.label}
            {approvalReason.raw ? <code>{approvalReason.raw}</code> : null}
          </DisabledReason>
        ) : null}
        {exportBlocked ? (
          <DisabledReason id="export-reason">
            {exportReason.label}
            {exportReason.raw ? <code>{exportReason.raw}</code> : null}
          </DisabledReason>
        ) : null}
      </div>
    </section>
  );
}

function NoPackageState({ data }: { data: Workspace }) {
  const blockers = data.campaign.validation?.blockers ?? [];
  if (data.campaign.state === "BLOCKED" || data.campaign.state === "NOT_APPLICABLE") {
    return <section className="no-package blocked-package"><ShieldAlert size={35} /><StatusBadge value={data.campaign.state} /><h2>Генерация не запущена</h2><p>Детерминированная проверка остановила процесс до любого вызова модели или провайдера.</p>{blockers.map((blocker) => <code key={blocker}>{blocker}</code>)}<button aria-describedby="blocked-approve-reason" className="button button-secondary" disabled type="button">Утверждение недоступно</button><DisabledReason id="blocked-approve-reason">Кейс завершён управляемым отказом: утверждать нечего.</DisabledReason></section>;
  }
  return <section className="no-package"><FileText size={35} /><h2>Артефакта ещё нет</h2><p>{data.campaign.state === "NEEDS_INPUT" ? "Ответьте на вопросы в брифе. Генерация отключена, вызовов модели: 0." : "Завершите текущий этап слева. Результат появится только после сохранённой операции."}</p></section>;
}

function TracePanel({
  data,
  streamEvents,
  streamNotice,
  activeRunId,
}: {
  data: Workspace;
  streamEvents: string[];
  streamNotice: string | null;
  activeRunId: string | undefined;
}) {
  const events = useMemo(() => [...data.safe_trace].reverse(), [data.safe_trace]);
  return (
    <section className="trace-panel">
      <div className="trace-heading"><div><p className="side-label">Безопасная трасса</p><span>событий: {events.length}</span></div><Network size={18} /></div>
      {activeRunId ? <div className="live-stream"><span className="live-dot" /><div><strong>Поток событий подключён</strong><code>{activeRunId}</code><p>{streamNotice}</p></div></div> : null}
      {streamEvents.length ? <div className="stream-events">{streamEvents.map((event, index) => <code key={`${event}-${index}`}>{event}</code>)}</div> : null}
      <ol className="trace-list">
        {events.map((event) => (
          <li key={event.event_id}><span className="trace-dot" /><div><strong>{event.label}</strong><code>{event.event_type}</code><small>{formatDate(event.created_at)}</small></div>{event.mode ? <ModeBadge mode={event.mode} /> : null}</li>
        ))}
      </ol>
      <div className="trace-boundary"><ShieldAlert size={16} /><p>Исходная системная инструкция, внутренние рассуждения, аргументы инструментов и ответы провайдера не сохраняются в этой трассе.</p></div>
    </section>
  );
}

function confirmationDetails(
  action: ConfirmAction,
  data: Workspace,
  proposal: RuleProposal | undefined,
  rule: RuleVersion | undefined,
  testOnly: boolean,
): { title: string; description: string; value: string; label: string; command: Command } | null {
  if (action === "package" && data.package) {
    return {
      title: `Утвердить комплект v${data.package.package_version}?`,
      description: testOnly
        ? "Будет сохранено тестовое решение. Оно не является финальным утверждением для сдачи и не отправляет коммуникацию."
        : "Решение будет сохранено от имени текущей сессии пользователя. Утверждение не отправляет коммуникацию и само по себе не является финальным подтверждением жюри.",
      value: data.package.package_hash,
      label: "Подтвердить точный хэш",
      command: {
        kind: "approve-package",
        path: `/api/v1/packages/${data.package.package_id}/approve`,
        body: {
          package_hash: data.package.package_hash,
          decision: "APPROVED",
          acknowledged_warning_ids: [],
          test_only: testOnly,
        },
      },
    };
  }
  if (action === "rule" && proposal) {
    return {
      title: "Активировать ограниченное правило?",
      description: testOnly
        ? "Решение создаст неизменяемую версию правила от имени тестового участника. Финальное утверждение для сдачи остаётся за человеком."
        : "Решение создаст неизменяемую версию правила от имени текущей сессии пользователя. Оно влияет только на будущие подходящие контексты.",
      value: proposal.proposal.candidate_rules_version,
      label: testOnly ? "Утвердить правило в тестовом режиме" : "Утвердить правило",
      command: {
        kind: "approve-rule",
        path: `/api/v1/rule-proposals/${proposal.proposal_id}/approve`,
        body: {
          candidate_rules_version: proposal.proposal.candidate_rules_version,
          test_only: testOnly,
        },
        tab: "rule",
      },
    };
  }
  if (action === "rollback" && rule) {
    return {
      title: "Откатить правило для будущих контекстов?",
      description: "Исторические артефакты останутся неизменными; активный набор перестанет включать эту версию.",
      value: rule.rules_version,
      label: "Выполнить откат",
      command: {
        kind: "rollback-rule",
        path: `/api/v1/rules/${rule.rule_version_id}/rollback`,
        body: {
          active_rules_version: rule.rules_version,
          reason: testOnly ? "Явный откат из тестовой кампании." : "Явный откат из пользовательской кампании.",
          test_only: testOnly,
        },
        tab: "rule",
      },
    };
  }
  return null;
}
