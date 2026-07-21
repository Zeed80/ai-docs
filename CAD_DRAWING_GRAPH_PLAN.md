# План точного перечерчивания через полный граф чертежа

Дата фиксации: 2026-07-22.

Статус инкремента: фундамент фаз 1–3 реализован; фазы 4–8 продолжаются по gate-ам ниже. Число `100%` используется только как целевая exact-sheet метрика: система не маркирует результат точным, пока независимые проверки не доказали полноту конкретного листа.

Этот документ — исполнимый план основного метода «По описанию». Под «описанием» понимается не пользовательский текст, а полное координатное и семантическое описание исходного листа, которое строит модель-распознаватель. Свободное текстовое задание остаётся отдельным вспомогательным входом и не считается доказательством качества перечерчивания.

## Целевой контракт

```text
Исходный raster/PDF
  → reader: наблюдения в координатах источника + evidence
  → EngineeringDrawingGraph
  → completeness / topology / dimension / pixel verifier
  → graph drafter без интерпретации
  → CadIR с сохранением идентификаторов
  → DXF/SVG/PDF + нормализованная проекция в PostgreSQL
  → две независимые подписи для выпуска и active learning
```

Reader не имеет права создавать подтверждённую геометрию. Drafter не имеет права распознавать, додумывать или исправлять вход. Любой видимый фрагмент листа должен быть связан с наблюдением и сущностью либо попасть в `unresolved_regions`.

## Что обязан содержать EngineeringDrawingGraph

- лист, система координат, источник масштаба, рамка и основная надпись;
- виды, разрезы, сечения, локальные системы координат и связи между видами;
- отрезки, окружности, дуги, полилинии, штриховки;
- текст с bbox, базовой точкой, высотой и поворотом;
- размеры с текстом, номиналом, допуском, стрелками, выносными линиями и ссылками на измеряемую геометрию;
- шероховатость, резьбы, геометрические допуски, базы и сварные обозначения;
- типы/толщины линий, принадлежность элементу листа и порядок наложения;
- топологические и инженерные отношения: принадлежность виду, совпадение, касание, параллельность, перпендикулярность, концентричность, размерная привязка и соответствие между видами;
- evidence каждого наблюдения, confidence, альтернативы и неразрешённые области;
- устойчивые идентификаторы, сохраняемые без замены до CadIR, DXF-проекции и базы данных.

## Фазы реализации

### Фаза 1. Строгий контракт и one-to-one drafter

- [x] Введена версионированная Pydantic-схема `EngineeringDrawingGraph`.
- [x] Проверяются уникальность идентификаторов, view/entity refs, координатные границы, dimension refs и relation endpoints.
- [x] Реализовано универсальное преобразование всех поддерживаемых graph-entity в CadIR без эвристик.
- [x] Сохраняются `graph_entity_id == cad_ir_entity_id`, evidence refs и first-class relations в CadIR/БД-проекции.
- [x] Неизвестный тип, битая ссылка, сущность без evidence или unresolved region блокируют результат.
- [x] Контракт покрыт unit-тестами и semantic DXF round-trip benchmark `2/2`.

**Приёмка:** эталонный граф смешанного механического листа воспроизводится с точным совпадением количества, типов, идентификаторов, текста, размеров и отношений; битая ссылка блокирует построение.

### Фаза 2. Reader orchestration и модельные назначения

- [x] Добавлена отдельная задача/слот `cad_drawing_graph_read`, не смешанная с текстовым `cad_spec_read`.
- [-] Manifest показывает graph-reader/coordinator и детерминированный graph-drafter; отдельные назначения layout/geometry/OCR/dimension/symbol/relation появятся вместе со специализированными стадиями.
- [x] Reader может выдавать только `observed|inferred`; самостоятельное повышение assurance отвергается схемой.
- [x] Reader получает overview и source-resolution tiles с картой глобальных координат.
- [x] Экспериментальный reader явно помечен candidate и не получает production authority до прохождения gate.

**Приёмка:** assignment-draft/smoke и UI показывают все назначения; один и тот же manifest полностью воспроизводит reader run.

### Фаза 3. Graph-first режим «По описанию»

- [x] Изображение/PDF сделано обязательным основным входом метода.
- [x] Выполняется `read_drawing_graph → schema/completeness validation → draft_graph → pixel/dimension verification`; непрошедший результат не получает статус точного.
- [x] Свободный текст вынесен в отдельный режим «Начертить по текстовому ТЗ» (`text_spec`).
- [-] Validated graph, hash, manifest и CadIR сохраняются связанно; отдельные raw graph/verifier artifacts ещё предстоят.
- [x] DXF не создаётся при невалидном/неполном graph или blocking unresolved.

**Приёмка:** UI и API больше не смешивают два разных продукта; graph-first run прослеживается от пикселя до DXF-сущности.

### Фаза 4. Полнота листа и независимые verifier-ы

