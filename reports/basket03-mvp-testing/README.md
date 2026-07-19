# Basket-03 — отчёт о тестировании MVP

Это обезличенный пакет фактического live-прогона `p0-glm-continuation-basket-20260715-03`.

**Статус:** `FAILED_NON_EVIDENCE`. Пройдено **13/15** кейсов: **10/12** live Ouroboros и
**3/3** validation-only. `B05` и `B08` явно сохранены как неуспешные. Этот пакет можно
использовать как честный отчёт о тестировании MVP, но нельзя называть каноническим release evidence.

## Привязка

- рекомендуемая основа кода: `6010d93300ddbfa87c19edf6e6d688e19198a0ff`;
- commit, на котором физически выполнен Basket-03: `0bdebf68f3e03039b425d3dae0483f26e9d143b1`;
- provider/model: `openrouter` / `z-ai/glm-5.2`;
- usage: `1440650` токенов, `$0.81329568`, `82` provider calls;
- исходный `report.json` SHA-256: `93d8601f5ea34e8930a234cdf036992007bdbd531e2df8175d94af2e6f52d5aa`.

## Содержимое

- `basket03-report.json` — полный обезличенный структурированный отчёт;
- `summary.csv` — плоская сводка по 15 кейсам;
- `cases/B01.json` … `cases/B15.json` — вход, фактические SMS/e-mail, QA, режим, latency и usage;
- `report.html` и `report.pdf` — компактное представление для просмотра;
- `checksums.sha256` — SHA-256 всех файлов пакета, кроме самого checksum manifest.

Сырые `runtime/`, transport payloads, provider-request/generation ID, task/project/package ID,
секреты и внутренние tool-трассы не публикуются. Все входные данные синтетические; отправки SMS
или e-mail не выполнялись.
