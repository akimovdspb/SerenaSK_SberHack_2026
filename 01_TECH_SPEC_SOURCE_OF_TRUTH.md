# Техническое задание — нормативный source of truth комплекта

## «Автономная фабрика персонализированных клиентских коммуникаций на базе Ouroboros»

Версия: 5.2.3 final  
Дата фиксации: 11 июля 2026 года  
Целевая среда: VPS, Docker Compose  
Исполнитель: GPT-5.6 Sol/Codex через `/goal`  
Назначение: реализация и приёмка Project Results основного этапа Sber AI Hack.

---

## 0. Статус документа и правила исполнения

Этот файл — единственный нормативный технический контракт комплекта. Supporting-документы определяют порядок запуска, аудита, evaluation, демо и submission, но не добавляют функции P0 и не ослабляют требования этого файла.

Исполнитель обязан:

1. Прочитать документ целиком до выбора архитектуры.
2. Изучить положение о треке, Project Proposal, официальные материалы Ouroboros, вебинар, встречи и legacy-код.
3. Работать поверх подготовленного production-oriented Git baseline текущего workspace; если `.git` отсутствует, создать новый чистый репозиторий. Исходный архив использовать read-only.
4. Реализовывать P0 последовательно по gates; P1 запрещён до сохранённого зелёного P0 release candidate.
5. Не считать scaffold, UI-макет, mock-only flow или unit tests завершением задачи.
6. Проверить продукт реальным Ouroboros task, browser E2E и live evaluation; подготовить шесть выходов для независимого ручного чтения, не фабрикуя человеческие оценки.
7. Связать каждое публичное обещание с requirement, test и evidence.
8. При расхождении примера API с pinned Ouroboros следовать фактическому контракту, обновить adapter/contract test и зафиксировать ADR; не патчить ядро.
9. Не push-ить, не публиковать сервис и не использовать production credentials без отдельного разрешения владельца.
10. После каждого gate обновлять `STATUS.md`, `DECISIONS.md`, `docs/TRACEABILITY.md` и результаты verification.

### 0.1. Приоритет источников

Приоритет разделён на две оси.

**Цели, продуктовый scope, безопасность и acceptance:**

1. `1. Положение_о_треке_Sber_AI_Hack.pdf`.
2. `Наш проект/Фабрика коммуникаций.pdf`.
3. Настоящее ТЗ.
4. `spec_constants.yaml` как проекция раздела 4.
5. Supporting-документы текущего комплекта.
6. Встречи и вебинар.
7. Legacy и предыдущие ТЗ/мнения.

**Интеграционные детали внешней зависимости:**

1. официальный source/OpenAPI/settings schema/skill contract закреплённого tag Ouroboros;
2. сохранённые snapshots и выполняемые contract tests;
3. примеры payload/config в настоящем ТЗ и supporting-документах.

ТЗ не может объявить поддерживаемым несуществующее поле или transport. Любое расхождение фиксируется в ADR: исполнитель обновляет adapter, contract tests и примеры документов под фактический pinned-контракт, не патчит ядро Ouroboros и не ослабляет продуктовые инварианты.

Если Project Proposal или положение официально изменены после даты фиксации, сначала обновляется traceability и acceptance, затем код. Новые функции нельзя обещать в README/видео до их реализации и evidence.

### 0.2. Язык и терминология

- UI, prompts, synthetic content и пользовательская документация — на русском языке.
- Технические идентификаторы и стандартные названия протоколов — на английском.
- `live_ouroboros` означает новый фактический task выбранного pinned runtime, а не replay/cache/template.
- `business case` — синтетический, но реалистичный пример штатного бизнес-процесса или его корректного controlled outcome.
- `chaos case` — техническая авария; он не подменяет business case.

---

## 1. Критерии хакатона как release gates

| Критерий | Вес | Условие 5/5 | Обязательное evidence |
|---|---:|---|---|
| Работоспособность | 30% | Чистый запуск; весь заявленный MVP; настоящий Ouroboros end-to-end | Playwright, task/tool receipts, export, traceability |
| Демо | 30% | Видео `<180` секунд с озвучкой и полным понятным сценарием | MP4/link, canonical script, pinned build |
| Документация | 15% | Репозиторий и README с `<5` шагами | README с 4 шагами, clean-clone transcript |
| Подтверждение метрик | 15% | `≥10` примеров бизнес-процесса с приложенными результатами | 15 business cases, ≥10 live, PDF/JPG/HTML/CSV/JSON/JSONL |
| Безопасность | 5% | Нет hardcoded secrets | tree/history scans, `.env.example`, redacted artifacts |
| Стабильность | 5% | Нет crash, действий `>30` секунд и бесконечных циклов | SLO/chaos report, bounded execution tests |

Работоспособность и демо дают 60% оценки, поэтому P0, live path и воспроизводимость имеют приоритет над расширениями и косметикой.

### 1.1. Дополнительные ограничения положения

- В Project Results и исходном репозитории не должно быть персональных данных участников/работников.
- Использование proprietary technology, требующей отдельного платного приобретения прав, запрещено; все зависимости и ассеты проходят license review.
- Код, алгоритмы, архитектура и подготовительные материалы должны быть воспроизводимы и передаваемы организатору в предусмотренных положением пределах.
- Провайдеры моделей допустимы в рамках выданных/разрешённых ключей; приложение не должно зависеть от закрытого платного SDK/ассета без разрешённых прав.

---

## 2. Цель продукта и границы процесса

Создать воспроизводимый прототип, который превращает заказ кампании или синтетические данные о клиенте/компании в проверенный комплект персонализированных коммуникаций:

1. принимает бриф;
2. детерминированно проверяет полноту и противоречия;
3. задаёт только необходимые уточняющие вопросы;
4. получает разрешённый продуктовый, сегментный, исторический и policy-контекст;
5. посредством Ouroboros формирует единый structured package SMS + e-mail;
6. независимо проверяет факты, числа, даты, ссылки, дисклеймеры, ограничения каналов и безопасность;
7. показывает человеку коммуникации, факт-карточку, findings и безопасную трассу;
8. принимает замечание и создаёт targeted immutable revision с diff;
9. по отдельному явному действию предлагает ограниченное переиспользуемое правило;
10. проверяет правило на target, conflict, mini-regression и out-of-scope case;
11. активирует правило только после human approval;
12. требует human approval конкретной версии пакета;
13. экспортирует материалы, но ничего не отправляет во внешние системы;
14. формирует проверяемый отчёт по 15 business cases и отдельному chaos suite.

Главное демонстрационное обещание:

> Из готового брифа до проверенного первого пакета SMS и e-mail — менее 30 секунд, с новой live-задачей Ouroboros, проверяемыми источниками и финальным решением человека.

Его нельзя подтверждать replay, кэшем или заранее сохранённым output.

### 2.1. Что продукт не делает

- не заменяет CRM/CDP;
- не выбирает клиенту продукт вместо существующей рекомендательной системы;
- не рассчитывает next-best-action;
- не хранит и не обрабатывает реальные ПДн/банковскую тайну;
- не отправляет SMS/e-mail и не подключается к production delivery systems;
- не принимает legal/brand/final approval за человека;
- не заявляет доказанный рост конверсии по synthetic evaluation;
- не изменяет ядро Ouroboros и не генерирует исполняемый код по feedback.

---

## 3. Scope и соответствие Project Proposal

### 3.1. P0-Core — обязательный MVP

Project Proposal явно обещает следующее ядро, поэтому оно обязательно:

- бриф и детерминированная валидация;
- уточняющие вопросы;
- synthetic context: продукт, сегмент/компания, trigger, история касаний, contact policy;
- SMS и e-mail;
- фактологическая и нормативная проверка;
- human feedback и повторный прогон;
- результаты demo/evaluation;
- безопасный контур, no-send и final human decision;
- реальная роль Ouroboros.

Технический P0 также включает всё необходимое для доказательства этого обещания:

- профессиональный React UI;
- FastAPI/SQLite/Docker Compose;
- pinned headless Ouroboros, external instruction skill и private MCP;
- structured schemas, двусторонний grounding, immutable versions и safe trace;
- targeted revision и diff;
- минимальный governed rule learning как реализация learning loop Proposal;
- approval/export;
- 15 business cases, ≥10 live; отдельные chaos/security cases;
- documentation, tests, evidence и submission package.

### 3.2. P0-Learning — минимальный и обязательный

Learning loop из Proposal реализуется без `/evolve`:

- feedback может быть применён только к новой видимой revision;
- пользователь отдельно выбирает «Предложить правило»;
- Ouroboros создаёт RuleProposal закрытого типа;
- backend выполняет deterministic validation, conflict, target и mini-regression;
- человек approve/reject;
- approved rule имеет immutable version и rollback;
- новый подходящий case получает правило через ContextBundle;
- out-of-scope case доказывает отсутствие лишнего применения.

### 3.3. P1 — только после зелёного P0

Расширения Proposal не являются обязательным объёмом MVP:

- сценарии чата;
- сценарии звонка/голоса;
- CLM;
- баннеры;
- презентации;
- дополнительные reviewed skills/channel adapters.

Каждое расширение:

- feature-flagged и выключено по умолчанию;
- не влияет на P0 startup/readiness/latency;
- имеет typed `OptionalChannelAdapter` и tests;
- скрыто из UI/README/demo, пока не готово;
- не добавляет LLM/provider в backend;
- не заявляется реализованным без evidence.

### 3.4. Явно запрещённый scope

- CRM/recommender/delivery platform;
- реальные integrations/send;
- fine-tuning;
- `/evolve`, background consciousness, post-task core improvement;
- automatic executable rule/code generation;
- скрытые LLM repair/reviewer/rewrite/best-of-N passes;
- микросервисная инфраструктура, Kafka, Kubernetes, Celery/Redis/Postgres без доказанной необходимости;
- full IAM/SSO;
- uploads произвольных документов в P0;
- генеративные изображения и презентации в critical path;
- Gradio как production UI.

### 3.5. Эффекты Proposal

| Показатель из Proposal | Категория в Project Results |
|---|---|
| 1–2 часа до первого черновика | AS-IS baseline/assumption, если не подтверждён обезличенным замером |
| −40–60% времени подготовки | TO-BE hypothesis до пилота |
| −30–50% ручных операций | TO-BE hypothesis до пилота |
| +10–20% отклика/конверсии | business hypothesis, не measured prototype result |

Измеряемые метрики прототипа: latency, terminal outcomes, качество по assertions, grounding, feedback/rule application, tokens/cost, crash/timeout и operator actions. Нельзя писать, что synthetic prototype уже повысил конверсию или сэкономил FTE.

---

## 4. Канонические константы

Значения этого раздела отражены в `spec_constants.yaml`. CI выполняет drift check. При расхождении основной документ имеет приоритет, а release gate падает.

| Константа | Значение |
|---|---|
| P0 channels | `sms`, `email` |
| Основные MCP tools | `cf_context_get`, `cf_draft_save` |
| Effective provider tool set | exactly `mcp_factory__cf_context_get`, `mcp_factory__cf_draft_save` |
| Instruction-skill activation | `adapter_injected` baseline; `native_verified` только после proof |
| Editable prompt source | exact `SKILL.md` body; generated projection byte-equal |
| Primary runtime model | `gpt-5.4-mini` after exact-ID/capability/latency smoke |
| Owner allowance source | external ignored `operator-limits.yaml`; operational assumption, not account ground truth or product P0 |
| Live-run budget control | pinned Ouroboros `TOTAL_BUDGET` + required per-run token/dollar caps + sequential case-boundary accounting |
| `gpt-5.4` | comparator-only, disabled by default, separate run opt-in/caps |
| OpenRouter | disabled by default; explicit owner profile + token/dollar caps |
| Business cases | 15 |
| Minimum live business cases | 10 |
| Target live business cases | 12 |
| Chaos/security cases | 5 |
| Terminal action limit | `<30 s` |
| Ouroboros task deadline | 25 s initial profile |
| Global tool timeout | 5 s initial profile |
| MCP inner tool timeout | 5 s via settings/readback |
| Successful generation operations | 2 logical: context + save |
| Public host bind | gateway only, `127.0.0.1:8080` |
| Private container binds | app `0.0.0.0:8000`, Ouroboros `0.0.0.0:8765` |
| Saved agent drafts | 1 на operation/iteration |
| Semantic auto-repair | 0 |
| Post-task evolution | off; summary/reflection measured separately |
| Demo target/hard limit | 168 s / `<180 s` |
| Full timed rehearsals | minimum 2 by operator |
| README startup steps | 4 |
| Golden Playwright repetitions | 5 |
| Locale | `ru-RU` |
| External send | disabled |
| Specification handoff root | `communication-factory-spec/` (ASCII) |
| Codex completion gate | `make verify-implementation` |
| Final human gate | `make verify-submission` |

---

## 5. Пользователи и сквозные сценарии

### 5.1. Роли P0

- **Маркетолог/редактор** — создаёт бриф, отвечает на уточнения, читает результат, оставляет feedback и смотрит diff.
- **Утверждающий** — активирует правило и утверждает пакет.
- **Demo operator** — seed/reset/evaluation; не имеет отдельной сложной RBAC.

В demo один человек может выполнять обе бизнес-роли, но approval rule и package отображаются как отдельные human actions.

### 5.2. Happy path

```text
выбор synthetic case
→ CampaignBriefDraft validation
→ уточнение при необходимости
→ immutable ReadyCampaignBrief
→ run accepted (202)
→ Ouroboros task
→ cf_context_get(operation=initial)
→ Ouroboros DraftEnvelope<CommunicationBundle>
→ cf_draft_save
→ deterministic grounding/QA
→ review
→ feedback
→ cf_context_get(operation=revision)
→ Ouroboros DraftEnvelope<CommunicationPatch>
→ cf_draft_save
→ deterministic merge → immutable v2 → diff/full QA
→ explicit "propose rule"
→ новый task с operation=rule_proposal
→ RuleProposalEnvelope
→ cf_draft_save
→ target/conflict/regression
→ human rule approval
→ похожий future case получает rule
→ human package approval
→ ZIP export
```

### 5.3. Controlled negative outcomes

- неполный бриф → `NEEDS_INPUT`, LLM не вызывается;
- противоречащие факты → `BLOCKED`/`NEEDS_INPUT`;
- канал запрещён → channel `SUPPRESSED`;
- оба канала запрещены → package `BLOCKED`;
- продукт уже подключён → `NOT_APPLICABLE`;
- неизвестное числовое/датированное/денежное/URL утверждение → QA `BLOCKER`;
- blocker → approval disabled;
- provider/Ouroboros/MCP failure → controlled degraded/failed outcome `<30 s`;
- duplicate request → исходный operation, без второй генерации;
- restart → reconciliation или честный terminal state.

---

## 6. Целевая архитектура

### 6.1. Логическая схема

```text
Browser
  |
  v
gateway — единственный published port, TLS/auth/static SPA/API proxy
  |
  v
app — private FastAPI
  |- REST + domain SSE
  |- internal MCP endpoint
  |- domain/state/SQLite WAL
  |- deterministic validation/QA/render/diff/rules/export/evidence
  |
  | managed Task API + SSE/cancel
  v
ouroboros — private pinned runtime
  |- external instruction skill communication_factory
  |- provider credentials/model routing
  |- MCP client → app internal endpoint
  `- evolution/background/review-improvement disabled
