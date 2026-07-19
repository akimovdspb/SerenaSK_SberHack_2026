import { presentStatus } from "./presentation/status";

export function formatDate(value: string | null): string {
  if (!value) return "—";
  const formatted = new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
  return `${formatted} МСК`;
}
export function formatLatency(value: number | null): string {
  if (value === null) return "—";
  if (value < 1_000) return `${value} мс`;
  return `${(value / 1_000).toFixed(1)} с`;
}

export function formatMoney(value: number): string {
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 3,
    maximumFractionDigits: 6,
  }).format(value);
}

export function shortHash(value: string | null, length = 9): string {
  if (!value) return "—";
  return `${value.slice(0, length)}…`;
}

export function humanStatus(value: string | null): string {
  return presentStatus(value).label;
}
