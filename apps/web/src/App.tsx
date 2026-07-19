import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  BadgeCheck,
  Factory,
  FlaskConical,
  LayoutDashboard,
  LockKeyhole,
  LogOut,
} from "lucide-react";
import { useEffect, useState } from "react";

import { apiGet, authLogout } from "./api";
import { DashboardPage } from "./pages/DashboardPage";
import { CampaignWizardPage } from "./pages/CampaignWizardPage";
import { DiagnosticsPage } from "./pages/DiagnosticsPage";
import { EvaluationPage } from "./pages/EvaluationPage";
import { MvpResultsPage } from "./pages/MvpResultsPage";
import { LoginPage } from "./pages/LoginPage";
import { WorkspacePage } from "./pages/WorkspacePage";
import type { Health, PublicConfig } from "./types";

function rememberedWorkspace(): string | null {
  const candidate = window.sessionStorage.getItem("cf-last-workspace");
  return candidate && /^\/campaigns\/[A-Za-z0-9_.:-]+$/.test(candidate) ? candidate : null;
}

export function App() {
  const [path, setPath] = useState(window.location.pathname);
  const isCampaignWizard = path === "/campaigns/new";
  const campaignMatch = isCampaignWizard
    ? null
    : path.match(/^\/campaigns\/([A-Za-z0-9_.:-]+)$/);
  const [lastWorkspacePath, setLastWorkspacePath] = useState<string | null>(rememberedWorkspace);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => apiGet<Health>("/api/v1/health"),
    refetchInterval: 30_000,
    enabled: path !== "/login",
  });
  const publicConfig = useQuery({
    queryKey: ["public-config"],
    queryFn: () => apiGet<PublicConfig>("/api/v1/config/public"),
    staleTime: 60_000,
    enabled: path !== "/login",
  });
  const logout = useMutation({
    mutationFn: authLogout,
    onSuccess: () => window.location.replace("/login"),
  });
  useEffect(() => {
    const onPopState = () => {
      const nextPath = window.location.pathname;
      setPath(nextPath);
      if (/^\/campaigns\/[A-Za-z0-9_.:-]+$/.test(nextPath)) {
        window.sessionStorage.setItem("cf-last-workspace", nextPath);
        setLastWorkspacePath(nextPath);
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = (nextPath: string) => {
    const next = new URL(nextPath, window.location.origin);
    if (`${next.pathname}${next.search}` === `${window.location.pathname}${window.location.search}`) return;
    window.history.pushState({}, "", `${next.pathname}${next.search}`);
    setPath(next.pathname);
    if (/^\/campaigns\/[A-Za-z0-9_.:-]+$/.test(next.pathname) && next.pathname !== "/campaigns/new") {
      window.sessionStorage.setItem("cf-last-workspace", next.pathname);
      setLastWorkspacePath(next.pathname);
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  if (path === "/login") return <LoginPage />;

  const activeSection = campaignMatch
    ? "workspace"
    : path === "/results" || path === "/evaluation"
      ? "results"
      : path === "/diagnostics"
        ? "diagnostics"
        : "campaigns";
  const healthLabel = health.isPending
    ? "Проверка контура"
    : health.data?.status === "ok"
      ? "Контур готов"
      : "Контур недоступен";

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        К основному содержимому
      </a>
      <header className="topbar">
        <a
          className="brand"
          href="/"
          aria-label="Фабрика коммуникаций — кампании"
          onClick={(event) => {
            event.preventDefault();
            navigate("/");
          }}
        >
          <span className="brand-mark"><Factory size={20} /></span>
          <span className="brand-copy"><strong>Фабрика коммуникаций</strong><small>Управляемые коммуникации</small></span>
        </a>
        <nav className="primary-nav" aria-label="Основная навигация">
          <NavItem active={activeSection === "campaigns"} href="/" icon={<LayoutDashboard size={16} />} label="Кампании" navigate={navigate} />
          <NavItem
            active={activeSection === "workspace"}
            disabled={!lastWorkspacePath}
            href={lastWorkspacePath ?? ""}
            hint={!lastWorkspacePath ? "сначала откройте кампанию" : undefined}
            icon={<FlaskConical size={16} />}
            label="Кампания"
            navigate={navigate}
          />
          <NavItem active={activeSection === "results"} href="/results" icon={<BadgeCheck size={16} />} label="Результаты" navigate={navigate} />
          <NavItem active={activeSection === "diagnostics"} href="/diagnostics" icon={<Activity size={16} />} label="Диагностика" navigate={navigate} />
        </nav>
        <div className="topbar-status">
          <div className="boundary-badges" aria-label="Границы системы">
            <span><FlaskConical size={13} aria-hidden="true" /> Синтетические данные</span>
            <span><LockKeyhole size={13} aria-hidden="true" /> Отправка отключена</span>
          </div>
          <span aria-hidden="true" className={`health-dot ${health.data?.status === "ok" ? "is-ready" : ""}`} />
          <span className="health-label" role="status">{healthLabel}</span>
          {publicConfig.data?.session_auth_enabled ? (
            <button
              aria-label="Выйти"
              className="icon-button logout-button"
              disabled={logout.isPending}
              onClick={() => logout.mutate()}
              type="button"
            >
              <LogOut size={17} />
            </button>
          ) : null}
        </div>
      </header>

      <div aria-label="Границы системы" className="safety-strip">
        <span><FlaskConical size={12} aria-hidden="true" /> Синтетические данные</span>
        <span><LockKeyhole size={12} aria-hidden="true" /> Отправка отключена</span>
      </div>

      <nav className="mobile-nav" aria-label="Мобильная навигация">
        <NavItem active={activeSection === "campaigns"} href="/" icon={<LayoutDashboard size={18} />} label="Кампании" navigate={navigate} />
        <NavItem
          active={activeSection === "workspace"}
          disabled={!lastWorkspacePath}
          href={lastWorkspacePath ?? ""}
          hint={!lastWorkspacePath ? "нет кампании" : undefined}
          icon={<FlaskConical size={18} />}
          label="Кампания"
          navigate={navigate}
        />
        <NavItem active={activeSection === "results"} href="/results" icon={<BadgeCheck size={18} />} label="Результаты" navigate={navigate} />
        <NavItem active={activeSection === "diagnostics"} href="/diagnostics" icon={<Activity size={18} />} label="Диагностика" navigate={navigate} />
      </nav>

      <main id="main-content" tabIndex={-1}>
        {isCampaignWizard ? (
          <CampaignWizardPage navigate={navigate} />
        ) : campaignMatch ? (
          <WorkspacePage
            key={campaignMatch[1]}
            campaignId={campaignMatch[1]}
            navigate={navigate}
            publicConfig={publicConfig.data}
          />
        ) : path === "/results" ? (
          <MvpResultsPage />
        ) : path === "/evaluation" ? (
          <EvaluationPage />
        ) : path === "/diagnostics" ? (
          <DiagnosticsPage />
        ) : (
          <DashboardPage navigate={navigate} />
        )}
      </main>

      <footer className="app-footer">
        <span>Фабрика коммуникаций · основной контур</span>
        <span>Все данные синтетические · внешняя отправка отключена</span>
      </footer>
    </div>
  );
}

function NavItem({
  active,
  href,
  icon,
  label,
  navigate,
  disabled = false,
  hint,
}: {
  active: boolean;
  href: string;
  icon: React.ReactNode;
  label: string;
  navigate: (path: string) => void;
  disabled?: boolean;
  hint?: string;
}) {
  if (disabled) {
    return (
      <span aria-disabled="true" className="nav-item is-disabled">
        {icon}<span>{label}{hint ? <small>{hint}</small> : null}</span>
      </span>
    );
  }
  return (
    <a
      aria-current={active ? "page" : undefined}
      className={active ? "nav-item is-active" : "nav-item"}
      href={href}
      onClick={(event) => {
        event.preventDefault();
        navigate(href);
      }}
    >
      {icon}<span>{label}</span>
    </a>
  );
}