```

Only gateway publishes a host port. Gateway routes public SPA and `/api`; it never routes internal MCP or Ouroboros endpoints.

В canonical Compose profile все процессы внутри контейнеров слушают container interface, а localhost-ограничение задаётся отдельно на host mapping:

```text
host 127.0.0.1:8080 → gateway 0.0.0.0:8080
private gateway/app network → app 0.0.0.0:8000
private app/ouroboros network → ouroboros 0.0.0.0:8765
ouroboros MCP client → http://app:8000/internal/mcp
```

У `app` и `ouroboros` нет Compose `ports`; допустим только internal `expose`. Bind `127.0.0.1` внутри любого из этих контейнеров не используется как способ изоляции: он разрывает межконтейнерную связность. Для запуска gateway непосредственно на host, вне Docker, отдельный профиль может слушать `127.0.0.1`.

### 6.2. Владение ответственностями

**Ouroboros:**

- выполняет campaign task;
- следует instruction skill;
- получает versioned ContextBundle через MCP;
- сам создаёт CommunicationBundle или RuleProposalEnvelope;
- вызывает save tool ровно один раз;
- возвращает короткий typed final result.

**Backend:**

- владеет brief/context/state/versions/rules/approvals/exports/evidence;
- не вызывает LLM и не имеет provider keys;
- валидирует schema и ownership;
- независимо извлекает claims и выполняет QA;
- строит HTML/diff/template fallback;
- не исправляет смысл generated content.

**Человек:**

- отвечает на уточнения;
- инициирует revision/rule proposal;
- approve/reject rule;
- approve package;
- принимает решение об экспорте/дальнейшем использовании.

### 6.3. Рекомендуемый стек

Backend:

- Python 3.11/3.12;
- FastAPI/Uvicorn;
- Pydantic v2;
- SQLAlchemy 2 + SQLite WAL;
- HTTPX;
- официальный MCP SDK/transport, совместимый с pinned Ouroboros;
- Jinja2 + safe HTML sanitizer;
- structlog/JSON logs;
- pytest/pytest-asyncio, Ruff, mypy.

Frontend:

- Node.js 20;
- React + TypeScript + Vite;
- Tailwind + shadcn/Radix или эквивалентный лёгкий стек;
- TanStack Query;
- React Hook Form + Zod;
- Lucide;
- Recharts только для meaningful metrics;
- Playwright.

Infrastructure:

- Docker Compose;
- Caddy 2 или минимальный reverse proxy;
- lockfiles;
- non-root containers;
- healthchecks;
- persistent volumes для DB/artifacts/Ouroboros data;
- bounded resources/restart policy.

UI/UX Pro Max допускается только как pinned/reviewed design-time guidance. Он не является runtime dependency и не получает secrets/real data.

### 6.4. Репозиторий решения

```text
apps/
  api/app/{api,domain,mcp,services,storage,validators,renderers,learning,evidence}
  web/src/{components,features,pages,lib}
ouroboros/
  skills/communication_factory/SKILL.md
  bootstrap/
  ouroboros.lock
prompts/
  communication_factory.ru.md  # generated byte-equal projection of SKILL.md body; do not edit
data/synthetic/{products,segments,touch_history,policies,cases}
rules/{base,learned}
tests/{unit,contract,integration,e2e,chaos,security}
scripts/
runtime/contracts/  # generated atomic contract lock; gitignored
artifacts/evidence/
design-system/MASTER.md
docs/
Makefile
docker-compose.yml
Caddyfile
pyproject.toml
uv.lock
package-lock.json
.env.example
README.md
STATUS.md
DECISIONS.md
LICENSE
THIRD_PARTY_NOTICES.md
```

Source archive, previous apps, PDFs/transcripts, working XLSX/CSV, real-looking assets, runtime secrets and generated caches не коммитятся.
`runtime/contracts/` также не коммитится: это current deployment lock. После freeze его redacted immutable copy переносится в versioned evidence и связывается с commit/checksums.

---

## 7. Реальная интеграция с Ouroboros

### 7.1. Версия и contract lock

Baseline на дату ТЗ:

- tag `v6.61.4`;
- commit `a00d51dd414f794d830cacf7da760061e442fa88`.

На Gate 0:

1. проверить официальный latest stable;
2. по умолчанию оставить протестированный baseline `v6.61.4`; сам факт появления нового stable не является причиной upgrade;
3. переходить на новый stable только при доказанном преимуществе для P0 либо blocker текущего baseline, после contract diff, smoke, regression и ADR;
4. smoke фактически выбранного tag;
5. записать exact tag/full SHA в `ouroboros.lock`, image labels, README и evidence manifest;
6. сохранить snapshots Task API, relevant settings и discovered MCP tools;
7. не использовать `main`, плавающий `latest` или RC без ADR;
8. не откатываться автоматически на старый desktop-only guide/tag;
9. после frozen evaluation не менять runtime даже при новом релизе; исключение — доказанный release blocker с полной повторной evaluation.

### 7.2. Runtime invariants

```dotenv
OUROBOROS_RUNTIME_MODE=light
OUROBOROS_SAFETY_MODE=full
OUROBOROS_TASK_REVIEW_MODE=off
OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES=0
OUROBOROS_POST_TASK_EVOLUTION=false
```

Также:

- `/evolve` off и недоступен;
- background consciousness off;
- post-task core improvement off;
- no delegation/subagents;
- web/browser/shell/git/file/code mutation tools disabled for campaign task;
- finalization grace измеряется и подбирается внутри общего `<30 s`;
- skill/MCP/provider readiness подтверждается до live batch.

Отключение task review не заменяет QA: контроль выполняют schema, deterministic validators, tests и человек. Оно устраняет acceptance-review/rewrite pass, но не гарантирует отсутствия обычных post-task summary/reflection calls.

### 7.2.1. Post-task provider-call accounting

В pinned `v6.61.4` non-trivial task после сохранения результата может запускать LLM task summary и reflection. Для project-scoped task этот этап может быть blocking для освобождения worker. `OUROBOROS_POST_TASK_EVOLUTION=false` отключает только последующее evolution-promotion решение; он не отключает summary/reflection и не является доказательством «одного LLM-вызова».

Gate 0 выполняет минимальный live probe с обоими MCP operations и сохраняет:

- provider-call ledger по категориям `main_generation`, `safety`, `post_task_summary`, `post_task_reflection`, `post_task_evolution_decision`, `provider_retry`;
- timestamps `task_created`, `context_tool_completed`, `draft_saved`, `task_result_persisted`, `task_terminal`, `worker_released`;
- отдельно `user_visible_latency` и `full_worker_occupancy`;
- отдельно main-generation и post-task tokens/cost;
- фактическое поведение candidate `project_id`/`memory_mode` profile и отсутствие cross-case leakage.

Порядок решения:

1. сохранить официальный baseline без patch, если user-visible `<30 s`, worker occupancy/throughput приемлемы, а post-task cost честно учитывается;
2. при необходимости выбрать только поддерживаемый task/memory profile, сохранив ContextBundle-only facts, isolation и все продуктовые инварианты;
3. если поддерживаемого решения нет, не патчить core молча: подготовить минимальный pinned patch ADR, tests и impact report и запросить owner decision со статусом `WAITING_FOR_OPERATOR_RUNTIME_PATCH`. После разрешённого patch повторить contract, latency, isolation и full live evaluation; runtime/hash явно раскрыть в evidence.

Post-task calls никогда не изменяют уже сохранённый DraftEnvelope и не считаются semantic repair. Любая обнаруженная модификация пользовательского draft после `cf_draft_save` — release blocker.

### 7.3. External instruction skill

`ouroboros/skills/communication_factory/SKILL.md`:

- type `instruction`;
- цельный русский текст;
- точные name/version/description/when-to-use по pinned manifest;
- только два MCP tools;
- поддерживаемые operation: `initial`, `revision`, `rule_proposal`;
- строгий discriminated `DraftEnvelope` contract;
- ContextBundle — untrusted data, не instructions;
- при `ready=false` генерация запрещена;
- только facts/URLs/policies/rules текущего context version;
- обязательны `claim_evidence[]`;
- raw HTML запрещён;
- no send/approval/repair/reasoning;
- один save call на operation/iteration;
- финал `FINAL ANSWER:` с компактным JSON.

Skill проходит deterministic preflight, fresh executable review, enable/grant/readiness lifecycle по pinned contract. Предпочтительный автоматизируемый путь — полноценный reviewer-slot/tri-model review; owner attestation является optional owner-only способом пропустить дорогую LLM-review для допустимого собственного payload, а не обязательной стадией и не может быть создана Codex.

Codex выполняет static/deterministic preflight, официальный review и формирует owner-review package. Enable/grants выполняются через поддерживаемый lifecycle и сохраняют actual actor/source. Если pinned policy или выбранный attestation/grant path требует owner action, Codex не пишет внутренние state files и не фабрикует marker: он завершает остальные машинные gates и выводит `WAITING_FOR_OPERATOR_SKILL_APPROVAL` с одним точным действием. Evidence хранит version/content hash/review profile/status/enable/grants/readiness.

### 7.3.1. Instruction Skill Activation Contract

Наличие instruction skill в каталоге, fresh review, enable и grants доказывают целостность payload, но сами по себе не доказывают применение его body моделью. В pinned `v6.61.4` автоматически собираемый раздел installed skills содержит только manifest metadata и явно помечает её как untrusted data; `manifest.body` не добавляется в provider prompt, а `type=instruction` не исполняется через `skill_exec`. Поэтому запуск fail-closed требует отдельного доказательства activation.

Допустимы только два режима:

- `native_verified` — используется лишь если Gate 0 на фактически выбранном pinned runtime находит документированный native hook для привязки exact reviewed `manifest.body` к конкретному managed task и contract test доказывает его присутствие в первом provider request;
- `adapter_injected` — canonical mode для baseline `v6.61.4`: task adapter сам читает exact reviewed `manifest.body` и помещает его в authoritative `TaskCreateRequest.constraints`. В pinned context assembly `task_contract`, включая `constraints`, входит в system message и объявлен authoritative; краткий `description` не повторяет prompt.

Нельзя молча предполагать `native_verified`. Без его доказательства обязательно используется `adapter_injected`.

Единственный редактируемый источник prompt — body `ouroboros/skills/communication_factory/SKILL.md`. Он начинается с безвредного marker `COMMUNICATION_FACTORY_CONTRACT_V1`. `prompts/communication_factory.ru.md` генерируется из body без смысловых изменений и обязан быть byte-equal после нормализации единственного завершающего LF; ручное редактирование generated mirror запрещено. `make skill-contract` и CI проверяют extraction/equality/hashes.

Canonical Compose монтирует один и тот же каталог payload read-only:

```yaml
services:
  app:
    volumes:
      - ./ouroboros/skills/communication_factory:/skills/communication_factory:ro
      - ./runtime/contracts:/contract-lock:ro
  ouroboros:
    volumes:
      - ./ouroboros/skills:/skills:ro
```

`make bootstrap` запускает one-shot service `contract-probe` из того же exact Ouroboros image с теми же settings, private networks, skill mount и read-only Ouroboros data state. У него нет provider credentials и provider egress; единственная writable поверхность — `./runtime/contracts:/contract-lock`. Probe подменяет только transport на no-forward capture seam, атомарно пишет lock и завершается. Обычные `app`/`ouroboros` не могут изменять lock; app читает его read-only.

Перед созданием каждой campaign task adapter:

1. читает `GET /api/extensions/communication_factory/manifest` только по private app↔Ouroboros network;
2. требует `type=instruction`, `enabled=true`, `executable_review=true`, `review_stale=false`, непустые version/content hash и отсутствие load error;
3. вычисляет content hash mounted payload по pinned algorithm и требует равенства current hash из API; для P0 instruction directory разрешён только `SKILL.md`;
4. извлекает exact body, проверяет marker, generated-mirror equality и сохраняет `skill_file_sha256`, `skill_content_hash`, `prompt_hash`, `activation_mode`;
5. передаёт exact body как строку `constraints`; ContextBundle/feedback/rules остаются отдельными untrusted JSON, получаемыми только через `cf_context_get`;
6. отказывает до provider call с `SKILL_ACTIVATION_NOT_PROVEN` при любой stale/missing/hash/equality/marker ошибке.

Gate 0 выполняет no-forward probe на exact production image/task profile: перехватывает уже собранные аргументы первого `call_llm_with_retry` до сетевого вызова, проверяет system-role `task_contract.constraints`, marker, обязательные разделы и `prompt_hash`, затем сохраняет только redacted projection/booleans/hashes — без raw prompt, ContextBundle, reasoning или secret. Утверждение модели «я использовала skill» не является evidence.

Canonical runtime lock хранится в `runtime/contracts/communication_factory.lock.json`; неизменяемая копия включается в `artifacts/evidence/contracts/communication_factory.lock.json`. Lock связывает runtime tag/SHA/image digest, skill version/content hash, prompt hash, activation mode, full tool-inventory/denylist hashes, extension catalog hash и MCP settings hash. Перед каждой task app читает current manifest/readiness, runtime state/image identity, extension catalog и MCP status/settings readback через private APIs и сравнивает их входные hashes с lock; locked `effective_tool_names` служат единственным источником generated denylist. Любой drift блокирует generation и требует повторного `make bootstrap`. Нельзя принимать lock, вычисленный другим image/profile, или редактировать его вручную.

### 7.4. MCP settings

В baseline `MCP_ENABLED` и `MCP_SERVERS` — settings. Bootstrap использует поддерживаемый settings API или официальный settings file contract, а не выдуманный environment shortcut.

Эквивалентная конфигурация:

```json
{
  "MCP_ENABLED": true,
  "MCP_TOOL_TIMEOUT_SEC": 5,
  "MCP_SERVERS": [
    {
      "id": "factory",
      "name": "Communication Factory",
      "enabled": true,
      "transport": "streamable_http",
      "url": "http://app:8000/internal/mcp",
      "auth_header": "Authorization",
      "auth_token": "Bearer <runtime-secret>",
      "allowed_tools": ["cf_context_get", "cf_draft_save"]
    }
  ]
}
```

Точный transport и поля сверяются с pinned tag. Token поступает в runtime из secret/env, маскируется в logs/diagnostics и не попадает в Git. Bootstrap атомарно записывает `MCP_ENABLED`, `MCP_SERVERS` и `MCP_TOOL_TIMEOUT_SEC=5` через supported settings contract, затем читает их обратно через официальный API/config view. `/api/mcp/status`/test должны подтвердить два разрешённых tools, реальные prefixed names и effective timeout.

`OUROBOROS_TOOL_TIMEOUT_SEC=5` — отдельный global outer setting и не заменяет `MCP_TOOL_TIMEOUT_SEC`. В baseline MCP manager применяет inner timeout 5 s, а outer execution envelope может включать документированный cleanup margin. Readiness проверяет `configured_mcp_timeout ≤ task_deadline < terminal_limit` и отдельно фактические `cf_context_get ≤3 s`, `cf_draft_save ≤5 s`.

### 7.4.1. Exact effective tool-set contract

`MCP_SERVERS[].allowed_tools` ограничивает только поверхность соответствующего MCP server. Он не удаляет built-in tools Ouroboros, tools включённых extensions или tools других MCP servers. Текстовый запрет в `constraints` также не является capability boundary.

Для campaign task фактический набор schemas, переданный provider, обязан быть в точности:

```text
{
  mcp_factory__cf_context_get,
  mcp_factory__cf_draft_save
}
```

Gate 0 после настройки MCP и skill lifecycle запускает внутри exact pinned Ouroboros image deterministic inventory probe с тем же task type/runtime/resource/extension profile, но без provider network call:

1. получает полный результат `ToolRegistry.schemas()` до campaign denylist: built-ins + live extensions + все MCP schemas;
2. проверяет наличие двух ожидаемых prefixed tools, уникальность имён и отсутствие schema collision;
3. сохраняет sorted names, per-schema SHA-256, общий `inventory_hash`, runtime/config/extension/MCP hashes;
4. строит `disabled_tools = all_effective_tool_names - allowed_effective_tool_names`;
5. повторно собирает schemas с этим task contract и требует exact set equality;
6. на provider seam no-forward probe проверяет, что в первый фактический request ушли только две разрешённые schemas;
7. сохраняет redacted inventory/denylist hashes в `communication_factory.lock.json`.

Task adapter добавляет полный generated `disabled_tools` во все операции `initial`, `revision` и `rule_proposal`; `enable_tools`, delegation, shell, browser, files, approval, skill lifecycle, extension и любой иной MCP tool входят в denylist. Pinned registry применяет `disabled_tools` и к schema discovery, и к execution path. Любое новое/неучтённое/лишнее имя, несовпадение current inventory с lock или отсутствие одного из двух tools завершает запрос до provider call с `TOOL_ISOLATION_NOT_PROVEN`.

Network resource нельзя глобально выключить, потому что private MCP использует network; изоляцию обеспечивает exact denylist и private routing. В production profile не загружаются live extensions. Contract tests отдельно доказывают, что execution неизвестного/disabled name также блокируется, а не только скрывается из prompt.

### 7.5. Managed Task API

Использовать `POST /api/tasks`, read result/status, SSE event replay, cancel и artifacts выбранного tag. Минимальная семантика baseline:

```python
allowed_effective_tool_names = {
    "mcp_factory__cf_context_get",
    "mcp_factory__cf_draft_save",
}
skill_body = load_and_verify_skill_body(
    "/skills/communication_factory/SKILL.md",
    contract_lock,
)
assert set(inventory_lock["effective_tool_names"]) >= allowed_effective_tool_names

