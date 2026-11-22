# Динамический каталог моделей Strata

Каталог семейств и вариантов больше не задаётся статическими `FAMILIES`/`QUANTS`
в коде. Источник — официальный MCP `strata_models` **установленного** образа
Strata. Список перечитывается после установки/обновления образа, при смене image
на старте backend и вручную кнопкой «Перечитать каталог». Кнопка не проверяет и
не устанавливает новый релиз.

## Хранение и обновление

- `strata_catalog_snapshots` хранит неизменяемые versioned JSON snapshots с
  source/image revision и SHA-256. Redis — только hot cache; после его очистки
  каталог восстанавливается из Postgres.
- `strata_official_catalog.v1.json` — offline snapshot для первого запуска и для
  просмотра каталога до установки сервера.
- Refresh выполняется под process-local и Redis lock, сохраняет snapshot и
  family-level `ModelCatalogRuntimeEntry` в одной транзакции. После commit
  обновляются cache и runtime overlays. При ошибке остаётся last-known-good.
- Исчезнувшие из upstream записи получают `origin=retired` и
  `upstream_present=false`; история, установленные файлы, selected model, slots,
  revisions и GPU owner не меняются.

MCP выполняется в одноразовом контейнере того же image без GPU и сети, с
read-only root и `/data`. Upstream не передаёт приложению shell args, env,
произвольные paths, image refs или download URLs.

## Поддерживаемые и read-only записи

Прежние variant IDs, `setup_id`, `catalog_key`, `served_name`, env и пути не
меняются. Новые варианты известных runtime profiles доступны к полному
lifecycle; перед первым скачиванием выполняется официальный
`strata launch --plan ... --output json`, который ничего не запускает.

Новое семейство отображается как `candidate/discovered` с
`capabilities_unknown=true`, но остаётся read-only, пока приложению не добавлен
подтверждённый runtime profile. Vision/tools не наследуются от Qwen, а перенос
слотов и действия download/load/delete блокируются fail-closed.

Unknown `aiw.env` больше не заменяется молча на `IQ2_XS`: status возвращает
`known=false`, `served_catalog_key=null`, `ram_plan=null`. Неизвестные локальные
run configs видны как `legacy__…`/`origin=orphan`; приложение их не загружает и
не удаляет.

## Lifecycle

Скачивание по-прежнему идёт в отдельном installer container и не меняет выбранную
модель или GPU owner. Остановка допускает resume. Retired not installed нельзя
скачать заново; retired installed можно загрузить при совместимой версии.

Удаление на Strata 0.2 использует официальный `strata_remove`: сначала
`confirm=false`, затем `confirm=true` после подтверждения оператора. Backend не
интерпретирует и не исполняет paths из removal plan. Для legacy 0.1 сохранён
filesystem fallback только для curated layout с containment/symlink checks.
Selected/downloading/orphan/unsupported записи удалять нельзя.

API: `POST /api/local-models/strata/catalog/refresh` (admin); 409 — параллельный
refresh, 422 — невалидный каталог, 502 — локальный source недоступен.

## Проверки

```bash
python3 -m pytest \
  backend/tests/test_strata_catalog.py \
  backend/tests/test_strata_catalog_source.py \
  backend/tests/test_strata_provider.py \
  backend/tests/test_strata_lifecycle.py \
  backend/tests/test_model_runtime_durability.py \
  backend/tests/ai/test_model_registry.py -q

cd frontend
npm run typecheck
PLAYWRIGHT_MOCK_API=1 npx playwright test \
  tests/e2e/strata-gpu-switch.spec.ts --project=chromium
```

Живые скачивания, обновление Strata и удаление тестового variant выполняются
только после отдельного подтверждения объёма и цели. Production-модели для
проверки не удаляются.
