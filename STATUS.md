# Статус публичной сборки

- Исходный интеграционный commit: `99e2ad5` поверх проверенного UI-checkpoint
  `02de9784b032d96fcb1cda7c354295c1dddd4d4f`.
- `make verify-core`: 21/21 на UI-checkpoint; 36 E2E пройдены, 2 штатно пропущены;
  controlled-retry 2/2; security, Ruff, mypy и frontend build — зелёные.
- Публичный срез содержит код, тесты, lockfiles, презентацию и обезличенный отчёт Basket-03.
- Исключены внутренние goal/handoff/agent-журналы, model-handoff, исходные рабочие материалы,
  credentials и текст озвучки.
- Текущий submission status: `WAITING_FOR_OPERATOR` — после публикации видео нужно добавить его
  ссылку в README и маршрут для жюри.