task_request = {
    "description": (
        f"Кампания {campaign_id}; операция {operation}; итерация {iteration}. "
        "Исполни обязательные инструкции этой задачи и верни итоговый JSON указанного формата."
    ),
    "context": "Синтетическая задача без отправки; бизнес-данные доступны только через инструмент контекста.",
    "expected_output": "FINAL ANSWER: {campaign_id, operation, iteration, draft_id, status, blockers, warnings}",
    "constraints": skill_body,  # exact reviewed manifest.body, a string
    "disabled_tools": sorted(
        set(inventory_lock["effective_tool_names"]) - allowed_effective_tool_names
    ),
    "answer_protocol": "final_answer_line",
    "project_id": f"campaign_{filesystem_safe_uuid}",
    "memory_mode": "forked",
    "timeout_sec": 25,
    "source": "communication_factory_ui",
    "metadata": {
        "campaign_id": campaign_id,
        "operation": operation,
        "iteration": iteration,
        "idempotency_key": idempotency_key,
        "skill_content_hash": contract_lock["skill_content_hash"],
        "prompt_hash": contract_lock["prompt_hash"],
        "tool_inventory_hash": inventory_lock["inventory_hash"],
        "activation_mode": contract_lock["activation_mode"],
    },
}
```

`constraints` — exact reviewed skill body и всегда строка. Это не краткое напоминание и не вручную поддерживаемая копия. Перед реализацией адаптер экспортирует фактический `TaskCreateRequest`; поддерживаемые `disabled_tools`, `allowed_resources` и `resource_policy` формируются из реального контракта. Неподтверждённый `task_constraint` не использовать. Wire fixture и no-forward probe проверяют типы, полный denylist, activation hashes и отсутствие дополнительных instructions/data в `description`.

Каждая кампания получает уникальный `project_id`; её итерации сохраняют scope. Разные business/evidence cases никогда не делят project scope. Memory не является источником продуктовых фактов или active rules.

### 7.6. Два MCP tools

Успешный `initial`/`revision`/`rule_proposal` run содержит ровно две **логические** доменные операции: один `context_get` и один `draft_save`. Допустимый transport retry использует тот же operation/idempotency key, не считается новой доменной операцией и не может создать второй persisted draft. Evidence хранит отдельно logical operation IDs и physical attempt count.

#### `cf_context_get`

Input:

```json
{
  "campaign_id": "cmp_uuid",
  "operation": "initial|revision|rule_proposal",
  "iteration": 1,
  "context_version": "optional expected sha256",
  "idempotency_key": "string"
}
```

Поведение:

- проверяет ownership/task/campaign/state;
- повторно запускает общий BriefValidator;
- при неготовности возвращает `ready=false`, typed questions и terminal instruction;
- при готовности возвращает compact ContextBundle и JSON Schema нужного envelope;
- для revision добавляет previous package hash, feedback, `allowed_changed_paths`, protected paths/facts/policies, hashes неизменяемых artifacts и schema `CommunicationPatch`;
- для rule proposal добавляет feedback, selected scope, current rules и закрытый DSL schema;
- read-only/idempotent, timeout initial target ≤3 s;
- публикует безопасные stage receipts без chain-of-thought.

#### `cf_draft_save`

Input:

```json
{
  "campaign_id": "cmp_uuid",
  "operation": "initial|revision|rule_proposal",
  "iteration": 1,
  "context_version": "sha256",
  "idempotency_key": "string",
  "draft": {"kind": "communication_bundle|communication_patch|rule_proposal", "...": "..."}
}
```

Поведение:

- принимает discriminated strict DraftEnvelope;
- разрешает ровно один persisted agent draft на operation/iteration;
- проверяет task/campaign/project ownership, ids, state, context hash, size/schema;
- для revision принимает только `CommunicationPatch`, проверяет base hash/feedback/changed paths, выполняет deterministic merge и отклоняет full-package replacement;
- при попытке изменить что-либо вне scope возвращает `REVISION_SCOPE_VIOLATION` без сохранения v2;
- сохраняет immutable version только после merge/protected checks/full QA;
- независимо извлекает claims и сверяет их с model-declared evidence/FactLedger;
- выполняет deterministic QA либо rule target/conflict/regression;
- создаёт safe renderer/diff/summary;
- не вызывает LLM, не исправляет содержание и не активирует rule;
- timeout initial target ≤5 s.

### 7.7. Safe trace и доказательство агентности

Хранить и показывать:

- app git SHA;
- Ouroboros tag/full SHA;
- task/project/campaign/operation/iteration IDs;
- skill version/hash/review status;
- exact prefixed MCP tool sequence/duration/status;
- model route, tokens/cost при наличии;
- execution mode, total duration, terminal outcome;
- safe stage receipts:
  - `brief_validated`;
  - `context_version_bound`;
  - `product_facts_loaded`;
  - `segment_loaded`;
  - `touch_policy_applied`;
  - `active_rules_loaded`;
  - `bundle_received`;
  - `schema_validated`;
  - `grounding_checked`;
  - `channel_policy_checked`;
  - `qa_completed`;
  - `version_persisted`.

Receipt содержит timestamp, duration, input/output hashes, safe source/policy/rule versions и status. Не хранить chain-of-thought, system prompt, raw context, Authorization, secrets или full provider response.

### 7.8. Машинный запрет backend LLM

CI/architecture test падает, если:

- backend импортирует OpenAI/OpenRouter/Anthropic/GigaChat/provider SDK;
- backend содержит прямой LLM/provider endpoint;
- provider keys передаются в backend container;
- domain/MCP module имеет provider abstraction или генерирует content через сеть;
- fallback вызывает LLM.

Provider credentials получает только Ouroboros container. Test допускает provider SDK только в независимых developer tools, не попадающих в runtime image, при явном allowlist и отсутствии доступа из app.

---

## 8. Prompt и generative policy

### 8.1. Модельная маршрутизация

Начальный профиль, который обязан подтвердить preflight:

- primary обычных/live cases: доступный low-latency `gpt-5.4-mini` route внутри Ouroboros;
- `gpt-5.4`: выключен по умолчанию; только отдельный явный golden/comparator run с opt-in и token cap, если latency/SLO позволяют;
- OpenRouter: выключенный по умолчанию резервный route внутри Ouroboros; баланс ключа не считается разрешением тратить;
- deterministic template: последний non-LLM fallback.

Публичные provider rate limits, owner-specific бесплатные allowances и расход конкретного проекта — разные сущности. Текущие значения владельца не являются стабильным продуктовым контрактом: они живут в ignored workstation-файле `operator-limits.local.yaml`, а на VPS отдельно передаются в `/home/dmitry/secrets/communication-factory/operator-limits.yaml`. Live preflight читает этот файл; приложение, evidence schemas и `spec_constants.yaml` не хардкодят его числа. Локальный usage не даёт права заявлять account-wide remaining quota, поскольку тот же ключ может использоваться вне проекта.

### 8.1.1. Project-local live-run budget control

Используется встроенный budget layer закреплённого Ouroboros: persistent `TOTAL_BUDGET`, reported token/cost counters, per-task cost controls и retry/deadline bounds. Проект не добавляет account-wide rolling-window quota service, provider proxy или patch ядра Ouroboros только ради metering. Такая архитектурная надстройка допустима лишь при доказанном blocker, ADR и явном owner approval.

Каждый paid smoke/pilot/comparator/evaluation run требует положительные `EVAL_MAX_TOKENS` и `EVAL_MAX_COST_USD`. Это project-local run caps, а не утверждение о provider-account balance. Для `gpt-5.4` дополнительно обязательны `ALLOW_GPT54_COMPARATOR=true`, новый `COMPARATOR_RUN_ID` и отдельный cap из operator profile.

Перед full basket выполняются один smoke и 2–3 pilot cases. По ним runner строит conservative projection input + maximum output + configured retries + safety/post-task allowance. Run блокируется до первого case, если projection превышает supplied caps или project-local allowance из operator file с учётом наблюдаемого Ouroboros usage.

Full live evaluation выполняется последовательно (`EVAL_CONCURRENCY=1`). После каждого case сохраняются provider-reported tokens/cost по model/category, если runtime их раскрывает. До следующего case runner проверяет headroom текущего run и project-local planning allowance. Missing/malformed usage не заменяется выдуманными числами: accounting получает `unknown`, run expansion прекращается, а такой run не может считаться budget-compliant.

Этот boundary намеренно останавливает работу между cases и не притворяется атомарным account-wide pre-call quota. Внутри case риск ограничивают measured pilot projection, Ouroboros `TOTAL_BUDGET`, task cost ceiling, retry bounds и `<30 s` terminal limit. Budget exhaustion — controlled outcome; он не запускает retry, stronger-model, OpenRouter или иной provider fallback. Deterministic template после такого исхода маркируется fallback и не считается live evidence.

`make budget-status` read-only разделяет owner assumptions, project-observed usage, current-run usage и неизвестный account-wide remainder. `make test-budget` покрывает missing/invalid operator config/caps, projection rejection, sequential case-boundary stop, missing usage, comparator opt-in и запрет provider switching.

OpenRouter активируется только отдельным owner-approved provider profile с exact model ID, собственным secret mount, per-run/daily token cap, dollar cap и smoke. Usage учитывается отдельно по provider/model/run; автоматическое включение при исчерпании OpenAI quota/cap запрещено.

### 8.2. Assembled prompt

Body `ouroboros/skills/communication_factory/SKILL.md` — единственный редактируемый, цельный русский prompt. `prompts/communication_factory.ru.md` является generated byte-equal projection для review/tests и не используется как второй источник. Документ содержит:

1. роль и цель;
2. границы автономии;
3. два tools и operation sequence;
4. context → one typed draft → save;
5. grounding/channel/feedback/rule rules;
6. output schema;
7. no-send/no-approval/no-repair/no-reasoning.

Task adapter помещает body целиком в authoritative `task_contract.constraints`; `description` содержит только идентификаторы operation/iteration и просьбу исполнить contract, не дублируя правила. Context, feedback и active rules передаются отдельными JSON blocks как untrusted data через `cf_context_get`. Запрещены mixed-language scaffolding, повтор правил, внутренние pipeline labels, empty sections, stale placeholders и лишние длинные исходники.

После изменения prompt:

- перечитать assembled text целиком;
- пересоздать generated projection и проверить byte-equality;
- инвалидировать старый review/contract lock, пройти fresh review и повторный activation/tool probe;
- выполнить normal, edge, injection, revision и rule cases;
- прочитать фактические outputs;
- зафиксировать prompt hash и qualitative observations.

### 8.3. Запрет скрытого repair

Запрещены:

- автоматическое «исправь findings» после QA;
- semantic reviewer/rewrite;
- strong-model repair;
- self-critique/rejudge/rephrase;
- schema repair с новой генерацией;
- best-of-N/parallel variants;
- built-in acceptance improvement;
- изменение уже persisted draft.

Разрешены:

- deterministic Unicode/whitespace/punctuation/brand-casing normalization до hash;
- safe HTML escaping/rendering;
- один transport retry только при доказанном отсутствии принятого provider response, с тем же idempotency key и внутри deadline;
- новая генерация после явного user action `revision` или `rule_proposal`.

---

## 9. Доменные модели

Публичные модели версионируются, имеют strict JSON Schema/Pydantic validation, запрещают неизвестные critical fields и доступны в OpenAPI/generated frontend types.

### 9.1. `CampaignBriefDraft` и `ReadyCampaignBrief`

API принимает и сохраняет семантически неполный черновик, не превращая обычное отсутствие бизнес-поля в transport `422`.

`CampaignBriefDraft`:

```yaml
campaign_id: string                 # server-generated
name: string|null
objective: string|null
product_id: string|null
segment_id: string|null
trigger_id: string|null
channels: [sms|email]               # default empty list
cta_label: string|null
cta_url: string|null
tone: string|null
offer_period: {start: date|null, end: date|null}|null
notes: string|null
synthetic: true
version: integer
input_hash: sha256
```

`ReadyCampaignBrief` — immutable validated snapshot:

```yaml
campaign_id: string
name: string
objective: string
product_id: string
segment_id: string
trigger_id: string
channels: [sms|email]               # non-empty, unique
cta_label: string
cta_url: uri
tone: string
mandatory_fact_ids: [string]
mandatory_concept_ids: [string]
prohibited_claim_ids: [string]
legal_policy_id: string
contact_policy_id: string
offer_period: {start: date|null, end: date|null}
notes: string
synthetic: true
version: integer
input_hash: sha256
```

Процесс:

```text
CampaignBriefDraft
→ deterministic semantic validation
→ NEEDS_INPUT + typed questions либо ReadyCampaignBrief
→ immutable ready brief snapshot
→ ContextBundle
```

Ограничения:

- malformed JSON, неверный primitive type и size limit могут дать transport/schema `4xx`;
- отсутствующий CTA/product/audience/objective/channel — валидный draft и доменный `NEEDS_INPUT`, а не Pydantic/FastAPI `422`;
- CTA URL в ready model проходит URL validation и synthetic allowlist/reserved-domain check;
- notes — untrusted data;
- `synthetic=true` неизменяемо;
- ready promotion атомарна и сохраняет source draft version/hash;
- агент получает только `ReadyCampaignBrief`; draft/NEEDS_INPUT никогда не запускает generation task;
- critical change инвалидирует старые context/QA/approval.

### 9.2. `ProductFactCard` и `FactLedgerItem`

`ProductFactCard`:

- product id/version/exact name;
- allowed claim/fact IDs;
- required disclaimer IDs;
- prohibited claim IDs;
- allowed CTA URLs/hosts;
- eligibility/active-product policy;
- connection/activation concept IDs;
- validity interval;
- `synthetic=true`.

`FactLedgerItem`:

```yaml
fact_id: string
source_id: string
kind: text|number|percentage|money|date|duration|url|condition|concept
canonical_text: string
normalized_value: scalar|object
allowed_surface_forms: [string]
valid_from: datetime|null
valid_to: datetime|null
synthetic: true
```

Любой значимый факт имеет stable ID. Сырые product tables не передаются модели.

### 9.3. `PersonaContext`

Только безопасные синтетические/агрегированные признаки:

- тип и стадия бизнеса;
- диапазон размера компании;
- region category;
- подключённые synthetic products;
- trigger/lifecycle event;
- агрегированная история касаний;
- channel preference/consent;
- contact pressure/frequency cap;
- tone preference;
- synthetic needs/signals.

Запрещены реальные ФИО, телефон, e-mail, ИНН, account IDs, пол, возраст, психотип, здоровье, национальность и иные sensitive/дискриминационные признаки.

### 9.4. `ContextBundle`

```yaml
context_version: sha256
operation: initial|revision|rule_proposal
brief_snapshot: ReadyCampaignBrief
product: ProductFactCard
facts: [FactLedgerItem]
persona: PersonaContext
touch_history: object
contact_policy: object
channel_policies: object
legal_policy: object
active_rules: [RuleVersion]
source_manifest: [object]
prompt_version: string
rules_version: string
content_plan:
  selected_fact_ids: [string]
  selected_concept_ids: [string]
  available_optional_fact_ids: [string]
  available_optional_concept_ids: [string]
  selection_sources: [base_policy|feedback|rule]
  applied_rule_version_ids: [string]
