import {
  AlertTriangle,
  Check,
  ChevronRight,
  CircleAlert,
  LoaderCircle,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";

import { shortHash } from "./format";
import { ADMISSION_OPEN, MODE_PRESENTATION, presentStatus } from "./presentation/status";
import type { StatusPresentation } from "./presentation/status";
import type { ExecutionMode } from "./types";

type StatusBadgeProps = {
  value: string | null;
  subtle?: boolean;
};

export function StatusBadge({ value, subtle = false }: StatusBadgeProps) {
  return <ToneBadge presentation={presentStatus(value)} subtle={subtle} />;
}

export function AdmissionBadge({ state }: { state: "CLOSED" | "OPEN" }) {
  const presentation = state === "OPEN" ? ADMISSION_OPEN : presentStatus("CLOSED");
  return <ToneBadge presentation={presentation} />;
}

export function ToneBadge({
  presentation,
  subtle = false,
}: {
  presentation: StatusPresentation;
  subtle?: boolean;
}) {
  const { label, tone, raw, known } = presentation;
  return (
    <span
      className={`badge badge-${tone}${subtle ? " badge-subtle" : ""}`}
      title={known && label !== raw ? `Код состояния: ${raw}` : undefined}
    >
      {label}
    </span>
  );
}

export function ModeBadge({
  mode,
  detailed = false,
}: {
  mode: ExecutionMode | null;
  detailed?: boolean;
}) {
  if (!mode) return <span className="mode-badge mode-empty">Нет запуска</span>;
  const { label } = MODE_PRESENTATION[mode];
  return (
    <span
      aria-label={`Режим: ${label} (${mode})`}
      className={`mode-badge mode-${mode}`}
      title={`Режим исполнения: ${mode}`}
    >
      <span>{label}</span>
      {detailed ? <code>{mode}</code> : null}
    </span>
  );
}

export function HashValue({ value, label }: { value: string | null; label?: string }) {
  return (
    <span className="hash-value" title={value ?? "Нет значения"}>
      {label ? <span>{label}</span> : null}
      <code>{shortHash(value)}</code>
    </span>
  );
}

export function LoadingState({ label = "Загружаем данные" }: { label?: string }) {
  return (
    <div className="state-panel" role="status" aria-live="polite">
      <LoaderCircle className="spin" size={22} />
      <div>
        <strong>{label}</strong>
        <p>Это состояние завершится ошибкой или результатом — бесконечного ожидания нет.</p>
      </div>
    </div>
  );
}

export function ErrorState({ error, retry }: { error: Error; retry?: () => void }) {
  return (
    <div className="state-panel state-error" role="alert">
      <CircleAlert size={22} />
      <div>
        <strong>Не удалось получить данные</strong>
        <p>{error.message}</p>
        {retry ? (
          <button className="button button-secondary button-small" type="button" onClick={retry}>
            Повторить
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="empty-state">
      <ShieldCheck size={23} />
      <strong>{title}</strong>
      <p>{children}</p>
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "warning" | "success";
  title: string;
  children: ReactNode;
}) {
  const Icon = tone === "warning" ? AlertTriangle : tone === "success" ? Check : ShieldCheck;
  return (
    <div className={`notice notice-${tone}`}>
      <Icon size={18} />
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
    </div>
  );
}

export function MetricCard({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {note ? <small>{note}</small> : null}
    </article>
  );
}

// Видимая причина недоступного действия: текст рядом с контролом,
// связанный через aria-describedby, а не только title.
export function DisabledReason({ id, children }: { id: string; children: ReactNode }) {
  return (
    <p className="control-reason" id={id}>
      {children}
    </p>
  );
}

export type TabOption<T extends string> = {
  id: T;
  label: string;
  count?: number;
  group?: string;
};

export function Tabs<T extends string>({
  idBase,
  label,
  options,
  value,
  onChange,
}: {
  idBase: string;
  label: string;
  options: Array<TabOption<T>>;
  value: T;
  onChange: (value: T) => void;
}) {
  const listRef = useRef<HTMLDivElement>(null);

  const focusAndSelect = (index: number) => {
    const option = options[(index + options.length) % options.length];
    onChange(option.id);
    const node = listRef.current?.querySelector<HTMLButtonElement>(
      `#${idBase}-tab-${option.id}`,
    );
    node?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const currentIndex = options.findIndex((option) => option.id === value);
    if (event.key === "ArrowRight") focusAndSelect(currentIndex + 1);
    else if (event.key === "ArrowLeft") focusAndSelect(currentIndex - 1);
    else if (event.key === "Home") focusAndSelect(0);
    else if (event.key === "End") focusAndSelect(options.length - 1);
    else return;
    event.preventDefault();
  };

  return (
    <div className="tabs" ref={listRef} role="tablist" aria-label={label} onKeyDown={onKeyDown}>
      {options.map((option, index) => {
        const previous = options[index - 1];
        const startsGroup =
          option.group !== undefined && (!previous || previous.group !== option.group);
        return (
          <span className="tab-slot" key={option.id}>
            {startsGroup && index > 0 ? (
              <span aria-hidden="true" className="tab-separator" />
            ) : null}
            {startsGroup ? (
              <span aria-hidden="true" className="tab-group-label">
                {option.group}
              </span>
            ) : null}
            <button
              aria-controls={`${idBase}-panel`}
              aria-selected={value === option.id}
              className={value === option.id ? "tab is-active" : "tab"}
              id={`${idBase}-tab-${option.id}`}
              onClick={() => onChange(option.id)}
              role="tab"
              tabIndex={value === option.id ? 0 : -1}
              type="button"
            >
              {option.label}
              {option.count !== undefined ? <span>{option.count}</span> : null}
            </button>
          </span>
        );
      })}
    </div>
  );
}

export function TabPanel({
  idBase,
  activeTab,
  children,
}: {
  idBase: string;
  activeTab: string;
  children: ReactNode;
}) {
  return (
    <div
      aria-labelledby={`${idBase}-tab-${activeTab}`}
      className="tab-panel"
      id={`${idBase}-panel`}
      role="tabpanel"
      tabIndex={0}
    >
      {children}
    </div>
  );
}

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function ConfirmDialog({
  open,
  title,
  description,
  confirmation,
  confirmLabel,
  danger = false,
  busy = false,
  requireTyping = false,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmation: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  requireTyping?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [typedConfirmation, setTypedConfirmation] = useState("");
  const cancel = () => {
    setTypedConfirmation("");
    onCancel();
  };
  const confirm = () => {
    setTypedConfirmation("");
    onConfirm();
  };

  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    dialog?.querySelector<HTMLElement>(".dialog-cancel")?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setTypedConfirmation("");
        onCancel();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (node) => !node.hasAttribute("disabled"),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      opener?.focus();
    };
  }, [open, onCancel]);

  if (!open) return null;
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={cancel}>
      <div
        aria-describedby="confirm-description"
        aria-labelledby="confirm-title"
        aria-modal="true"
        className="dialog"
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
      >
        <button className="icon-button dialog-close" aria-label="Закрыть" onClick={cancel} type="button">
          <X size={18} />
        </button>
        <p className="eyebrow">Точное подтверждение</p>
        <h2 id="confirm-title">{title}</h2>
        <p id="confirm-description">{description}</p>
        <code className="confirmation-value">{confirmation}</code>
        {requireTyping ? (
          <label className="field confirmation-input" htmlFor="typed-confirmation">
            <span>Введите подтверждение точно как показано выше</span>
            <input
              autoComplete="off"
              id="typed-confirmation"
              onChange={(event) => setTypedConfirmation(event.target.value)}
              value={typedConfirmation}
            />
          </label>
        ) : null}
        <div className="dialog-actions">
          <button className="button button-secondary dialog-cancel" onClick={cancel} type="button">
            Отмена
          </button>
          <button
            className={`button ${danger ? "button-danger" : "button-primary"}`}
            disabled={busy || (requireTyping && typedConfirmation !== confirmation)}
            onClick={confirm}
            type="button"
          >
            {busy ? <LoaderCircle className="spin" size={17} /> : null}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ActionLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a className="inline-link" href={href}>
      {children} <ChevronRight size={14} aria-hidden="true" />
    </a>
  );
}
