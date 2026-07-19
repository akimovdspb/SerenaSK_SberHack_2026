import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  BookOpenText,
  FlaskConical,
  Layers3,
  RefreshCw,
  SquarePen,
} from "lucide-react";
import { useState } from "react";

import { apiGet, apiMutation } from "../api";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  MetricCard,
  ModeBadge,
  StatusBadge,
  TabPanel,
  Tabs,
} from "../components";
import { formatLatency, formatMoney } from "../format";
import { presentCaseTitle } from "../presentation/labels";
import type {
  AuthoringCatalog,
  Campaign,
  Dashboard,
  DashboardCase,
  RecentCampaign,
} from "../types";

type MainTab = "campaigns" | "tests";
type CaseTab = "business" | "chaos";

export function DashboardPage({ navigate }: { navigate: (path: string) => void }) {
  const [mainTab, setMainTab] = useState<MainTab>("campaigns");
  const [caseTab, setCaseTab] = useState<CaseTab>("business");
  const queryClient = useQueryClient();
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => apiGet<Dashboard>("/api/v1/dashboard"),
  });
  const catalog = useQuery({
    queryKey: ["authoring-catalog"],
    queryFn: () => apiGet<AuthoringCatalog>("/api/v1/authoring/catalog"),
  });
  const recent = useQuery({
    queryKey: ["recent-campaigns"],
    queryFn: () => apiGet<RecentCampaign[]>("/api/v1/campaigns"),
  });
  const createTestCase = useMutation({
    mutationFn: (caseId: string) =>
      apiMutation<Campaign>("/api/v1/campaigns", { case_id: caseId }),
    onSuccess: (campaign) => {
      void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      navigate(`/campaigns/${campaign.campaign_id}`);
    },
  });

  if (dashboard.isPending || catalog.isPending || recent.isPending) {
    return <LoadingState label="Собираем кампании" />;
  }
  const error = dashboard.error ?? catalog.error ?? recent.error;
  if (error) {
    return (
      <ErrorState
        error={error}
        retry={() => {
          void dashboard.refetch();
          void catalog.refetch();
          void recent.refetch();
        }}
      />
    );
  }
  if (!dashboard.data || !catalog.data || !recent.data) {
    return <LoadingState label="Собираем кампании" />;
  }

  const data = dashboard.data;
  const authoringCatalog = catalog.data;
  const recentCampaigns = recent.data;
  const cases = caseTab === "business" ? data.business_cases : data.chaos_cases;

  return (
    <div className="page page-dashboard">
      <section className="page-heading">
        <div>
          <p className="eyebrow">Кампании · синтетический контур</p>
          <h1>Кампании и коммуникации</h1>
          <p className="page-lede">
            Создавайте брифы для своих синтетических продуктов и аудиторий. Тестовые B-сценарии
            доступны отдельно и не подменяют обычное создание кампании.
          </p>
        </div>
        <div className="heading-actions">
          <button
            className="button button-primary button-prominent"
            onClick={() => navigate("/campaigns/new")}
            type="button"
          >
            <SquarePen size={18} />
            Новая кампания
          </button>
        </div>
      </section>

      <Tabs
        idBase="dashboard"
        label="Раздел панели"
        options={[
          { id: "campaigns", label: "Кампании", count: recentCampaigns.length },
          { id: "tests", label: "Тестовые сценарии", count: data.business_cases.length },
        ]}
        value={mainTab}
        onChange={setMainTab}
      />

      <TabPanel activeTab={mainTab} idBase="dashboard">
        {mainTab === "campaigns" ? (
          <div className="dashboard-overview">
            <section className="content-card recent-campaigns-card">
              <div className="dashboard-section-heading">
                <div><h2>Последние кампании</h2><p>Созданные через обычный authoring-flow</p></div>
                <button className="icon-button" aria-label="Обновить кампании" onClick={() => void recent.refetch()} type="button"><RefreshCw size={17} /></button>
              </div>
              {recentCampaigns.length ? (
                <div className="recent-campaign-list">
                  {recentCampaigns.map((campaign) => (
                    <button className="recent-campaign-row" key={campaign.campaign_id} onClick={() => navigate(`/campaigns/${campaign.campaign_id}`)} type="button">
                      <div><strong>{campaign.name || "Кампания без названия"}</strong><span>{campaign.product_name || "Продукт уточняется"} · {campaign.channels.map((item) => item === "sms" ? "SMS" : "E-mail").join(" + ") || "каналы уточняются"}</span></div>
                      <StatusBadge value={campaign.state} />
                      <ArrowRight size={17} />
                    </button>
                  ))}
                </div>
              ) : (
                <EmptyState title="Пока нет пользовательских кампаний">
                  Создайте первый бриф — здесь появится его актуальное состояние.
                </EmptyState>
              )}
            </section>

            <aside className="content-card reference-card">
              <div className="dashboard-section-heading"><div><h2>Начать с примера</h2><p>Только редактируемый бриф, без копирования готового текста</p></div><BookOpenText size={19} /></div>
              <div className="reference-list">
                {authoringCatalog.references.map((reference) => (
                  <button key={reference.reference_id} onClick={() => navigate(`/campaigns/new?reference=${reference.reference_id}`)} type="button">
                    <strong>{reference.title}</strong><span>{reference.description}</span><small>Редакционный ориентир · не live evidence</small>
                  </button>
                ))}
              </div>
            </aside>
          </div>
        ) : (
          <section className="test-scenarios-section">
            <div className="test-section-intro">
              <div><h2>Тестовые сценарии B01–B15</h2><p>Изолированная проверочная корзина. Идентификаторы и технические метрики показаны только здесь.</p></div>
            </div>
            <section className="metrics-grid" aria-label="Метрики тестового среза">
              <MetricCard label="Каталог" value={`${data.metrics.catalog_case_count}/${data.metrics.target_business_case_count}`} note="фактические / целевые кейсы" />
              <MetricCard label="Наблюдались" value={String(data.metrics.observed_case_count)} note="сохранённые состояния" />
              <MetricCard label="Live · Ouroboros" value={String(data.metrics.live_case_count)} note="измеренные запуски" />
              <MetricCard label="p50 / p95" value={`${formatLatency(data.metrics.p50_latency_ms)} / ${formatLatency(data.metrics.p95_latency_ms)}`} note={`максимум ${formatLatency(data.metrics.max_latency_ms)}`} />
              <MetricCard label="Сбои / таймауты" value={`${data.metrics.crash_count} / ${data.metrics.timeout_count}`} note="только live" />
              <MetricCard label="Стоимость среза" value={formatMoney(data.metrics.provider_cost_usd)} note={`${data.metrics.provider_tokens.toLocaleString("ru-RU")} токенов`} />
            </section>
            <section className="content-card cases-card">
              <div className="section-toolbar">
                <Tabs idBase="cases" label="Тип тестовых сценариев" options={[{ id: "business", label: "B01–B15", count: data.business_cases.length }, { id: "chaos", label: "Проверки сбоев", count: data.chaos_cases.length }]} value={caseTab} onChange={setCaseTab} />
                <button className="icon-button" aria-label="Обновить тестовые сценарии" onClick={() => void dashboard.refetch()} type="button"><RefreshCw size={17} /></button>
              </div>
              <TabPanel activeTab={caseTab} idBase="cases">
                {cases.length ? (
                  <CaseTable cases={cases} busyCase={createTestCase.isPending ? createTestCase.variables : undefined} onOpen={(item) => item.campaign_id ? navigate(`/campaigns/${item.campaign_id}`) : createTestCase.mutate(item.case.case_id)} onCreate={(item) => createTestCase.mutate(item.case.case_id)} />
                ) : (
                  <EmptyState title="Нет зафиксированных проверок сбоев">Здесь показываются только фактические записи, без нарисованных результатов.</EmptyState>
                )}
              </TabPanel>
            </section>
          </section>
        )}
      </TabPanel>

      <section className="integrity-strip" aria-label="Границы данных">
        <div><FlaskConical size={18} /><span>Все данные синтетические</span></div>
        <div><Layers3 size={18} /><span>Факты отделены от цели и заметок</span></div>
        <div><ArrowRight size={18} /><span>Отправка во внешние системы отсутствует</span></div>
      </section>
    </div>
  );
}