previous_package: CommunicationBundle|null
feedback: Feedback|null
allowed_changed_paths: [json_pointer]
protected_paths: [json_pointer]
protected_hashes: object
output_schema_id: string
```

Каждый блок явно помечен как untrusted data. Context ограничен данными конкретной кампании и имеет size budget. FactLedger определяет, что фактологически допустимо вообще; `content_plan` детерминированно выбирает, что должно войти в текущий draft. Skill не добавляет в текст unselected optional fact/concept. Feedback может расширить plan только для revision, а approved rule — для следующего matching case; source и `rule_version_id` всегда доказуемы.

### 9.5. `DraftEnvelope`

```yaml
kind: communication_bundle|communication_patch|rule_proposal
schema_version: string
campaign_id: string
operation: initial|revision|rule_proposal
iteration: integer
context_version: sha256
payload: CommunicationBundle|CommunicationPatch|RuleProposal
```

Discriminator contract:

- `operation=initial` → `kind=communication_bundle`;
- `operation=revision` → `kind=communication_patch`;
- `operation=rule_proposal` → `kind=rule_proposal`.

Любая другая комбинация отклоняется до persistence.

### 9.6. `CommunicationBundle`

```yaml
summary: string
personalization_rationale: [string]
sms: SmsArtifact|null
email: EmailArtifact|null
channel_suppressions: [ChannelSuppression]
claim_evidence: [ClaimEvidence]
warnings: [string]
```

`SmsArtifact`:

- `text`;
- `cta_url`;
- `fact_refs[]`;
- `personalization_refs[]`.

Encoding, length и segments вычисляет backend. Модель не является source of truth этих полей.

`EmailArtifact`:

- subject;
- preheader;
- headline;
- structured sections;
- CTA label/URL;
- disclaimer IDs;
- plain-text intent/content;
- fact refs и personalization refs по sections.

Модель не возвращает произвольный raw HTML. Backend создаёт deterministic sanitized responsive HTML из structured blocks.

### 9.7. `CommunicationPatch`

Revision agent возвращает не полный replacement package, а только typed patch:

```yaml
base_package_hash: sha256
feedback_id: string
changed_paths: [json_pointer]
sms: SmsArtifact|null
email: EmailArtifact|null
claim_evidence: [ClaimEvidence]
warnings: [string]
```

Backend выполняет единственный authoritative merge:

```text
load immutable v1
→ compare base_package_hash
→ verify feedback_id and exact changed_paths
→ require changed_paths ⊆ allowed_changed_paths
→ merge supplied channel artifacts only at declared paths
→ verify protected paths/hashes and unchanged siblings
→ rebuild complete CommunicationBundle
→ full grounding + deterministic QA
→ save immutable v2 + diff
```

Пустой patch, stale base, несовпадение actual diff/`changed_paths`, full-package replacement или изменение вне scope возвращает typed `REVISION_SCOPE_VIOLATION`/`STALE_BASE_PACKAGE`; v2 не сохраняется. Claim evidence пересчитывается/проверяется для полного merged package, а не только изменённого fragment.

### 9.8. `ClaimEvidence`

```yaml
claim_id: string
channel: sms|email
artifact_path: json_pointer
text_fragment: string
claim_type: text|number|percentage|money|date|duration|url|condition|concept
normalized_value: scalar|object|null
fact_id: string
source_id: string
```

Указанный fragment должен реально присутствовать в фактическом output/path. Model-declared claims не заменяют независимый backend extraction.

### 9.9. `Finding` и `QualityReport`

`Finding`:

```yaml
finding_id: string
check_id: string
severity: BLOCKER|WARNING|INFO
artifact: brief|sms|email|package|rule
path: json_pointer|null
quote: string|null
expected: string|null
actual: string|null
source_ids: [string]
recommendation: string
checker: deterministic|human
status: OPEN|FIXED|ACCEPTED|RECHECKED
blocking: boolean
```

`QualityReport`:

- report/check registry versions;
- findings;
- approvable boolean;
- checked facts/claims/policies;
- SMS metrics;
- deterministic score как вспомогательная величина;
- evidence hashes.

Semantic/LLM finding в P0 отсутствует. Human note не становится blocker без фактической policy/rule.

### 9.10. `Feedback`

```yaml
feedback_id: string
campaign_id: string
package_version: integer
artifact_path: json_pointer
comment: string
scope: CURRENT_FIELD|CURRENT_CHANNEL|PACKAGE
author_role: editor|approver
created_at: datetime
```

Feedback — untrusted user data и не может отменить protected legal/contact/fact rules. Сохранение feedback никогда автоматически не запускает RuleProposal task.

RuleProposal стартует только отдельным подтверждённым `POST /api/v1/feedback/{feedback_id}/rule-proposals` с typed request `{selected_scope}`; `feedback_id` берётся из path и фиксируется как source. Любой UI `rule_creation_intent` до подтверждения является transient presentation state и не частью `Feedback` domain model.

### 9.11. `RuleProposal` и `RuleVersion`

Разрешённые types:

- `forbid_phrase`;
- `require_phrase`;
- `require_fact`;
- `require_concept_id`;
- `tone_hint`.

Schema:

```yaml
proposal_id: string
source_feedback_id: string
type: enum
scope:
  product_ids: [string]
  channel: sms|email|null
  segment_ids: [string]
condition_id: string|null
value: string
rationale: string
target_case_ids: [string]
base_rules_version: string
candidate_rules_version: string
risk: low|medium
```

Нет global scope, arbitrary regex, code, tool instructions, secrets, paths, URLs или free-form executable conditions. `require_concept_id` ссылается на allowlisted concept с deterministic accepted surface forms/checker; LLM не используется для проверки применения.

`RuleVersion` добавляет status, human decision, target/conflict/regression results, activation timestamp, hash и previous version. Rollback создаёт новую immutable active pointer/version event, не переписывает историю.

### 9.12. `RunRecord`

- run/operation/campaign/project/task IDs;
- `mode`: `live_ouroboros|deterministic_template|replay|validation_only|mock`;
- status/reason code;
- task/skill/MCP/model provenance;
- context/prompt/rules/schema versions;
- lifecycle timestamps including result persisted, terminal and worker released;
- user-visible/stage/full-worker-occupancy latency;
- provider-call ledger and main/safety/post-task/retry tokens/cost;
- saved draft hash before/after post-task processing;
- logical operation IDs, physical attempt counts и one saved draft ID/hash;
- fallback/replay source reason;
- safe trace refs;
- terminal outcome.

---

## 10. Состояния и инварианты

### 10.1. Campaign/package states

```text
DRAFT
  ├─► NEEDS_INPUT
  └─► READY
        └─► QUEUED
              └─► RUNNING
                    ├─► REVIEW_REQUIRED
                    ├─► APPROVABLE
                    ├─► BLOCKED
                    ├─► NOT_APPLICABLE
                    ├─► FAILED
                    └─► CANCELLED

REVIEW_REQUIRED
  └─► revision operation → новая immutable version

APPROVABLE
  ├─► human APPROVED
  └─► human ACCEPTED_WITH_WARNING   # только explicit warning acknowledgements

APPROVED/ACCEPTED_WITH_WARNING
  └─► explicit export action → EXPORTED
```

Инварианты:

- `NEEDS_INPUT` не запускает LLM;
- один active run per campaign;
- terminal run не меняется задним числом;
- retry/rerun — новый RunRecord/operation;
- approval привязан к exact package hash/context/rules versions;
- любая новая package version или изменение critical brief/context/rule инвалидирует approval и требует нового review/decision;
- approval и export — разные audit events; approval не создаёт ZIP автоматически;
- `ACCEPTED_WITH_WARNING` хранит acknowledged warning IDs и запрещён при любом BLOCKER;
- blocker нельзя approve;
- состояния `SENT` не существует.

### 10.2. Rule states

```text
PROPOSED
  ├─► VALIDATION_FAILED
  ├─► READY_FOR_APPROVAL
  │      ├─► APPROVED
  │      └─► REJECTED
  └─► CANCELLED

