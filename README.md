# Фабрика коммуникаций

Подготовка персонализированной клиентской рассылки вручную занимает часы: нужно собрать
разрозненный контекст, написать тексты для нескольких каналов и проверить каждое число, ссылку и
ограничение. «Фабрика коммуникаций» — synthetic-only/no-send прототип, который соединяет свободный
бриф с проверенной факт-картой продукта, превращает их в единый комплект СМС и писем и оставляет
финальное решение
человеку. Все данные вымышленные; внешней отправки нет как функции.

Агент проходит процесс end-to-end: неполный бриф детерминированно останавливается на уточняющих
вопросах (`NEEDS_INPUT` — состояние «нужны данные», модель не вызывается); готовый бриф
превращается в versioned-контекст; управляемая задача Ouroboros читает контекст, сама формирует
typed-комплект каналов и один раз сохраняет его; backend без LLM независимо сверяет каждое число,
дату, ссылку и политику по 22 детерминированным проверкам; замечание человека создаёт видимую
новую версию с diff; согласованное правило и готовый пакет утверждает только человек, после чего
доступен ZIP-экспорт.

## Роль Ouroboros и две MCP-границы

Ouroboros — закреплённый агентный runtime (tag `v6.61.4`, commit
`a00d51dd414f794d830cacf7da760061e442fa88`): он исполняет проверенную инструкцию и сам создаёт
содержимое, а не служит обёрткой над одиночным chat-вызовом. Продукт отделён от агента двумя
закрытыми MCP-границами (MCP — протокол инструментов модели): `mcp_factory__cf_context_get`
выдаёт только разрешённую версию фактов кампании, `mcp_factory__cf_draft_save` принимает только
typed-черновик и прогоняет его через независимый QA перед сохранением. Provider-запрос содержит
ровно эти два инструмента; все прочие built-in/extension-инструменты запрещены, поэтому агент
не может отправить сообщение, не имеет инструмента произвольного сетевого доступа и не может
изменить состояние в обход доменного контракта (сетевой доступ самого сервиса Ouroboros ограничен
provider-egress). Backend не вызывает LLM ни в основном, ни в аварийном пути.

```text
Browser → Caddy gateway (127.0.0.1:8080, защищённая сессия) → FastAPI + SQLite
                                     ↓ private Task API
                               pinned Ouroboros v6.61.4
                                     ↓ ровно два MCP-инструмента
                 mcp_factory__cf_context_get → mcp_factory__cf_draft_save
                                     ↓
               deterministic QA / версии → human approval → ZIP-экспорт
```

Схема контейнеров, trust boundaries и поведение при сбоях — в [архитектуре](docs/ARCHITECTURE.md).

## Запуск — четыре шага

1. `git clone https://github.com/akimovdspb/SerenaSK_SberHack_2026.git && cd SerenaSK_SberHack_2026`
2. `make init`
3. `make up`
4. Открыть `http://127.0.0.1:8080`, войти с данными из `runtime/operator/access.txt` и нажать
   «Новая кампания».

Prerequisites: Ubuntu/Linux, Docker Engine 24+, Docker Compose v2.23+, GNU Make, Git и `uv` 0.11+.
`make init` скрыто запрашивает ключ OpenRouter и хранит его только вне checkout. Вместо ввода можно
заранее создать внешний файл с одной строкой и mode `0600`, затем выполнить
`PROVIDER_KEY_FILE=/absolute/path/to/openrouter_key make init`. В `.env.local` сохраняются только
несекретные настройки и пути; случайный пароль интерфейса находится в ignored
`runtime/operator/access.txt`. `make up` собирает тот же single-image профиль
`openrouter::z-ai/glm-5.2`, что используется на Railway, и ждёт полной готовности Ouroboros;
первый холодный запуск может занять несколько минут. Остановка — `make down`.

## Рабочий путь: бриф и факт-карта

Бриф отвечает на вопрос «кому, зачем и в каком тоне пишем»: в нём находятся цель, аудитория,
событие, каналы, период и заметки. Факт-карта отвечает на вопрос «что о продукте доказано»: в ней
зафиксированы точное название, CTA, разрешённые формулировки, типизированные значения и безопасные
источники. Текст в цели или заметках не становится продуктовым фактом автоматически.

В мастере `/campaigns/new` есть два честных пути:

- выбрать существующий синтетический продукт из серверного справочника;
- создать новый синтетический продукт, ввести минимум один структурированный факт и подтвердить
  отсутствие ПДн. Сервер создаёт versioned fact card и сам назначает идентификаторы источников.

Семь карточек «Начать с примера» заполняют только редактируемый исходный бриф. Сохранённый удачный
текст не передаётся ни в API мастера, ни в новую генерацию и помечен
`EDITORIAL_REFERENCE_NOT_LIVE_NOT_RELEASE_EVIDENCE`. Отдельная вкладка «Тестовые сценарии» хранит
B01–B15 как regression-корзину; эти пятнадцать сценариев не ограничивают рабочий authoring.

### Hosted-демо в Railway

Для общего стенда команды репозиторий можно развернуть одним Railway-сервисом: корневой
`railway.toml` и корневой `Dockerfile` выбирают готовый образ с gateway, приложением и закреплённым
Ouroboros. Автоматически предложенный Railway-сервис `@communication-factory/web` использовать
нельзя: нужен Empty Service с пустым Root Directory. Нужны только три secret-переменные —
`APP_ACCESS_USERNAME`, `APP_ACCESS_PASSWORD` и `OPENROUTER_API_KEY`; маршрут модели зафиксирован как
`openrouter::z-ai/glm-5.2` без model fallback. Hosted-профиль использует отдельную страницу входа и
live-генерацию через Ouroboros; Volume рекомендуется для сохранения SQLite/review между деплоями,
но не требуется для самого запуска.
Пошаговая инструкция и поведение первого запуска описаны в [Railway runbook](docs/RAILWAY.md).
Это дополнительный hosted-профиль; воспроизводимый запуск для жюри остаётся четырёхшаговым
Docker Compose-сценарием выше.

