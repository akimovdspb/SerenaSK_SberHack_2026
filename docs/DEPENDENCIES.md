# Зависимости и лицензии

Зависимости воспроизводимо зафиксированы:

- `uv.lock` — host-окружение Python для разработки и тестов;
- `apps/requirements.lock` — hash-locked подмножество для образа приложения;
- `package-lock.json` — граф React/Vite/Playwright;
- `ouroboros/requirements.lock` — hash-locked граф образа pinned runtime;
- digest-pinned базовые образы в Dockerfile и точный архив исходников Ouroboros
  (tag `v6.61.4`, commit `a00d51dd414f794d830cacf7da760061e442fa88`).

`make license` читает метаданные установленных пакетов для точных locked-версий Python,
license-метаданные `package-lock.json` и метаданные собранного образа Ouroboros. Для
platform-only колёс допустимы только version-bound записи в
`data/dependency_license_overrides.json`. Команда падает при расхождении версий, отсутствующих
метаданных и запрещённых лицензиях (proprietary/AGPL/SSPL/BUSL/Commons-Clause) и пишет ignored
machine-инвентарь с input-хэшами в `runtime/security/`. `make security` пересобирает инвентарь и
не принимает устаревший отчёт.

Атрибуции — в [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md). Известное расхождение: upstream
Ouroboros декларирует MIT в `pyproject.toml`, но tag `v6.61.4` не содержит файла LICENSE, на
который ссылается его README; факт раскрыт, файл не выдумывается.

Платные проприетарные SDK, шрифты, изображения, логотипы, шаблоны и legacy-ассеты не требуются.
Единственная внешняя платная поверхность — доступ к модельному сервису, разрешённому
организатором.