APPROVED ─► ROLLED_BACK
```

Agent не активирует, не отклоняет и не откатывает rule.

### 10.3. Idempotency

- mutating HTTP/MCP action требует `Idempotency-Key`;
- key связан с request hash;
- повтор с тем же body возвращает исходный operation/result;
- тот же key с другим body → `409`;
- unique `(campaign_id, operation, iteration, generation_slot)` не допускает два persisted drafts;
- unknown task outcome сначала reconciles через Task API и persisted save receipt;
- late task result не перезаписывает уже terminal deterministic fallback.

---

## 11. REST/SSE и экспорт

### 11.1. Минимальные REST endpoints

```text
GET    /api/v1/health
GET    /api/v1/ready
GET    /api/v1/config/public
GET    /api/v1/cases
POST   /api/v1/campaigns
GET    /api/v1/campaigns/{id}
PATCH  /api/v1/campaigns/{id}/brief
POST   /api/v1/campaigns/{id}/validate
POST   /api/v1/campaigns/{id}/answers
POST   /api/v1/campaigns/{id}/runs
GET    /api/v1/runs/{id}
GET    /api/v1/runs/{id}/events
POST   /api/v1/runs/{id}/cancel
POST   /api/v1/packages/{id}/feedback
POST   /api/v1/packages/{id}/revision
POST   /api/v1/feedback/{id}/rule-proposals
GET    /api/v1/rule-proposals/{id}
POST   /api/v1/rule-proposals/{id}/approve
POST   /api/v1/rule-proposals/{id}/reject
POST   /api/v1/rules/{id}/rollback
POST   /api/v1/packages/{id}/approve
POST   /api/v1/packages/{id}/export
GET    /api/v1/exports/{id}
GET    /api/v1/evaluation/runs
GET    /api/v1/evaluation/runs/{id}
```

Approval endpoints требуют human web session/action и exact hash confirmation. Package approve body содержит decision `APPROVED|ACCEPTED_WITH_WARNING` и, во втором случае, точный набор acknowledged warning IDs. Export вызывается отдельным endpoint только для всё ещё действующей approved version. Agent token/MCP не имеет authority вызвать их.

### 11.2. Domain SSE

UI не подключается напрямую к Ouroboros. Backend нормализует события:

```text
run.accepted
run.started
run.stage
run.task_bound
run.tool_started
run.tool_completed
run.qa_completed
run.terminal
package.version_created
rule.proposed
rule.tested
rule.approved
export.ready
```

SSE имеет monotonic event ID, reconnect/resume, heartbeat и terminal close. Reconnect не повторяет действия.

### 11.3. Campaign export

ZIP только для human-approved version:

```text
campaign.json
brief.json
run.json
context-manifest.json
fact-card.json
rules-version.json
sms/message.txt
sms/metrics.json
email/email.html
email/email.txt
email/content.json
qa/findings.json
qa/report.html
feedback/feedback.json
feedback/diff.json
learning/rule-proposal.json
trace/safe-events.jsonl
trace/mcp-calls.jsonl
trace/model-usage.json
manifest.json
README.txt
```

Manifest содержит file checksums, versions, synthetic/no-send notice и approval hash. Никаких secrets, raw prompts, reasoning или source archive.

---

## 12. Функциональные требования

### FR-01. Бриф и уточнения

- ручное создание, выбор/клонирование synthetic case как `CampaignBriefDraft`;
- semantically incomplete draft сохраняется и возвращается как domain state, а не transport validation error;
- required/conflict/eligibility/contact validation без LLM;
- максимум пять коротких вопросов за шаг;
- вопрос содержит missing/conflict field, причину и при возможности safe answer options;
- prepared synthetic answers в demo имеют audit event;
- при `NEEDS_INPUT` generation disabled.

Acceptance:

- пропуск required CTA/fact/policy выявлен;
- конфликт brief/fact выявлен;
- LLM/token usage равен нулю;
- после valid answer создаётся новая draft version и атомарный immutable `ReadyCampaignBrief` snapshot;
- generation API невозможно вызвать, пока ready snapshot отсутствует;
- contract test доказывает: missing CTA → `NEEDS_INPUT`, не FastAPI/Pydantic `422`.

### FR-02. Сбор контекста

- product facts, persona, history, contact/channel/legal policies и active rules имеют IDs/versions/retrieved_at/synthetic;
- opt-out/contact block имеет приоритет;
- уже активный продукт приводит к `NOT_APPLICABLE`;
- context compact и campaign-isolated;
- каждый fact/source отображается в UI.

### FR-03. Генерация SMS

- один primary structured SMS;
- только allowed facts/CTA/policies;
- русский естественный текст;
- backend вычисляет GSM-7 basic/extension либо UCS-2, code units, chars и segments;
- configurable max segments/length;
- channel suppression вместо выдуманного сообщения, если SMS запрещён.

Проверки:

- exact product naming;
- CTA/allowed URL;
- mandatory fact/concept/disclaimer/label policy fixture;
- prohibited/absolute/guaranteed claims;
- unknown numeric/date/money values;
- whitespace/placeholders/PII;
- fact/personalization refs;
- contact/frequency policy.

### FR-04. Генерация e-mail

- subject, preheader, headline, structured sections, CTA, disclaimer IDs и plain text;
- deterministic safe responsive HTML renderer;
- desktop/mobile preview;
- channel suppression, если e-mail запрещён.

Проверки:

- subject/preheader lengths;
- required sections;
- CTA/HTTPS/allowed host/UTM policy;
- disclaimer;
- facts/grounding;
- HTML sanitation/CSP-safe output;
- placeholders/PII;
- accessibility для включённых декоративных/контентных элементов.

### FR-05. Двусторонний grounding

1. Ouroboros возвращает `claim_evidence[]`.
2. Backend проверяет fragment/path/fact/source existence.
3. Backend независимо извлекает actual number/percentage/money/date/duration/URL/condition claims из SMS и e-mail.
4. Model-declared, actual-extracted и FactLedger сравниваются.
5. Missing declaration, false reference или unsupported actual claim → `BLOCKER`.
6. UI показывает claim → fact/source mapping.

Approved basket должен иметь ноль unsupported numeric/date/money/URL claims.

### FR-06. QA

- deterministic registry с version/hash;
- finding всегда содержит evidence;
- `BLOCKER` запрещает approval;
- `WARNING` требует видимого acknowledgement человеком;
- `INFO` не блокирует;
- после revision создаётся новый report, старый immutable;
- QA не вызывает LLM.

### FR-07. Feedback/revision/diff

- пользователь выбирает artifact path, comment и scope;
- revision — новый task/iteration;
- context включает previous hash, feedback, allowed/protected paths;
- неизменённые artifacts копируются backend без LLM;
- агент возвращает только `CommunicationPatch`, никогда полный replacement `CommunicationBundle`;
- backend проверяет `base_package_hash`, `feedback_id`, declared/actual `changed_paths`, затем выполняет deterministic merge;
- save отклоняет out-of-scope mutation с `REVISION_SCOPE_VIOLATION`, stale base — с `STALE_BASE_PACKAGE`;
- protected paths и все неизменённые siblings совпадают byte/semantic hash;
- создаются v2, word/structural diff и новый QA;
- UI показывает что изменено, что защищено и применён ли feedback.

### FR-08. Governed reusable rule

- rule proposal запускается отдельной кнопкой после сохранённой revision;
- feedback schema не содержит auto-trigger flag; один только feedback никогда не создаёт agent task;
- новый Ouroboros task использует те же два tools с `operation=rule_proposal`;
- proposal строго соответствует allowlisted DSL;
- backend выполняет schema, protected-policy conflict, target test, 3–5 regression fixtures и минимум один out-of-scope negative case;
- человек видит scope/value/tests/diff и approve/reject;
- approved rule создаёт immutable version;
- будущий explicit run получает active rule через context;
- rollback доступен;
- никакого executable code/global scope/automatic activation.

### FR-09. Approval/no-send/export

- approval disabled при blocker/stale context/hash;
- warning может быть принят только явным `ACCEPTED_WITH_WARNING` с warning IDs;
- approval и export — разные действия/events; новая package version инвалидирует старый approval;
- UI явно пишет «Утверждение не означает отправку»;
- agent/MCP не может approve;
- P0 не содержит SMTP/SMS gateway/Sendsay/CRM send integration;
- approved package экспортируется в ZIP с manifest/checksums.

### FR-10. Evaluation/evidence

- fixed synthetic dataset и expected assertions;
- 15 business cases;
- ≥10 new live Ouroboros runs, target 12;
- 5 separate chaos/security cases;
- live batch requires explicit opt-in/new evaluation ID/provider profile/spend cap/empty immutable directory;
- `verify*` validates existing evidence and never invokes provider;
- live/replay/template/validation/mock не смешиваются;
- failed runs не удаляются;
- report/evidence доступны offline в PDF/JPG/HTML/CSV/JSON/JSONL;
- six-case human qualitative review.

---

## 13. Детерминированный QA registry

Обязательные checks:

1. Brief required fields.
2. Brief/fact conflicts.
3. Eligibility/product already active.
4. Contact consent/channel permission/frequency cap.
5. Exact product name.
6. Numeric/percentage/money/date/duration allowlist.
7. Allowed HTTPS URL/host/UTM.
8. Required disclaimer/legal/channel label policy.
9. Forbidden/absolute/guaranteed claims.
10. Mandatory CTA.
11. Required fact/concept application.
12. SMS encoding/segments/length.
13. E-mail structure/subject/preheader.
14. HTML sanitation and unsafe URI.
15. Placeholder/control-label detection.
16. PII/internal-domain/secret-pattern detection.
17. Model-declared claim fragment/path/ref consistency.
18. Actual claim extraction and FactLedger match.
19. Personalization refs allowed and visibly used.
20. Revision allowed/protected paths.
21. Active rule application and scope.
22. Approval/export state integrity.

Channel/legal rules являются versioned synthetic policy fixtures. Например, `ad_label` может требовать заданную маркировку/наименование только в конкретном synthetic case; система не заявляет универсальную юридическую интерпретацию без подтверждённого policy source.

Approval определяется отсутствием open blockers, а не общим score.

---

## 14. Синтетические данные и test basket

### 14.1. Data factory

Создать минимум:

- 5–6 fictional products;
- 8–10 safe business segments/personas;
- touch histories и contact states;
- versioned fact cards;
- channel/legal/brand policy fixtures;
- allowed concept catalog;
- fixed seed;
- reserved domains `.invalid`/`.test`, fictional phones/e-mails;
- явный `synthetic=true` и UI/report badge.

Не копировать реальные product tables, дисклеймеры, URLs, названия компаний/клиентов или legal copy из архива. Допустимы обобщённые категории вроде «зарплатный проект», но все конкретные условия/брендинг вымышлены.

### 14.2. 15 business-process cases

| ID | Сценарий | Ожидаемый результат | Live target |
|---|---|---|---:|
| B01 | Demo: зарплатный проект, missing CTA, injection note; затем prepared answer | NEEDS_INPUT → live v1; injection ignored; optional online-bank concept absent by initial content plan | yes |
| B02 | Полный обычный бриф без learned rule | SMS+email grounded/approvable | yes |
| B03 | Похожий case после approved `require_concept_id` | Rule applied without repeated feedback; target pass; `rule_version_id` evidenced | yes |
| B04 | Разрешённый срок/число в fact card | Exact grounded value | yes |
| B05 | Неподтверждённое «99%/мгновенно» в notes | Claim excluded; no unsupported actual claim | yes |
| B06 | Обязательный synthetic disclaimer/label | Required policy text/ref present | yes |
| B07 | CTA URL/UTM policy | Only allowed HTTPS URL/host/UTM | yes |
| B08 | Кириллица/emoji/SMS boundary | Correct UCS-2/GSM metrics and segments | yes |
| B09 | SMS запрещён, e-mail разрешён | SMS SUPPRESSED, grounded e-mail | yes |
| B10 | E-mail запрещён, SMS разрешён | E-mail SUPPRESSED, grounded SMS | yes |
| B11 | Оба канала запрещены | Controlled BLOCKED, no LLM draft | no |
| B12 | Продукт уже подключён | NOT_APPLICABLE, no generation | no |
| B13 | Eligibility/critical fact отсутствует | NEEDS_INPUT, no generation | no |
| B14 | Prompt injection внутри brief/history | Live package follows skill/policy only | yes |
| B15 | Targeted feedback/revision | v2 changes only allowed paths; diff/QA pass | yes |

Минимум 10 фактических business cases должны иметь `mode=live_ouroboros`. Если любой target case завершился template/replay/failure, добавить новые normal live cases до фактических десяти. Validation-only controlled outcomes остаются business evidence, но не входят в live count/latency.

B01 feedback и B03 образуют before/after learning proof только при следующем зафиксированном контракте:

1. `connection_via_online_bank` присутствует в B01 FactLedger как разрешённый optional concept, но не является base-policy requirement.
2. Initial B01 `content_plan.selected_concept_ids` его не содержит; skill обязан соблюдать plan, поэтому v1 детерминированно не упоминает concept. Это expected assertion, а не выбранный постфактум удачный sample.
3. Feedback явно требует concept; revision context добавляет его в selected plan, v2 содержит его, а diff показывает только разрешённые пути.
4. Separate RuleProposal создаёт `require_concept_id(connection_via_online_bank)` со scope `product=synthetic_payroll`, `channel=email`; human-approved version имеет stable `rule_version_id`.
5. B03 matching e-mail получает concept без повторного feedback; context/package/report показывают applied `rule_version_id`.
6. Negative fixture с другим product либо SMS-only channel не применяет правило.

Если v1 нарушил initial content plan или уже содержит concept, B01 attempt считается failed assertion и не используется как learning demo. Нельзя cherry-pick output или выдавать base policy за learned rule.

### 14.3. Отдельные chaos/security cases

| ID | Fault | Expected |
|---|---|---|
| X01 | Ouroboros unavailable/admission failure | terminal degraded/failed `<30 s`, no crash |
| X02 | MCP timeout/malformed payload | typed failure, cancel/reconcile, no duplicate |
| X03 | Provider 429/timeout | bounded provider routing/cancel/template, marked |
| X04 | Duplicate click/request | one billable generation/save |
| X05 | App restart/stale RUNNING | reconcile to honest terminal state |

Chaos timings, fallback rates и outcomes публикуются отдельно от normal live metrics.

### 14.4. Human qualitative review

После frozen live run Codex формирует минимум шесть immutable representative review packets без предзаполненных оценок. Затем человек полностью читает их и оценивает 1–5:

- ясность;
- естественность русского языка;
- соответствие segment/brief;
- полезность персонализации;
- убедительность без давления;
- отсутствие шаблонности/странностей;
- согласованность SMS/e-mail;
- пригодность для демонстрации после минимальной редакторской правки.

Сохранить evaluator role/name или обезличенный ID, rubric, scores, comments и aggregate. Это `manual measured`, не LLM-as-a-judge. `schema valid` недостаточно. До человеческого ввода соответствующий gate имеет status `WAITING_FOR_OPERATOR`; test actor не заменяет reviewer.

---

## 15. UI/UX

### 15.1. Дизайн-принципы

- светлый спокойный enterprise-интерфейс;
- русский язык;
- один главный action на состоянии;
- без emoji как основных иконок;
- restrained green accent, но без копирования защищённого branding;
- карточки/таблицы/trace только когда помогают задаче;
- WCAG AA contrast, keyboard navigation, visible focus, semantic labels;
- desktop-first demo, рабочий responsive tablet/mobile;
- loading/error/empty/degraded/stale states;
- badge «Все данные синтетические» и «Отправка отключена».

`design-system/MASTER.md` фиксирует typography, spacing, colors, components, states, charts и accessibility. UI/UX Pro Max используется только design-time после review.

### 15.2. Экран 1 — Cases / Dashboard

- 15 business cards/table и отдельный chaos tab;
- expected/actual outcome, mode, last run, latency, QA;
- live count, p50/p95, crash/timeout, cost;
- synthetic/no-send badges;
- быстрый выбор B01 demo.

### 15.3. Экран 2 — Campaign Workspace

Трёхзонная раскладка:

- слева: stepper, brief, questions/answers, context summary;
- центр: SMS/e-mail/fact card/claims/QA/diff/rule tabs;
- справа: safe Ouroboros trace и run metadata.

Показывать:

- versions/hashes/source chips;
- SMS encoding/segments;
- sanitized e-mail preview;
- claim → fact mapping;
- blocker evidence и disabled approval reason;
- feedback form/scope;
- changed/protected paths;
- rule target/regression/out-of-scope;
- human approvals и export.

### 15.4. Экран 3 — Evaluation

- frozen run selector;
- business и chaos отдельно;
- case-by-case assertions/artifacts;
- live/mode/status/tokens/cost;
- normal p50/p95/max;
- qualitative rubric;
- measured/assumed/hypothesis;
- report links/checksums.

### 15.5. Экран 4 — Diagnostics

- app/DB/Ouroboros/MCP/skill/provider readiness;
- pinned tag/SHA/skill hash/discovered tools;
- public-safe config only;
- circuit breaker/queue;
- latest errors/recovery actions;
- никакого secret/raw settings/raw prompt.

### 15.6. UX acceptance

- no console errors;
- every async action has progress and cancel/recovery;
- no endless spinner;
- direct URL/reload works;
- stale data indicated;
- destructive/approval actions confirm exact version/hash;
- main demo flow читаем при 1080p/90–100% zoom;
- Playwright covers happy, needs-input, blocker, revision/rule, degraded and export.

---

## 16. Security, privacy and licensing

### 16.1. Secrets

- `.env`, key files, databases, logs and artifacts in `.gitignore` as applicable;
- all secret-bearing values in `.env.example` are empty; documented non-secret defaults may be populated;
- no default credentials;
- keys not in source, frontend, image layers, task metadata, prompts, traces, screenshots or exports;
- gitleaks/tree and git-history scan;
- custom patterns for provider keys, bearer tokens, passwords, internal domains and known legacy defaults;
- found secret is removed from history and rotated, not merely ignored.

Owner-provided root `OPENAI_API_KEY.txt` на workstation является только transient transfer source для P0 Ouroboros runtime. Он обязан быть ignored/untracked, содержать одну непустую строку и никогда не выводиться, не diff/stage/archive/upload, не попадать в prompt или image layer и не копироваться вместе с repository.

На VPS единственный ожидаемый owner source — `/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt` вне Git checkout, mode `0600`. Любая копия ключа внутри VPS repository является security violation даже при наличии `.gitignore`. Codex может проверить только presence/form без значения; перемещать, удалять или ротировать owner source без approval нельзя. Обнаружение ключа в Git/history/artifact — stop-the-line и rotation.

До первого Docker build `.dockerignore` обязан исключать `OPENAI_API_KEY.txt`, `OPENROUTER_API_KEY.txt`, `.env` и `secrets/`; security gate проверяет build context, image history и final filesystem без вывода secret values.

Этот ключ не является разрешением аутентифицировать им Codex, запускать прямые SDK experiments или обходить Ouroboros/live-run guards. Model, запускающий сам Codex, относится к control plane и не учитывается runtime usage этого проекта.

Root `.env` может использоваться Docker Compose только как файл подстановки. Host path монтируется read-only только в `ouroboros` как `/run/secrets/openai_api_key`; pinned runtime ожидает `OPENAI_API_KEY`, поэтому entrypoint Ouroboros читает mount без логирования, экспортирует `OPENAI_API_KEY` только внутри service process и затем выполняет `exec` runtime. Plaintext key не хранится в `.env`, Compose config или image. Запрещён общий `env_file` с provider keys для `gateway`, `app` или других контейнеров. Architecture test проверяет итоговый `docker compose config`, mount, entrypoint и фактические env каждого сервиса без вывода значений.

### 16.2. Data governance

- synthetic-only runtime/evidence;
- no source PDFs/transcripts/working XLSX/CSV/real logs in submission repo;
- reserved domains/phones/e-mails;
- no real legal copy, product conditions or tracking URLs;
- redaction at log boundary;
- prompt/output size limits;
- no uploads P0;
- exports scanned before finalization.

### 16.3. Network/application

- only gateway published;
- canonical mapping: host `127.0.0.1:8080` → gateway container `0.0.0.0:8080`;
- app слушает `0.0.0.0:8000` только в private Compose network и не имеет host `ports`;
- Ouroboros слушает `0.0.0.0:8765` только в private Compose network, не имеет host `ports`, а gateway не проксирует его HTTP/WebSocket API;
- non-local Ouroboros bind использует password либо документированный trust flag; canonical private-network profile допускает `OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1` только вместе с network/route tests;
- any host-public exposure requires HTTPS and auth from runtime secret; private container binds are governed by the preceding network policy;
- Ouroboros/MCP/DB private-only;
- same-origin CORS;
- secure cookies/session or bounded Basic Auth profile;
- rate limit login and expensive actions;
- CSP, X-Content-Type-Options, Referrer-Policy;
- e-mail preview sandbox;
- HTML sanitization;
- SSRF/path traversal/zip slip/unsafe URI prevention;
- MCP auth, ownership and strict schemas;
- external URLs never fetched by campaign task.

### 16.4. Prompt injection/tool safety

- skill/system instructions separated from all data;
- brief/catalog/history/feedback treated as untrusted;
- instructions inside data ignored;
- campaign task sees only two allowlisted MCP tools;
- no browser/web/shell/git/file/code/send/approval tools;
- feedback/rule cannot weaken protected facts/legal/contact/safety policies;
- B14 and dedicated security tests required.

### 16.5. Licensing/IP

- dependency lockfiles and license inventory;
- `LICENSE` and `THIRD_PARTY_NOTICES.md`;
- no fonts/images/logos/templates/reference copy without confirmed rights compatible with hackathon terms;
- no paid proprietary component required to run solution except organizer-approved model service access;
- SBOM or machine-readable dependency list;
- source/provenance record for every reused code block/asset;
- final package scan excludes legacy binaries, `.pyc`, generated models/caches and unlicensed assets.

---

## 17. Stability, SLO and recovery

### 17.1. SLO

| Operation | Target |
|---|---:|
| `GET /health` | p95 `<200 ms` |
| create/validate/feedback API до `202` | p95 `<1 s` |
| first domain SSE event | `<1 s` |
| `cf_context_get` | target/timeout `≤3 s` |
| `cf_draft_save` включая QA | target/timeout `≤5 s` |
| managed Ouroboros task | initial hard timeout `25 s` |
| ready brief → terminal package | normal live p95 `<30 s` |
| fault → terminal controlled outcome | `<30 s` |
| deterministic template after degradation decision | `<2 s` target |
| campaign ZIP export | `<5 s` target |

HTTP request не держится открытым до LLM: app сразу сохраняет operation, возвращает `202` и run ID; UI получает progress через SSE/poll recovery.

### 17.2. Измеряемый deadline profile

Стартовый ориентир:

- app validation/context admission: 1–3 s;
- Ouroboros admission/first tool: 3–6 s;
- model generation: 8–13 s;
- save/grounding/QA: 2–5 s;
- finalization/cancel reconciliation: 1–3 s;
- deterministic template при подтверждённой деградации: до 2 s;
- total hard terminal bound: `<30 s`.

`OUROBOROS_SAFETY_CALL_TIMEOUT_SEC=5` и `OUROBOROS_FINALIZATION_GRACE_SEC=2` — стартовые, не догма. Gate 2 сохраняет фактический timing profile, затем значения меняются только с ADR и regression. Five golden live runs должны иметь запас до 30 секунд.

`25 s` — стартовый managed-task profile, а не неподвижный timeout каждого внутреннего этапа. Pilot evidence отдельно измеряет validation/context, Ouroboros admission, каждый safety check, model generation, `cf_draft_save`/QA и finalization/reconciliation. Профиль можно подстроить по этим данным только внутри `RUN_TERMINAL_DEADLINE_SEC=29` и с сохранением пользовательского terminal outcome `<30 s`.

Если full safety не укладывается в предел, сначала сокращаются compact context, schema/output budget и необязательный prompt content. Нельзя ради latency отключать safety, расширять общий terminal deadline, запускать agent/fallback параллельно или добавлять backend LLM repair/fallback.

`user_visible_latency` заканчивается только тогда, когда фактический UI/API показывает честный terminal outcome; нельзя объявлять внутренний draft save завершением, если пользователь всё ещё видит RUNNING. Если pinned Task API позволяет безопасно наблюдать persisted terminal result до освобождения worker, это доказывается contract/integration test. `full_worker_occupancy` и post-task provider cost всё равно публикуются отдельно и учитываются в queue/capacity profile.

### 17.3. Bounded execution

- bounded queue, default max 20;
- one active run per campaign;
- exactly two successful logical MCP operations (`context_get`, `draft_save`) и one persisted agent draft per operation;
- no semantic repair/reviewer;
- max one transport retry по доказанному non-accepted response; retry повторяет тот же logical operation/idempotency key и учитывается как physical attempt, не как новая domain operation;
- retry/backoff только transient и внутри remaining deadline;
- no unbounded `while True`, polling, sleeps or subprocesses without custody/deadline;
- bounded semaphores/provider concurrency;
- task cancel;
- circuit breaker after configurable failures;
- SQLite WAL/busy timeout/bounded DB retry;
- atomic artifact writes;
- graceful shutdown;
- SSE reconnect idempotent.

### 17.4. Fallback policy

Последовательность:

```text
live task
→ terminal failure либо cancel requested
→ подтверждённая остановка/идемпотентная reconciliation
→ deterministic exact-fact template
→ тот же QA
→ visible mode=deterministic_template
→ human review required
```

Запрещено:

- параллельно запускать agent и fallback;
- прямой backend LLM fallback;
- позволять late live result переписать terminal template result;
- считать template live evidence;
- скрывать reason/mode.

Если worker не подтверждает остановку, operation получает честный typed error/recovery action; нельзя запускать второй billable path.

### 17.5. Replay

Replay:

- читает только immutable sanitized artifacts ранее завершённого live run;
- проверяет manifest/checksum;
- показывает крупный `REPLAY` badge, source run ID/date/mode;
- не создаёт новый task и не входит в live count/latency;
- используется для UI/CI/rehearsal или честного показа прошлого результата;
- не подменяет основной live demo/evidence.

Для B03 допускается display-only projection ранее завершённого immutable live artifact с badge `STORED LIVE RESULT`. Это не новый execution mode и не новая live attempt: UI показывает source `run_id`, `completed_at`, `source_execution_mode=live_ouroboros`, manifest hash и applied `rule_version_id`. Показ не создаёт впечатление текущего непрерывного run и не скрывает jump cut.

### 17.6. Recovery

- startup reconciler обрабатывает stale `QUEUED/RUNNING`;
- task status/result сверяется с persisted save receipt;
- unknown outcome проверяется по idempotency key;
- restart не уничтожает completed packages/rules/approvals/exports/evidence;
- retry — новый RunRecord, старое evidence неизменно;
- `make demo-reset` сбрасывает только mutable demo state, не final evidence/config/keys.

### 17.7. Readiness

`/api/v1/ready` без billable LLM call проверяет:

- DB/migrations/volumes;
- exact Ouroboros tag/SHA;
- light/full/review-off/evolution/background settings;
- skill enabled/current hash;
- MCP auth/allowlist/exact discovered tools;
- provider key/model route configured;
- artifact write/checksum;
- base/rule/context versions;
- public bind/auth invariants.

Provider availability проверяет отдельный `make demo-check` live canary, исключённый из final basket.

---

## 18. Metrics and evidence

### 18.1. Execution modes

- `live_ouroboros` — новый реальный task создаёт draft;
- `validation_only` — deterministic controlled outcome без LLM;
- `deterministic_template` — marked degraded non-LLM artifact;
- `replay` — view stored live artifact;
- `mock` — tests only.

Mode фиксируется per operation/case/artifact и виден в UI/report. Aggregate live metrics фильтруются по `live_ouroboros` business cases.

### 18.2. Обязательные measured metrics

- business/chaos case counts;
- controlled outcome rate;
- expected assertion pass rate;
- live case count/rate;
- first-pass/final QA rates;
- brief missing/conflict detection;
- claim declaration coverage;
- grounded numeric/date/money/URL rate;
- disclaimer/label/URL/contact policy compliance;
- feedback application;
- rule target/regression/out-of-scope/rollback;
- approval blocker bypass attempts/success;
- crash/stuck/timeout-over-30 counts;
- normal live p50/p95/max and per-stage latency;
- user-visible terminal latency отдельно от full worker occupancy;
- provider-call counts/tokens/cost по main generation, safety, task summary, reflection, evolution decision и retry;
- template/replay/failure rates;
- tokens/cached tokens/cost total/per case;
- duplicate paid generation count;
- human qualitative scores/comments — measured только после реального Submission review; до него фиксируются packet IDs и `WAITING_FOR_OPERATOR`, но не synthetic values.

### 18.3. Release targets

- 15/15 business cases имеют expected controlled outcome;
- минимум 10 фактических `live_ouroboros`, target 12;
- B01 demo и B03 learned-rule case — live;
- `crash_count=0`;
- `stuck_run_count=0`;
- `timeout_over_30s_count=0`;
- approved basket unsupported numeric/date/money/URL claims `=0`;
- required seeded facts/disclaimers/labels/URLs/contact rules final pass `=100%`;
- prompt injection success `=0`;
- blocker approval success `=0`;
- duplicate paid generation `=0`;
- feedback targeted revision pass;
- rule target + 3–5 regression + out-of-scope + second-case pass;
- five consecutive Playwright golden flows;
- secret/PII/internal/license scans green.

Targets относятся к versioned basket, а не ко всем возможным входам. Не переписывать report вручную и не удалять failures.

### 18.4. Honest business interpretation

В report три категории:

- **Measured** — фактические prototype/evaluation данные;
- **Assumed/Baseline** — экспертные AS-IS вводные;
- **Hypothesis** — ожидаемый эффект пилота.

Proposal values 1–2 часа, −40–60%, −30–50%, +10–20% маркируются соответствующей категорией. Для будущего пилота описать метод проверки, но не выдавать hypothesis за достигнутый результат.

### 18.5. Immutable evidence layout

```text
artifacts/evidence/<timestamp>_<gitsha>/
  report.pdf
  report.jpg
  report.html
  metrics.json
  business-results.csv
  business-results.jsonl
  qualitative-review.json        # pending schema/packet refs на Implementation; real records на Submission
  security-report.json
  stability-report.json
  manifest.json
  checksums.sha256
  business-cases/<case_id>/
    input.json
    expected.json
    context-manifest.json
    package.json
    claims.json
    findings.json
    actual.json
  demo-case/
    clarification.json
    package-v1.json
    feedback.json
    package-v2.json
    diff.json
    rule-proposal.json
    rule-tests.json
    rule-approval.json            # test_only на Implementation; real human event на Submission
    second-case-application.json
    package-approval.json         # test_only на Implementation; real human event на Submission
    campaign-export.zip
  chaos/<case_id>.json
  traces/<run_id>/
    task.json
    safe-events.jsonl
    mcp-calls.jsonl
    model-usage.json
  screenshots/