- [-] Pixel evidence coverage: evidence обязателен для каждой сущности, а экспорт проверяется по raster recall/precision; независимое type-aware сопоставление ещё предстоит.
- [ ] Unexplained ink: каждый неописанный фрагмент становится `unresolved_region`.
- [ ] Topology verifier: открытые концы, пересечения, касания, дубли и разрывы.
- [-] Dimension verifier: реализованы независимые проверки linear/diameter/radius и relation refs; tolerances, angular/ordinate и цепочки размеров ещё предстоят.
- [ ] Cross-view verifier: соответствующие признаки видов согласованы.
- [ ] OCR/text completeness и отдельная метрика точного совпадения строк.

**Приёмка:** модель не может сама присвоить `exact_candidate`; ложный полный результат даёт `false_exact = 0` на независимом holdout.

### Фаза 5. Специализированный координатный reader

- [ ] Layout/view/title-block detector полного листа.
- [ ] Type-specific source-resolution heads: segment/endpoint/junction, circle/arc, text, dimension, annotation, hatch.
- [ ] OCR с bbox и чтением технических символов; отдельное связывание OCR с геометрией.
- [ ] Graph assembly и constraint solver вместо абсолютных whole-sheet queries.
- [ ] Профиль mechanical первым; construction/electrical/hydraulic/P&ID проходят независимые gates.
- [ ] Использовать VLM только для классификации/разрешения альтернатив, не как координатный источник истины.

**Приёмка:** native-DXF holdout по профилю проходит promotion gate; до этого сервис остаётся opt-in candidate.

### Фаза 6. Эталонный корпус и метрики

- [-] Добавлен первый версионированный graph truth набор; связка с реальными source raster/PDF и native DXF расширяется далее.
- [ ] Split только по source group; holdout не участвует в подборе.
- [ ] Отдельные метрики по каждому типу, OCR, размерам, отношениям и unexplained ink.
- [-] Exact-graph evaluator проверяет counts, IDs, relation kinds, тексты, размеры и DXF reopen; exact-sheet на реальном holdout ещё предстоит.
- [ ] Двухподписные production-исправления экспортируются в curated active-learning corpus.
- [ ] Replay mix и ранняя остановка по независимому gate, а не по synthetic val-loss.

**Promotion gate:** entity precision/recall ≥ `0.995`, exact-sheet ≥ `0.99`, DXF reopen = `1.0`, false-exact = `0`, blocking unresolved = `0`.

### Фаза 7. UI проверки и дополнения

- [-] UI показывает graph status, число views/entities/relations/evidence/unresolved и назначенные модели; визуализация связей/bbox ещё впереди.
- [-] Список unresolved уже отображается через существующий CadIR review; coverage по типам ещё впереди.
- [ ] Bulk confirm/delete/replace рамкой с сохранением provenance.
- [ ] Показывать модель и revision каждой стадии, graph hash и verifier versions.
- [ ] Давать пользователю дополнять graph без потери устойчивых идентификаторов.

### Фаза 8. Production rollout

- [-] Первый fail-closed запуск на живом стэке выполнен; корпусный read-only pilot ещё не пройден.
- [ ] Editable draft с обязательной review queue.
- [ ] Ограниченный профильный production после gate.
- [ ] Две разные подписи, нормализованная проекция в БД и воспроизводимый release package.
- [ ] Мониторинг false-exact, schema drift, model/config drift и rollback assignment revision.

## Текущий честный статус

- [x] CadIR, DXF renderer, ревизии, review, verifier, две подписи и нормализованная БД-проекция уже существуют.
- [x] Native-DXF entity-level evaluator и fail-closed promotion gate существуют.
- [x] Модели чтения/черчения и reproducibility manifest управляются через UI.
- [x] Вспомогательный text-to-spec drafter поддерживает ограниченные параметрические профили; это не основной acceptance benchmark.
- [-] Multi-type whole-sheet candidate реализован, но отклонён независимым gate и не является production authority.
- [x] EngineeringDrawingGraph v1 и универсальный graph drafter реализованы как первый инкремент; это доказанный контракт, а не доказанная точность reader-а.

## Evidence первого production-инкремента

- contract benchmark: `2/2`, exact-graph `1.0`, DXF reopen `1.0` на версионированных graph truth cases;
- production generation: `c9720590-826b-41b3-9123-9b9de2f5b18d`;
- источник: реальный сложный механический лист A3 `test_vector_files/detal_126.png`, SHA-256 сохранён в manifest;
- время reader run: `106.9 s`;
- результат: `failed`, потому что экспериментальный reader не вернул полный валидный graph;
- частичный DXF не создан, `false_exact = 0` для этого запуска;
- вывод: fail-closed контур работает, текущая универсальная VLM не проходит real-sheet gate и не может быть повышена.

## Правило выполнения

Следующая фаза начинается только после тестируемой приёмки предыдущей. Нельзя заменять exact-graph метрику pixel coverage, считать DXF reopen доказательством правильности или расширять text-to-spec вместо основного graph-first контура.
