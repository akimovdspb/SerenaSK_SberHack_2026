# Runbook

## Инструменты

- Docker Engine 29+ и Docker Compose v5+;
- GNU Make 4+;
- Node 20–22 с npm 10–11 (нужны для `make setup`/локальной сборки фронтенда);
- `uv` 0.11+ — Python 3.12 и зависимости ставятся через него/контейнеры, host-`python` не
  предполагается.

## Bootstrap и запуск

```bash
make init    # локальная конфигурация: случайные внутренние credentials, ignored .env
make setup   # локальные зависимости: uv sync --frozen && npm ci
make up      # preflight → build/start gateway+app+ouroboros → fail-closed bootstrap
make down    # остановить только сервисы этого проекта
```

`make up` завершается `make bootstrap`: настраивается pinned runtime и одноразовый
`contract-probe` пишет атомарный runtime lock; без валидного lock генерация заблокирована.
Провайдерский ключ оператор заранее кладёт в
`/home/dmitry/secrets/communication-factory/OPENAI_API_KEY.txt` (mode `0600`, вне checkout);
профиль лимитов — рядом в `operator-limits.yaml`. Пароль UI — в ignored
`runtime/operator/access.txt`; не печатать и не копировать его.

## Диагностика

```bash
make doctor        # redacted-диагностика окружения и сервисов, read-only
make seed          # проверка точности синтетического seed без мутаций
make budget-status # honest budget: допущения владельца / расход проекта / account = unknown
```

## Команды без provider-вызовов

| Задача | Команда |
| --- | --- |
| Линт / формат | `make lint` и `make format-check` |
| Типы (Python + TypeScript) | `make typecheck` |
| Юнит/интеграционные тесты | `make test` |
| Контрактные тесты | `make test-contract` |
| Сборка фронтенда и образов | `make build` |
| Изолированный Compose-smoke | `make smoke` |
| Изолированная Playwright-матрица | `make e2e` |
| Providerless retry fault-профили (успех + исчерпание) | `make e2e-controlled-retry` |
| Детерминированный оракул B01–B15 | `make eval-replay` |
| Chaos-suite X01–X05 | `make test-chaos` |
| Сканы секретов/PII/лицензий | `make security` |
| Все детерминированные gates | `make verify-core` |
| Read-only проверка frozen evidence | `make verify-implementation` (alias `make verify`) |
| Валидация guard'а live-корзины | `make eval-live-preflight` |
| Сборка evidence из существующего live-прогона | `EVALUATION_ID=<id> make evidence` |
| Dry-run схем финального пакета | `make package-submission-dry-run` |

`make verify-core` дополнительно вычищает live opt-in переменные из окружения и падает, если
какой-либо шаг изменил usage-журнал, live-каталоги или evidence. `make smoke` и `make e2e`
поднимают собственный providerless-стек со случайным loopback-портом и убирают его за собой.

## Контролируемый retry задачи Ouroboros

- Release-default: `CONTROLLED_PROVIDER_RETRY_ENABLED=false`. Максимум зашит в коде: один
  логический run содержит одну обычную и не более одной повторной физической попытки.
- `CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE` — только test-переключатель. Значения
  `transient_then_success` и `transient_twice` валидны лишь при `APP_ENV=test` и включённом
  retry; production/Compose default — `none`.
- Аудит попыток хранится в SQLite и доступен через run/workspace API и ZIP export: отдельные
  `attempt_id`/`task_id`, identity/digests, timestamps, outcome/reason, решение retry, receipts,
  наличие результата и usage (`EXACT` либо честное `UNKNOWN`).
- Команда `make e2e-controlled-retry` не запускает Ouroboros и не использует provider-ключ: она
  поднимает два изолированных app/gateway-стека с детерминированным Task API double.

Будущая release-операция допустима только после отдельного решения владельца:

```bash
CONTROLLED_PROVIDER_RETRY_ENABLED=true \
CONTROLLED_PROVIDER_RETRY_FAULT_PROFILE=none \
docker compose up --detach --no-deps --force-recreate app
```

До release/Railway enablement нужно заново проверить runtime lock и exact route/tools, выполнить
`make verify-core`, providerless fault-матрицу, затем новый guarded live smoke, 2–3 pilots и
последовательную live-корзину с уникальными ID, пустыми evidence-каталогами и положительными
token/$ caps. Результаты требуется прочитать и заморозить как новую qualification; прежнее frozen
evidence не подтверждает включённый retry. Prompt/schema/model/provider/P1 менять для этого нельзя.

## Платные команды (каждая — за отдельным guard)