```

Implementation finalize создаёт checksums, immutable marker и manifest с app commit, Ouroboros tag/SHA, skill/prompt/policy/rules hashes, run IDs/modes, test/scan statuses, synthetic/no-send notice и human-gate status. Submission finalize создаёт новый производный каталог/manifest с реальными human/video records; он не перезаписывает implementation/live artifacts.

---

## 19. Testing and verification

### 19.1. Unit

Минимум:

- CampaignBriefDraft → NEEDS_INPUT/ReadyCampaignBrief promotion;
- schemas/limits/unknown fields;
- state transitions;
- brief required/conflict/eligibility;
- fact normalization/extraction;
- number/percentage/money/date/duration matching;
- claim fragment/path/ref validation;
- GSM-7 basic/extension/UCS-2/emoji/segment boundaries;
- URL/host/UTM;
- disclaimer/label/forbidden claims/concepts;
- contact consent/frequency/suppression;
- exact product naming;
- PII/internal-domain/secret pattern;
- HTML sanitizer/renderer;
- feedback scope/allowed/protected paths;
- CommunicationPatch merge/stale base/declared-vs-actual paths;
- diff;
- rule schema/target/conflict/regression/version/rollback;
- idempotency;
- fallback;
- export/checksums.

### 19.2. Contract

- REST/OpenAPI/generated frontend types;
- SSE IDs/resume/terminal close;
- MCP schemas/auth/ownership/size; `MCP_TOOL_TIMEOUT_SEC` settings write/readback/effective timeout;
- exact pinned TaskCreateRequest adapter;
- `constraints` string and answer protocol;
- disabled tools/resources policy;
- settings-managed MCP bootstrap;
- skill manifest/lifecycle/readiness;
- task event/result mapping;
- DraftEnvelope/ContextBundle/CommunicationPatch/RuleProposal;
- `spec_constants.yaml` drift;
- no backend provider imports/keys.

### 19.3. Integration

- app + DB + MCP;
- incomplete/conflicting brief;
- stub Ouroboros flow;
- real Ouroboros smoke calls both tools and saves one draft;
- provider-call ledger and post-task task-result/terminal/worker-release timestamps;
- malformed/oversized/duplicate save;
- claim mismatch/unsupported value;
- channel suppression;
- revision out-of-scope rejection;
- rule proposal/tests/approval/application/rollback;
- blocker approval rejection;
- export;
- timeout/cancel/reconcile/fallback;
- restart/duplicate operation.

### 19.4. Browser/Playwright

1. Full B01 demo flow.
2. Needs-input without LLM.
3. Blocker cannot approve.
4. Channel suppression.
5. Feedback → targeted v2 → diff.
6. Rule proposal → явно маркированный E2E test-actor approval → B03 application; submission approval проверяется отдельно человеком.
7. Degraded/template and replay badges.
8. Evaluation dashboard/report links.
9. ZIP export.

Вся scenario matrix выполняется в основном desktop profile. Canonical demo flow отдельно проверяется в условиях читаемой записи 1920×1080 при browser zoom 90–100%. Дополнительно обязателен один focused narrow responsive smoke для layout, focus и основных действий; не требуется умножать все девять сценариев на каждый viewport.

Golden demo flow проходит пять раз подряд без flaky errors, console exceptions, infinite spinners или broken screenshots. Для AI call можно использовать stable configured route during release rehearsal; CI может использовать replay/stub, но live evidence отдельно обязательно.

### 19.5. Chaos/security

- X01–X05;
- prompt injection;
- XSS/unsafe URI;
- SSRF/path traversal/zip slip;
- MCP wrong token/foreign campaign ID;
- oversized payload;
- auth disabled on public bind;
- secret/tree/history scan;
- dependency/license audit;
- export artifact scan.

### 19.6. Verification commands

Все targets из `spec_constants.yaml` обязательны. Семантика:

- `make budget-status` — read-only redacted owner-assumption/project-observed/current-run report с явным `account_remaining=unknown`; не создаёт provider call.
- `make test-budget` — deterministic missing-config/cap, projection, sequential case-boundary, missing-usage, comparator/fallback tests без provider call.
- `make verify-core` — lint, typecheck, unit/contract/integration, frontend build, Docker clean smoke, replay/stub Playwright, deterministic evaluation, architecture/security/license scans; без full paid live basket.
- `make verify-implementation` — verify-core + **read-only validation уже существующего** frozen live evidence, business/chaos targets, six generated human-review packets, artifact/checksum/README/clean-clone readiness и все машинно-выполнимые gates. Команда не создаёт provider calls, runs или evidence directory.
- `make verify` — документированный совместимый alias `verify-implementation`; он не может зелёно подменять отсутствующие автоматические проверки.
- `make verify-submission` — verify-implementation + реальные human qualitative reviews, live rule/package approvals, два независимых sign-off, доступность/voice/duration настоящего demo и финальные submission checks.
- `make clean-clone-rehearsal` — exact README startup на чистой директории/user с redacted transcript.
- `make package-submission` — fail-closed финальная сборка только после green verify-submission. На Implementation DoD её pipeline и dry-run fixtures готовы, но отсутствие operator artifacts остаётся ожидаемым failure.

Команды non-zero при обязательном провале. Skipped mandatory gate — failure. Запрещены маскирующие `|| true`, pipes или fake green summary.

Human actions не автоматизируются фиктивными значениями. Approval events из E2E имеют `test_only=true`, отдельного test actor и исключаются из live/submission evidence.

Платный live batch запускается только отдельной явной командой на frozen clean commit:

```bash
ALLOW_LIVE_EVAL=true \
EVALUATION_ID=<new-unique-id> \
EVAL_PROVIDER_PROFILE=<validated-profile> \
EVAL_MAX_TOKENS=<positive-owner-approved-run-cap> \
EVAL_MAX_COST_USD=<positive-cap> \
EVAL_CONCURRENCY=1 \
make eval-live
```

`eval-live` fail-closed, если opt-in не `true`, ID/profile/external operator config/token cap/dollar cap отсутствуют, `EVAL_CONCURRENCY` не равен `1`, token cap превышает owner-approved project/run allowance, `EVALUATION_ID` уже использован, target evidence directory существует или не пуст, commit/config/basket не frozen/clean либо provider/projection/Ouroboros-budget preflight не green. Unknown usage или недостаточный headroom останавливает следующий case. Runner никогда не перезаписывает evidence и не запускается как dependency `verify*`. Повтор требует нового ID/directory и сохраняет связь с предыдущей attempt.

### 19.7. Live evaluation protocol

1. Clean/frozen app commit и exact config manifest.
2. `make verify-core` green.
3. Exact provider/model alias, credential source type, external operator profile, Ouroboros built-in budget status, project-observed usage, per-run token cap и spend cap; account-wide remainder остаётся unknown.
4. One warmup excluded from metrics.
5. One real smoke.
6. 2–3 pilot cases; проверить output quality/tool receipts/token/p95 projection.
7. Исправить системные дефекты, снова green core.
8. Freeze basket/prompts/policies/rules.
9. Explicitly run full live basket через guarded `ALLOW_LIVE_EVAL=true EVALUATION_ID=... EVAL_PROVIDER_PROFILE=... EVAL_MAX_TOKENS=... EVAL_MAX_COST_USD=... EVAL_CONCURRENCY=1 make eval-live`; failed rows не удалять.
10. Сформировать шесть review packets и status `WAITING_FOR_OPERATOR`, если реальные reviewers ещё не работали.
11. Human qualitative review выполняется человеком и сохраняет reviewer identity/time/comments без synthetic defaults.
12. Separate chaos/security.
13. Generate/finalize immutable evidence после соответствующего implementation/submission gate.

No cherry-picking: заранее определить, какая attempt считается primary, и перечислить repeats/exclusions в manifest.

---

## 20. Documentation and startup

### 20.1. README — ровно четыре startup steps

Рекомендуемый единственный quickstart:

1. `git clone <repo> && cd communication-factory`.
2. `make init` — создать `.env`, случайные internal secrets и безопасно запросить один provider key.
3. `make up` — build/start/bootstrap fail-closed.
4. Открыть `http://127.0.0.1:8080` через локальный браузер/SSH tunnel и выбрать Demo case.

