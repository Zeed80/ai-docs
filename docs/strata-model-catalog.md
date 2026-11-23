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
  просмотра каталога до установки сервера. Снимок `official-v0.2.20` содержит
  10 семейств и 50 вариантов: 9 актуальных семейств с 44 вариантами плюс
  исторический DeepSeek с 6 вариантами.

| Семейство | Вариантов в 0.2.20 | Профиль приложения |
|---|---:|---|
| Qwen3.8-Flash-Next | 4 | `flash_next_pack` |
| Qwen3.8-Flash-Next Swift 1.5 | 3 | `flash_next_pack` |
| Qwen3.8-Coder-Next | 1 | `flash_next_pack` |
| Unsloth Qwen3.8-Flash-Next | 12 | `unsloth_budget` |
| Qwen3.6-35B-A3B | 3 | `qwen36_own` |
| Qwen3.8-Flash-Next Gyro | 2 | `gyro_gguf` |
| Qwen3.8-Flash-Next NVFP4 | 3 | `nvfp4_pack` |
| Hy4-Flash | 3 | `hy4_shape` |
| Inkling-2-Mini | 13 | `inkling_shape` |
| DeepSeek V4.1 Flash | 6 | исторический, read-only |

DeepSeek отсутствует в downloadable-каталоге актуального релиза. Подписанный feed
0.2.20 содержит только его engine packages, а не скачиваемые варианты модели,
поэтому записи сохранены как `origin=retired`, `upstream_present=false`,
`available=false`. Кнопка скачивания для них отключена; неподтверждённый DeepSeek
Coder в каталог не добавляется. При обновлении backend bundled snapshot также
добавляет в существующий durable-каталог отсутствующие curated/retired записи и
переводит ранее обнаруженные unknown-семейства на подтверждённые профили;
записи, которых нет в bundled file, при этом не помечаются снятыми с публикации. Состав bundled
snapshot не означает, что более старая установленная Strata уже умеет скачивать
все новые варианты: `min_version` и preflight выполняются установленной версией.

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

Для семейств, которые прежний backend сохранил как unknown, разрешён только
явно закодированный переход `unknown → curated supported`: подтверждённый bundled
profile восстанавливает стабильные identity и включает lifecycle. Для уже
поддерживаемых семейств identity и runtime/disk profiles остаются immutable.

Правила ресурсов зависят от семейства:

- Gyro использует официальный `mem_total`: проверяется сумма ОЗУ и VRAM.
- NVFP4/NVFP4-NVIDIA — готовые packs; NVFP4-QAD дополнительно резервирует 110 GB
  на prepare. Размер expert arena повторно к download size не прибавляется.
- Hy4 и Inkling используют собственный shape engine: generic low-RAM,
  resident-budget и conversation-cache flags в их run config не записываются.
  У Inkling допустимы только официальные значения parallel `1/4/8/32/64`.
- `min_version` описывает требуемую версию сервера Strata, а `engine_min` —
  версию engine; эти требования не смешиваются.

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