Общие правила: положительные token- и $-лимиты обязательны; неудачную платную попытку не
повторять на месте — новый связанный ID с причиной; никакая ситуация не переключает
модель/провайдера автоматически. В owner-authorized continuation-v4 неполное usage не расширяет
provider ledger: generation ID опрашивается не более 600 секунд, затем только отдельный запрос
получает bounded estimate; no-ID anomaly имеет нулевой reservation, а независимые кейсы продолжаются.

| Команда | Обязательный opt-in |
| --- | --- |
| `make live-probe` — транспортный probe двух инструментов | `ALLOW_LIVE_PROBE=true`, новый `EVALUATION_ID`, лимиты |
| `make gate2-live-pilot` — один разрешённый профилем pilot | `ALLOW_GATE2_LIVE=true`, `PILOT_CASE_ID`, лимиты |
| `make live-readiness` — связать warmup/smoke/пилоты в readiness-манифест | ID прогонов + отметка о фактическом просмотре результатов (сам provider не вызывает) |
| `make eval-live` — полная последовательная корзина B01–B15 | `ALLOW_LIVE_EVAL=true`, новый `EVALUATION_ID`, `EVAL_PROVIDER_PROFILE`, `EVAL_MAX_TOKENS`, `EVAL_MAX_COST_USD`, `EVAL_CONCURRENCY=1`, пустой каталог назначения |
| `make demo-canary` — один capped B04-канарейка перед демо | `ALLOW_DEMO_CANARY=true`, новый `DEMO_CANARY_ID` |
| `make clean-clone-rehearsal` — README-репетиция на чистом checkout + один live B04 | `ALLOW_CLEAN_CLONE_LIVE=true`, новый `CLEAN_CLONE_EVALUATION_ID`, лимиты |

Полный протокол платной оценки и семантика evidence — в [Evaluation](EVALUATION.md).

Для уже сохранённого failed Gate 2 run с неполным usage continuation-v4 допускает одноразовое
reconciliation без изменения `report.json`: передайте его `EVALUATION_ID`, активные
`GLM_NIGHT_*` authority-переменные и выполните
`uv run python -m scripts.gate2_live --reconcile-accounting`. Повторный запуск отклоняется; exact
metadata добавляет отдельную ledger-строку, а estimate никогда не выдаётся за provider usage.

Перед платным executable review bootstrap читает effective runtime route и отклоняет несовпадение
с provider profile до создания marker и provider call. Исторический failed review с exact
persisted actor record можно один раз восстановить командой
`uv run python -m scripts.bootstrap_runtime --recover-review-run-id <failed-id>`, пока proving
runtime snapshot ещё доступен. Recovery сохраняет failure/non-evidence, пишет usage под фактически
наблюдавшимися provider/model и отклоняет duplicate, stale или не связанный по времени record.

## Демо

```bash
make demo-reset   # сбрасывает только изменяемое демо-состояние (SQLite приложения и экспорты)
make demo-check   # read-only: строгий lock, frozen evidence и свежая (<24 ч) канарейка
```

`make demo-reset` не трогает runtime, evidence и usage. `make demo-check` намеренно красный,
пока нет frozen live evidence и текущей канарейки, — это честное состояние, а не дефект.
Сценарий записи — в [demo script](DEMO_SCRIPT.md).

## Резервное копирование

```bash
BACKUP_ID=<unique-id> make backup   # online SQLite Backup API → checksummed ZIP mode 0600
make backup-check                   # restore-readability выбранной/последней копии
make backup-prune                   # dry-run ретенции; удаление только с ALLOW_BACKUP_PRUNE=true
```

Архив содержит БД (кампании и правила), redacted runtime lock и валидные immutable
evidence-каталоги; `.env`, ключи и task-state Ouroboros не попадают в него. Восстановление в
рабочий том — только явное действие владельца после `backup-check`, не поверх работающего тома.

## Известные особенности

- `runtime/`, `private_sources/` и генерируемый `artifacts/` намеренно вне Git и вне
  Docker-контекстов; `make init` создаёт каталог evidence-mount.
- Внешний operator-профиль управляет только планированием платных прогонов; счётчики проекта
  никогда не заявляют account-wide остаток квоты.
- Перед любым платным прогоном обязательны зелёный `make runtime-patch-assessment`, image-bound
  lock со `strict=true` для обоих инструментов, чистый commit и уникальный связанный ID;
  отката на нестрогую проекцию нет.
- Ouroboros `v6.61.4` декларирует MIT в `pyproject.toml`, но в теге нет файла LICENSE, на который
  ссылается README апстрима; расхождение раскрыто в [Dependencies](DEPENDENCIES.md).
- Uvicorn стартует приложение фабрикой `apps.api.app.main:create_app --factory`; каждый жизненный
  цикл владеет собственным одноразовым FastMCP session manager.
