import { useMutation, useQuery } from "@tanstack/react-query";
import { Factory, FlaskConical, LockKeyhole, LogIn, ShieldCheck } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

import { authLogin, authSession } from "../api";

function requestedPath(): string {
  const candidate = new URLSearchParams(window.location.search).get("next") ?? "/";
  return candidate.startsWith("/") && !candidate.startsWith("//") ? candidate : "/";
}

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const session = useQuery({ queryKey: ["auth-session"], queryFn: authSession, retry: false });
  const login = useMutation({
    mutationFn: () => authLogin(username, password),
    onSuccess: () => {
      setPassword("");
      window.location.replace(requestedPath());
    },
  });

  useEffect(() => {
    if (session.data) window.location.replace(requestedPath());
  }, [session.data]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (username.trim() && password) login.mutate();
  };

  return (
    <main className="login-page" id="main-content">
      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-brand">
          <span className="brand-mark"><Factory size={22} /></span>
          <div><strong>Фабрика коммуникаций</strong><span>Командный демо-контур</span></div>
        </div>
        <div className="login-copy">
          <p className="eyebrow">Защищённый вход</p>
          <h1 id="login-title">Добро пожаловать</h1>
          <p>Войдите, чтобы работать с синтетическими кейсами и управляемой генерацией.</p>
        </div>
        <form className="login-form" onSubmit={submit}>
          <label className="field" htmlFor="login-username">
            <span>Логин</span>
            <input
              autoComplete="username"
              autoFocus
              id="login-username"
              maxLength={64}
              onChange={(event) => setUsername(event.target.value)}
              required
              value={username}
            />
          </label>
          <label className="field" htmlFor="login-password">
            <span>Пароль</span>
            <input
              autoComplete="current-password"
              id="login-password"
              maxLength={256}
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>
          {login.isError ? <div className="login-error" role="alert">{login.error.message}</div> : null}
          <button
            className="button button-primary button-block"
            disabled={login.isPending || !username.trim() || !password}
            type="submit"
          >
            <LogIn size={17} /> {login.isPending ? "Проверяем…" : "Войти"}
          </button>
        </form>
        <div className="login-boundaries" aria-label="Границы демо-контура">
          <span><FlaskConical size={14} /> Только синтетические данные</span>
          <span><LockKeyhole size={14} /> Отправка отключена</span>
          <span><ShieldCheck size={14} /> Защищённая сессия в браузере</span>
        </div>
      </section>
      <aside className="login-aside" aria-label="О продукте">
        <p className="eyebrow">Фабрика коммуникаций</p>
        <h2>От брифа до проверенного пакета коммуникаций</h2>
        <p>Факты, проверка качества, версии, управляемая обратная связь и человек в точке решения.</p>
      </aside>
    </main>
  );
}