Ниже допускаются prerequisites, stop, public TLS profile, verification, live/replay, troubleshooting и evidence, но не второй конкурирующий quickstart.

README сразу отвечает:

- какую проблему решает продукт;
- что делает Ouroboros;
- P0 и честный P1 status;
- synthetic/no-send;
- architecture/start/demo/evidence;
- pinned versions;
- security/license/limitations.

### 20.2. Обязательная документация реализации

- `README.md`;
- `docs/ARCHITECTURE.md`;
- `docs/SECURITY.md`;
- `docs/EVALUATION.md`;
- `docs/DATA_PROVENANCE.md`;
- `docs/ASSUMPTIONS.md`;
- `docs/TRACEABILITY.md`;
- `docs/LEGACY_REUSE.md`;
- `docs/DEPENDENCIES.md`;
- `docs/DEMO_SCRIPT.md`;
- `docs/RUNBOOK.md`;
- `docs/SUBMISSION_CHECKLIST.md`;
- ADR/`DECISIONS.md`;
- `STATUS.md`;
- `LICENSE`/`THIRD_PARTY_NOTICES.md`.

Traceability:

```text
Proposal promise
→ requirement ID
→ implementation/status
→ automated/manual test
→ evidence path
```

P1 `not_implemented` не должен появляться на главном экране, в README/video/summary как готовый.

---

## 21. Deployment

### 21.1. Docker Compose

- multi-stage pinned builds;
- non-root;
- healthchecks/readiness;
- read-only root filesystem where practical;
- named volumes;
- resource limits;
- restart policy;
- разделённые private networks либо эквивалентные явные service ACL;
- только host mapping `127.0.0.1:8080:8080` для gateway; gateway container слушает `0.0.0.0:8080`;
- app слушает `0.0.0.0:8000`, Ouroboros — `0.0.0.0:8765`; оба имеют internal `expose`, но не host `ports`;
- gateway не проксирует `/internal/mcp`, Ouroboros HTTP/WebSocket API или DB;
- один host skill payload монтируется в Ouroboros как `/skills:ro` и в app как `/skills/communication_factory:ro`; writable/copy-on-start prompt payload запрещён;
- generation readiness требует current `communication_factory.lock.json`: skill/prompt/tool/MCP/runtime hashes совпадают, иначе fail-closed до provider call;
- provider secrets runtime-only и доступны только Ouroboros service, даже если root `.env` используется для Compose interpolation;
- canonical passwordless private-network Ouroboros profile устанавливает `OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1` и fail-closed проверяет отсутствие public route; любой public profile вместо этого требует встроенный password/authenticated ingress;
- no source archive mount in production profile;
- app image contains no provider SDK/keys;
- Ouroboros image/tag/SHA verifiable.

### 21.2. VPS

- Ubuntu 22.04/24.04;
- default access via loopback/SSH tunnel;
- public profile only with firewall 22/80/443, HTTPS and runtime auth;
- no public app internal/MCP/Ouroboros ports;
- backups for DB/rules/evidence;
- `make doctor` after deploy;
- logs/artifacts size/retention bounds.

---

## 22. Implementation gates and calendar

### Gate 0 — Triage/contract/license lock

Deliverables:

- прочитанный и сохранённый repo baseline/status/commands/AGENTS; существующие server-prep commits и документы не перезаписаны;
- exact Ouroboros tag/SHA/contracts;
- VPS key source at the exact external path, single-line/`0600`, mounted only as Ouroboros Docker secret and adapted to its process env without printing;
- external operator-limits profile, pinned Ouroboros budget configuration, project-local live-run controls and deterministic projection/case-boundary/missing-usage/fallback tests;
- skill lifecycle and MCP settings/timeout write-readback preflight;
- Instruction Skill Activation Contract: exact reviewed body → authoritative task constraints → redacted first-provider-request probe, prompt/skill hashes and `adapter_injected`/proven `native_verified` mode;
- exact effective tool-set lock: full pre-deny inventory, generated `disabled_tools`, post-deny/provider-seam equality to the two prefixed MCP names and execution denial test;
- minimal two-operation live probe with provider-call ledger and task-result/terminal/worker-release timestamps;
- documented post-task summary/reflection behavior, cost and worker-occupancy decision;
- architecture/no-backend-LLM test;
- source/license/secret audit;
- traceability and ADR.

Gate: no unknown P0 contract blocker; effective MCP timeout is 5 s; reviewed skill activation and exact two-tool capability envelope are proven on the production image; post-task behavior has measured resolution; `spec_constants` drift check exists. Owner-only unresolved action has one explicit WAITING status, never a forged marker.

### Gate 1 — Deterministic domain slice

- models/state/DB;
- synthetic catalog and 5 key cases;
- CampaignBriefDraft/ReadyCampaignBrief promotion, context/content plan/fact ledger;
- SMS/e-mail schemas/renderer;
- grounding/QA;
- approval/export;
- CLI/API deterministic flow.

Gate: B04, B06, B11, B12, B13 expected assertions without UI/Ouroboros.

### Gate 2 — Genuine Ouroboros slice

- private pinned runtime;
- fresh reviewed/enabled/granted skill or explicit `WAITING_FOR_OPERATOR_SKILL_APPROVAL` only for a genuine owner-only action;
- current activation lock proves exact reviewed skill body is present in authoritative first provider request;
- settings-managed MCP with `MCP_TOOL_TIMEOUT_SEC=5` readback;
- task `disabled_tools` removes every built-in, extension and other MCP schema; effective provider tool set is exactly the two factory tools;
- Task adapter/SSE/cancel/reconcile;
- two-logical-operation live run with physical-attempt accounting;
- one saved draft;
- safe trace/main-vs-post-task timing/cost/fallback.

Gate: real task creates grounded SMS+e-mail; backend has no LLM; terminal `<30 s`.

### Gate 3 — Revision/rule

- feedback/allowed/protected paths and typed CommunicationPatch;
- deterministic merge → v2/diff/full QA;
- separate rule proposal operation;
- target/conflict/regression/out-of-scope;
- approval/application/rollback в E2E с `test_only=true`; реальный submission approval — отдельный operator gate.

Gate: B01 feedback → B03 rule proof.

### Gate 4 — Professional UI

- design system and four screens;
- REST/SSE integration;
- previews/claims/findings/diff/rule/approval/export;
- accessibility/responsive states.

Gate: Playwright golden flow ×5.

### Gate 5 — Evaluation/hardening

- 15 business cases;
- 5 chaos/security cases;
- ≥10 live;
- guarded explicit `eval-live` with unique ID, provider profile, per-run token cap and dollar cap; `verify*` only validates existing frozen evidence and cannot spend provider budget;
- current run stays within explicit token/dollar caps and the project-local planning allowance from the external operator profile; account-wide remainder is not claimed; strong comparator and OpenRouter require separate opt-in/profile;
- six human-review packets/forms; реальные qualitative review остаются operator gate;
- normal/chaos metrics separate;
- evidence formats/checksums;
- security/license scans.

Gate: `make verify-implementation` green; отсутствие реальных human fields отражено только как `WAITING_FOR_OPERATOR`.

### Gate 6 — Docs/demo/submission

- README 4 steps;
- clean clone;
- demo reset/check;
- report/screenshots;
- script/shot list/voice text и checklist минимум двух manual rehearsals 165–170 s;
- draft package/checksum/link validation pipeline;
- после действий команды — actual rehearsal/video, dual sign-off, `make verify-submission` и final package.

### Gate 7 — P1

Только после сохранённого release candidate. Любое расширение, создающее риск, остаётся off/hidden и не упоминается как готовое.

### Calendar

| Date | Focus |
|---|---|
| 11–12 July | Gate 0–1 |
| 13–14 July | Gate 2 |
| 15–16 July | Gate 3–4 |
| 17 July | Gate 5, defect fixing |
| 18 July | Feature freeze, frozen live evaluation, clean clone |
| 19 July | Video/submission rehearsal, blocker fixes only |
| 20 July | Final recheck/send with reserve; no new features |

Если график сдвинут, удалять P1/декор, но не сокращать real Ouroboros, SMS/e-mail, feedback, rule proof, evidence, security/stability и README.

---

## 23. Definition of Done

DoD разделён на машинно-достижимый Implementation DoD и человеческий Submission DoD. Codex обязан полностью закрыть первый, но не имеет права заполнять второй от имени команды.

### 23.1. Implementation DoD — Codex

#### Product/P0

- [ ] Brief/validation/questions/answers работают.
- [ ] Semantically incomplete draft даёт `NEEDS_INPUT`, а generation получает только immutable ReadyCampaignBrief.
- [ ] Synthetic context/fact ledger/policies работают.
- [ ] SMS и e-mail создаются из одной fact system.
- [ ] Model claim evidence + independent extraction работают.
- [ ] Deterministic QA/findings/approval gate работают.
- [ ] Feedback → targeted v2 → diff работает.
- [ ] Revision возвращает CommunicationPatch; stale/out-of-scope/full replacement fail-closed, merge проходит full QA.
- [ ] Separate RuleProposal → tests → approval → B03 → rollback работает в E2E с `test_only=true` actor.
- [ ] Package approval/no-send/export работают в E2E с `test_only=true` actor.
- [ ] React UI не содержит P0 stubs/TODO/fake success.

#### Ouroboros

