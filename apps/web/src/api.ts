export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}
export async function apiGet<T>(path: string): Promise<T> {
  return apiRequest<T>(path, { method: "GET" });
}

export async function apiMutation<T>(path: string, body?: unknown): Promise<T> {
  const csrf = readCookie("cf_csrf");
  return apiRequest<T>(path, {
    method: "POST",
    headers: {
      "Idempotency-Key": `ui-${crypto.randomUUID()}-${Date.now()}`,
      ...(csrf ? { "X-CF-CSRF": csrf } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function authSession(): Promise<{ username: string } | null> {
  const response = await fetch("/auth/session", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw await apiError(response);
  const payload = (await response.json()) as {
    authenticated: boolean;
    username?: string;
  };
  return payload.authenticated && payload.username ? { username: payload.username } : null;
}

export async function authLogin(username: string, password: string): Promise<void> {
  const response = await fetch("/auth/login", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw await apiError(response);
}

export async function authLogout(): Promise<void> {
  const csrf = readCookie("cf_csrf");
  const response = await fetch("/auth/logout", {
    method: "POST",
    headers: {
      Accept: "application/json",
      ...(csrf ? { "X-CF-CSRF": csrf } : {}),
    },
  });
  if (!response.ok) throw await apiError(response);
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const row = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return row ? decodeURIComponent(row.slice(prefix.length)) : null;
}

async function apiRequest<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...init.headers,
    },
  });
  if (!response.ok) {
    if (response.status === 401 && window.location.pathname !== "/login") {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    }
    throw await apiError(response);
  }
  return response.json() as Promise<T>;
}

async function apiError(response: Response): Promise<ApiError> {
  let detail = `HTTP ${response.status}`;
  try {
    const payload = (await response.json()) as { detail?: unknown; error?: unknown };
    detail = formatApiDetail(payload.detail) ?? formatApiDetail(payload.error) ?? detail;
  } catch {
    // The status remains authoritative when a response has no JSON body.
  }
  return new ApiError(response.status, detail);
}

function formatApiDetail(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (Array.isArray(value)) {
    const items = value
      .map((item) => formatApiDetail(item))
      .filter((item): item is string => Boolean(item));
    return items.length ? items.join("; ") : null;
  }
  if (!value || typeof value !== "object") return null;

  const issue = value as Record<string, unknown>;
  const message = presentApiIssue(issue.type, issue.msg ?? issue.message);
  if (message) {
    const location = presentApiLocation(issue.loc);
    return location ? `${message}: ${location}` : message;
  }
  return formatApiDetail(issue.detail) ?? formatApiDetail(issue.error);
}

function presentApiIssue(type: unknown, message: unknown): string | null {
  const translations: Record<string, string> = {
    missing: "Не заполнено обязательное поле",
    string_too_short: "Значение слишком короткое",
    string_too_long: "Значение слишком длинное",
    value_error: "Недопустимое значение",
  };
  if (typeof type === "string" && translations[type]) return translations[type];
  if (message === "Field required") return translations.missing;
  return typeof message === "string" && message.trim() ? message.trim() : null;
}

function presentApiLocation(value: unknown): string | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const [scope, ...parts] = value.map(String);
  const location = parts.join(".");
  if (!location) return null;
  if (scope === "header") return `заголовок ${location}`;
  if (scope === "query") return `параметр ${location}`;
  if (scope === "body") return `поле ${location}`;
  return [scope, ...parts].join(".");
}