function CaseTable({ cases, busyCase, onOpen, onCreate }: { cases: DashboardCase[]; busyCase?: string; onOpen: (item: DashboardCase) => void; onCreate: (item: DashboardCase) => void }) {
  return <div className="table-scroll"><table className="data-table cases-table"><thead><tr><th scope="col">Кейс</th><th scope="col">Ожидается</th><th scope="col">Фактически</th><th scope="col">Режим</th><th scope="col">Задержка</th><th scope="col">Качество</th><th scope="col" aria-label="Действие" /></tr></thead><tbody>{cases.map((item) => <tr key={item.case.case_id}><td><strong>{item.case.case_id}</strong><span className="table-secondary">{presentCaseTitle(item.case.case_id, item.case.title)}</span></td><td><StatusBadge value={item.case.expected_status} subtle /></td><td><StatusBadge value={item.actual_status} /></td><td><ModeBadge mode={item.execution_mode} /></td><td>{formatLatency(item.latency_ms)}</td><td>{item.qa_score === null ? "—" : <span className="score-value">{item.qa_score}<small>/100</small></span>}</td><td className="table-action"><div className="row-actions">{item.campaign_id ? <button className="button button-ghost button-small" onClick={() => onOpen(item)} type="button">Открыть</button> : null}<button className="button button-ghost button-small" disabled={busyCase === item.case.case_id} onClick={() => onCreate(item)} type="button">Новый прогон <ArrowRight size={15} /></button></div></td></tr>)}</tbody></table></div>;
}