## Подтверждённые результаты MVP

В сохранённой выборке подтверждены 10 живых end-to-end кейсов: B01, B02, B03, B04, B06, B07,
B09, B10, B14 и B15. Каждый из них выполнен через OpenRouter/GLM-5.2 внутри Ouroboros, завершён
готовым СМС и/или письмом и прошёл детерминированную проверку качества со счётом 100/100.
Публичная вкладка «Результаты» показывает только эти десять фактических выходов. Полный
санитизированный технический протокол Basket-03 сохранён в
[reports/basket03-mvp-testing](reports/basket03-mvp-testing); он остаётся отчётом тестирования
MVP, а не каноническим frozen release evidence.

## Проверка: детерминированная и live

`make verify-core` выполняет lint, typecheck, тесты, сборку, изолированный Compose-smoke,
Playwright-матрицу, replay-корзину B01–B15 и chaos/security-сканы — без единого provider-вызова.
`make verify-implementation` дополнительно только читает уже существующее frozen live evidence.
Платная live-корзина запускается исключительно отдельной командой с явным opt-in, новым
`EVALUATION_ID`, положительными token/$-лимитами и `EVAL_CONCURRENCY=1` — протокол описан в
[Evaluation](docs/EVALUATION.md). Replay и deterministic-template всегда помечены и никогда не
выдаются за live-результат Ouroboros.

### Контролируемый provider retry

Приложение умеет один раз повторить физическую задачу Ouroboros только после типизированного
временного transport/provider-сбоя и доказанного освобождения первой задачи. Логический run,
operation/iteration, контекст, provider/model, prompt/skill/tool hashes и request digest при этом
не меняются; сохраняется максимум один draft/package. Неоднозначный submit сначала разыскивается
по заранее созданному `task_id`, а сохранённый результат восстанавливается без новой генерации.
Schema/QA/content/safety/policy/contract failures, отмена пользователя и неполное usage сами по
себе retry не разрешают. Это transport recovery, а не content retry или скрытый LLM repair pass.

В canonical engineering Compose-профиле флаг `CONTROLLED_PROVIDER_RETRY_ENABLED` по умолчанию
равен `false`, а `make verify-core` проверяет retry отдельно через providerless fault-тест
`make e2e-controlled-retry`. Четырёхшаговый portable local-профиль (`make up`) и Railway используют
одинаковый single-image runtime и явно включают один контролируемый retry для интерактивной работы:
он срабатывает только при типизированном временном transport/provider-сбое, не повторяет контент
из-за замечаний QA и не меняет маршрут `openrouter::z-ai/glm-5.2`. Профиль
`openrouter-glm-5.2-campaign-authoring` использует task/terminal deadline 600/900 секунд, чтобы
медленный, но штатный ответ GLM-5.2 не подменялся резервным шаблоном через три минуты.
Текущие frozen evidence и десять подтверждённых MVP-кейсов этим operational-профилем не
переквалифицированы.

## Безопасность и данные

Provider-ключ монтируется read-only только в контейнер Ouroboros; gateway, app, браузер и host его
не получают. Наружу опубликован только loopback-порт gateway. Все каталоги, персоны и политики —
синтетические, с зарезервированными доменами `.test`/`.invalid`; реальных ПДн нет. Детали — в
[Security](docs/SECURITY.md) и [Data provenance](docs/DATA_PROVENANCE.md); лицензии — в
[LICENSE](LICENSE) и [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md).

## Материалы проекта

- **Рабочий стенд:** [serenask-sberaihack.up.railway.app](https://serenask-sberaihack.up.railway.app/) — вход по учётным данным, выданным командой.
- **Публичный код:** [akimovdspb/SerenaSK_SberHack_2026](https://github.com/akimovdspb/SerenaSK_SberHack_2026).
- **Презентация:** [«Фабрика коммуникаций (основной этап)»](docs/%D0%A4%D0%B0%D0%B1%D1%80%D0%B8%D0%BA%D0%B0%20%D0%BA%D0%BE%D0%BC%D0%BC%D1%83%D0%BD%D0%B8%D0%BA%D0%B0%D1%86%D0%B8%D0%B9%20%28%D0%BE%D1%81%D0%BD.%20%D1%8D%D1%82%D0%B0%D0%BF%29.pdf).
- **Демо-видео (<3 минут, с озвучкой, MP4):** [Яндекс Диск](https://disk.yandex.ru/i/PiKV2pLeqjPqJQ) ·
  [запасная ссылка на Google Диск](https://drive.google.com/file/d/1OOV1NQ3ZYUUdqHO4xfbsChhrK5RSk0E6/view?usp=sharing).

- **Судье:** [маршрут жюри](docs/JURY_GUIDE.md) и
  [отчёт о результатах MVP](docs/PROJECT_RESULTS_REPORT.md) — критерии, что показать и где
  проверить.
- **Разработчику:** [архитектура](docs/ARCHITECTURE.md), [runbook](docs/RUNBOOK.md),
  [evaluation](docs/EVALUATION.md), [зависимости](docs/DEPENDENCIES.md).
- **Оператору стенда:** [Railway runbook](docs/RAILWAY.md) и [общий runbook](docs/RUNBOOK.md).

Нормативный контракт — [техническое задание](01_TECH_SPEC_SOURCE_OF_TRUTH.md); ограничения и
допущения — в [Assumptions](docs/ASSUMPTIONS.md).