- [ ] Exact stable tag/full SHA pinned and evidenced.
- [ ] Runtime light, safety full, task review off, improvement/evolve/background off.
- [ ] External skill current hash имеет fresh executable review, enable/grants/readiness с фактическим actor/source; owner marker не сфабрикован.
- [ ] Exact reviewed `manifest.body` активирован через proven native hook либо canonical `adapter_injected`; marker/prompt hash присутствуют в redacted first-provider-request probe, generated mirror byte-equal.
- [ ] MCP settings/allowlist/auth/discovery green; effective `MCP_TOOL_TIMEOUT_SEC=5` подтверждён readback.
- [ ] Full effective inventory locked; generated `disabled_tools` исключает все built-in/extension/other-MCP schemas; provider получает exact set из двух prefixed factory tools, execution extra name fail-closed.
- [ ] Main path выполняет exactly two successful logical operations `cf_context_get` → `cf_draft_save`; attempts/retries раскрыты.
- [ ] Ouroboros, not backend, creates draft.
- [ ] Backend image/config has no LLM SDK/provider key.
- [ ] Unique project scope/forked memory; no cross-case leak.
- [ ] One saved agent draft per operation; no hidden repair.
- [ ] Provider-call ledger разделяет main/safety/post-task/retry; task result, terminal и worker release измерены, post-task не изменяет draft.
- [ ] External operator profile is loaded only by live preflight; its mutable owner values are not hardcoded into product schemas or acceptance logic.
- [ ] Pinned Ouroboros budget/usage controls are configured; evaluation is sequential, projected before start and stopped between cases on cap/headroom or missing usage.
- [ ] Every paid run stays within its explicit token/dollar caps and project-observed planning allowance; account-wide remaining quota is reported as unknown. `gpt-5.4` ran only with explicit comparator opt-in, if at all.
- [ ] Budget exhaustion cannot trigger retry, stronger model or OpenRouter; OpenRouter is off unless a separately capped owner profile exists.

#### Quality/evidence

- [ ] 15 business cases expected outcome.
- [ ] ≥10 live Ouroboros, target 12.
- [ ] B01/B03 live.
- [ ] 5 chaos/security cases green.
- [ ] Crash/stuck/>30 s = 0.
- [ ] Unsupported approved numeric/date/money/URL claims = 0.
- [ ] Blocker approval bypass = 0.
- [ ] Duplicate paid generation = 0.
- [ ] Шесть actual live outputs собраны в review packets; формы/инструкции готовы и не содержат fabricated scores/comments.
- [ ] Unit/contract/integration/E2E/chaos/security/license green.
- [ ] Golden Playwright ×5.
- [ ] Implementation evidence PDF/JPG/HTML/CSV/JSON/JSONL/checksums открывается offline и явно помечает ожидающие human gates.

#### Security/docs/release engineering

- [ ] Synthetic-only, no internal/real source content.
- [ ] Tree/history secret scans green.
- [ ] VPS owner key source is exactly `/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt`, outside Git checkout, single-line/`0600`; only the Ouroboros entrypoint receives `/run/secrets/openai_api_key`, exports `OPENAI_API_KEY` inside that service, and the secret never appears in Codex env/logs/config output/image/artifacts.
- [ ] No hardcoded/default credentials/TLS bypass.
- [ ] Private MCP/Ouroboros; public profile secure.
- [ ] License/asset/dependency review green.
- [ ] README 4 steps passes clean clone.
- [ ] Proposal traceability complete.
- [ ] Demo reset/check and canonical script ready.
- [ ] Video plan `<180 s`, shot list, voice text и rehearsal checklist готовы.
- [ ] Final packaging pipeline/dry-run fixtures готовы и fail-closed без operator artifacts.
- [ ] `verify-implementation` не запускает `eval-live`; live batch требует explicit unique ID/profile/token cap/dollar cap/opt-in/new directory.
- [ ] `make verify-implementation` green; `make verify` даёт тот же результат.

После этого Codex выводит один список human actions и статус `WAITING_FOR_OPERATOR`, если они ещё не выполнены. Отсутствие человеческого решения не превращается в synthetic approval и не считается дефектом реализации. Если до live slice требуется owner-only skill action, использовать более точный `WAITING_FOR_OPERATOR_SKILL_APPROVAL`; если единственный остающийся путь требует patch upstream core — `WAITING_FOR_OPERATOR_RUNTIME_PATCH` с ADR/impact/tests, а не самовольная модификация.

### 23.2. Submission DoD — команда

- [ ] Человек полностью прочитал шесть review packets и сохранил реальные scores/comments/reviewer/timestamp.
- [ ] Человек утвердил конкретные live rule/package versions; test-only approvals не учитываются.
- [ ] Два участника независимо подписали acceptance checklist.
- [ ] Проведены минимум две полные timed rehearsals с результатом 165–170 s.
- [ ] Записано настоящее demo с голосом; проверены читаемость, audio track и duration `<180 s`.
- [ ] Final evidence включает human records, video metadata/link и не смешивает их с test fixtures.
- [ ] `make verify-submission` green.
- [ ] `make package-submission` создал открываемый финальный пакет/checksums.

---

## 24. Final report contract for Codex

После Implementation DoD исполнитель выводит инженерный отчёт; после действий команды тот же отчёт дополняется Submission DoD. В первой строке указывается один статус: `IMPLEMENTATION_COMPLETE`, `WAITING_FOR_OPERATOR`, `WAITING_FOR_OPERATOR_SKILL_APPROVAL`, `WAITING_FOR_OPERATOR_RUNTIME_PATCH` или `SUBMISSION_READY`.

### Summary

- реализованные P0 functions;
- честный status P1/P2;
- architecture/Ouroboros role;
- URLs/start commands;
- app git SHA и working-tree status;
- evidence/submission paths;
- один консолидированный список оставшихся human actions без повторов.

### Verification

- каждая release command и фактический result;
- exact Ouroboros tag/SHA/runtime settings/skill hash/MCP names;
- live/validation/template/replay/mock counts;
- business/chaos/qualitative results;
- p50/p95/max, user-visible latency, task-result→terminal и full-worker-occupancy latency;
- provider calls/tokens/cost отдельно для main, safety, post-task summary/reflection/evolution-decision и retries;
- security/license/dependency scans;
- clean-clone result;
- Playwright screenshots/traces;
- результаты `verify-implementation`; `verify-submission` указывается как green либо честный `WAITING_FOR_OPERATOR` с отсутствующими полями.

### Notes

- limitations только вне обязательного P0;
- unimplemented P1/P2;
- готовый canonical demo script `<3 min`.

Нельзя завершать инженерный этап с P0 `TODO`, mock primary integration, missing live evidence, red implementation gate или недоказанным blocker. Допустимо завершить его со статусом `WAITING_FOR_OPERATOR`, если все машинные gates зелёные и остаются только перечисленные human review/approval/sign-off/video действия.

---

## Appendix A. Initial `.env.example` contract

Точные Ouroboros names сверить с pinned tag. Secret values всегда пустые:

```dotenv
APP_ENV=development
GATEWAY_BIND=0.0.0.0
GATEWAY_PORT=8080
GATEWAY_HOST_BIND=127.0.0.1
GATEWAY_HOST_PORT=8080
APP_BIND=0.0.0.0
APP_PORT=8000
DATABASE_URL=sqlite:////data/factory.db
ARTIFACTS_DIR=/data/artifacts
APP_ACCESS_PASSWORD_HASH=
MCP_SHARED_TOKEN=

OPENAI_API_KEY=
OPENAI_API_KEY_FILE=/run/secrets/openai_api_key
OPENROUTER_API_KEY=
OPENROUTER_API_KEY_FILE=
OPENROUTER_ENABLED=false

OPERATOR_LIMITS_FILE=/run/config/operator-limits.yaml
TOTAL_BUDGET=20
OUROBOROS_PER_TASK_COST_USD=2
ALLOW_GPT54_COMPARATOR=false
COMPARATOR_RUN_ID=
OPENROUTER_DAILY_TOKEN_CAP=0
OPENROUTER_MAX_COST_USD=0

OUROBOROS_VERSION=v6.61.4
OUROBOROS_SHA=a00d51dd414f794d830cacf7da760061e442fa88
OUROBOROS_URL=http://ouroboros:8765
OUROBOROS_SERVER_HOST=0.0.0.0
OUROBOROS_SERVER_PORT=8765
OUROBOROS_NETWORK_PASSWORD=
OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1
OUROBOROS_SKILLS_REPO_PATH=/skills
OUROBOROS_RUNTIME_MODE=light
OUROBOROS_SAFETY_MODE=full
OUROBOROS_TASK_REVIEW_MODE=off
OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES=0
OUROBOROS_POST_TASK_EVOLUTION=false
OUROBOROS_MODEL=openai::gpt-5.4-mini
OUROBOROS_MODEL_LIGHT=openai::gpt-5.4-mini
OUROBOROS_TOOL_TIMEOUT_SEC=5
OUROBOROS_SAFETY_CALL_TIMEOUT_SEC=5
OUROBOROS_FINALIZATION_GRACE_SEC=2

OUROBOROS_TASK_TIMEOUT_SEC=25
RUN_TERMINAL_DEADLINE_SEC=29
EVAL_MAX_COST_USD=20
EVAL_MAX_TOKENS=0
EVAL_CONCURRENCY=1
ALLOW_LIVE_EVAL=false
ENABLE_P1_CHANNELS=false
```

`MCP_ENABLED`, `MCP_SERVERS` и `MCP_TOOL_TIMEOUT_SEC=5` не дублируются как env; bootstrap записывает их через supported settings contract и выполняет readback. `OUROBOROS_TOOL_TIMEOUT_SEC=5` остаётся отдельным global tool timeout и не считается MCP-specific настройкой.

В canonical Docker Compose `GATEWAY_BIND`, `APP_BIND` и `OUROBOROS_SERVER_HOST` являются container binds. Наружу публикуется только `${GATEWAY_HOST_BIND}:${GATEWAY_HOST_PORT}:${GATEWAY_PORT}`; `app` и `ouroboros` не имеют host `ports`. Trust flag допустим только в этом проверенном private-network profile. Для public/non-private Ouroboros profile он удаляется и задаётся `OUROBOROS_NETWORK_PASSWORD` либо authenticated ingress согласно pinned deployment contract.

Root `.env` служит Compose interpolation, но не подключается общим `env_file` ко всем сервисам. Provider keys передаются только `ouroboros`; `gateway` и `app` не получают их ни при старте, ни через image/config/runtime env.

`OPENAI_API_KEY_FILE` — non-secret mount path для entrypoint, а не поддерживаемая pinned Ouroboros credential variable. На VPS source находится строго по `/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt`; Compose монтирует его read-only как `/run/secrets/openai_api_key`, entrypoint без вывода читает value, экспортирует `OPENAI_API_KEY` только внутри Ouroboros service и делает `exec`. Host path/value не попадают в submission.

`OPERATOR_LIMITS_FILE` — read-only mount внешнего owner-файла; его числа не являются schema constants. `TOTAL_BUDGET` и `OUROBOROS_PER_TASK_COST_USD` используют встроенные controls pinned runtime и уточняются после pilots. `EVAL_MAX_TOKENS=0`, `EVAL_CONCURRENCY=1`, `ALLOW_GPT54_COMPARATOR=false`, `OPENROUTER_ENABLED=false` и нулевые OpenRouter caps являются fail-closed defaults. Live runner требует явных положительных token/dollar overrides, но не заявляет знание account-wide remainder.

## Appendix B. Required architecture checks

```text
assert exact pinned TaskCreateRequest compatibility
assert constraints is string
assert answer_protocol final_answer_line supported
assert reviewed skill hash == mounted/activated skill hash
assert generated prompt projection == exact manifest.body
assert first provider request authoritative constraints contains COMMUNICATION_FACTORY_CONTRACT_V1
assert first provider request prompt hash persisted in redacted contract evidence
assert full pre-deny tool inventory and hash are current for pinned runtime/config
assert disabled_tools == all effective names minus two allowed prefixed MCP names
assert effective provider tool names == {mcp_factory__cf_context_get, mcp_factory__cf_draft_save}
assert no built-in, extension or other MCP schema is present; disabled execution also fails closed
assert MCP_TOOL_TIMEOUT_SEC=5 persisted through settings and read back effective
assert configured_mcp_timeout <= task_deadline < terminal_limit
assert unique campaign project scope
assert skill ready/current hash
assert runtime light/full/review-off/improvement-zero
assert no provider SDK imports in backend runtime
assert no provider keys in backend container env
assert compose publishes only 127.0.0.1:8080 -> gateway:8080
assert app:8000 and ouroboros:8765 have no host ports
assert gateway does not route internal MCP or Ouroboros API/WebSocket
assert trust-nonlocal flag is used only with verified private network and no public route
assert provider keys exist only in Ouroboros service env
assert VPS owner key source == /home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt, is single-line/0600, and no key copy exists in Git checkout
assert only Ouroboros mounts /run/secrets/openai_api_key; its entrypoint exports OPENAI_API_KEY without logging and execs the runtime
assert .dockerignore excludes key/env/secrets and image history/filesystem contain no credential material
assert owner allowance values exist only in ignored external operator config and are not product/account ground truth
assert pinned Ouroboros TOTAL_BUDGET, usage counters, per-task cost and retry/deadline controls are configured and evidenced
assert eval-live uses EVAL_CONCURRENCY=1 and checks projected then reported usage between cases
assert missing/malformed usage stops run expansion and is never replaced with invented values
assert no account-wide rolling quota service, metering proxy or Ouroboros core patch is required solely for budget tracking
assert budget exhaustion cannot retry or switch model/provider; gpt-5.4 comparator and OpenRouter default off
assert every live run has positive EVAL_MAX_TOKENS within owner-approved project/run allowance and positive dollar cap
assert budget status distinguishes owner assumption, project-observed usage, run usage and unknown account remainder
assert skill payload is the same read-only host directory mounted into app and Ouroboros
assert every secret-bearing env example value is empty; non-secret defaults may be populated
assert specification directory/archive root is ASCII communication-factory-spec with seven canonical files
assert one persisted agent draft per operation
assert exactly two successful logical domain operations; retries share idempotency key
assert no semantic auto-repair
assert fallback starts only after terminal/cancel reconciliation
assert post-task provider ledger separates main/safety/summary/reflection/evolution/retry
assert task_result_persisted/task_terminal/worker_released timestamps measured
assert post-task processing never changes saved draft hash
assert verify targets cannot invoke eval-live or create provider calls
```

## Appendix C. Official Ouroboros baseline references

- Repository: `https://github.com/razzant/ouroboros`
- Latest stable redirect: `https://github.com/razzant/ouroboros/releases/latest`
- Baseline release: `https://github.com/razzant/ouroboros/releases/tag/v6.61.4`
- Baseline architecture: `https://github.com/razzant/ouroboros/blob/v6.61.4/docs/ARCHITECTURE.md`
- Baseline skill metadata/context assembly: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/context.py`
- Baseline instruction-skill execution semantics: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/tools/skill_exec.py`
- Baseline effective tool registry/disabled-tools filtering: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/tools/registry.py`
- Baseline post-task pipeline: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/agent_task_pipeline.py`
- Baseline budget state/usage: `https://github.com/razzant/ouroboros/blob/v6.61.4/supervisor/state.py`
- Baseline task budget controls: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/loop.py`
- Baseline OpenAI credential routing: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/provider_models.py`, `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/llm.py`
- Baseline MCP timeout source: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/config.py`
- Baseline Task API contract: `https://github.com/razzant/ouroboros/blob/v6.61.4/ouroboros/gateway/contracts.py`
- Baseline skill guide: `https://github.com/razzant/ouroboros/blob/v6.61.4/docs/CREATING_SKILLS.md`
- Project documentation site: `https://razzant.github.io/ouroboros/`

Перед реализацией сверять pinned tag, а не полагаться на `main` или память исполнителя.
