# Оставшийся TO DO: универсальное чтение чертежей, 3D/BIM и выпуск 2D

**Статус:** выполняется последовательно.  
**Главный план:** [`CAD_UNIVERSAL_BENCHMARK_PLAN.md`](./CAD_UNIVERSAL_BENCHMARK_PLAN.md).  
**Область:** машиностроительные и строительные чертежи; чтение, semantic truth, восстановление 3D/BIM, обязательные 2D-виды, проверка и редактор.  
**Не входит:** оформление рамки и основной надписи, если они не нужны для связи листов или масштаба.

Обозначения: `[x]` завершено и проверено, `[-]` выполняется/частично, `[ ]` не начато, `[!]` заблокировано внешним условием.

## 0. Неподвижные правила приёмки

- [x] Не принимать визуально похожую картинку за точную геометрию.
- [x] Считать источником истины STEP/B-Rep, IFC и официальные semantic definitions; raster/vector PDF — evidence, но не замена семантике.
- [x] Разделять `mechanical` и `construction` схемы, валидаторы и promotion gates.
- [x] Делить train/dev/holdout по `source_group` до растеризации и дефектов.
- [x] Публиковать precision, recall, F1, missing/invented и false-exact по каждому классу, а не только среднее.
- [x] Неполный граф, неизвестный класс или неоднозначная геометрия завершаются `blocked`/`review_required`, а не выдуманным exact-результатом.
- [x] В production выпускать только модель и виды, прошедшие соответствующий доменный валидатор.

## 1. Зафиксировать управляемый план и исходную точку

- [x] Создать этот исполнимый TO DO с зависимостями, артефактами и критериями готовности.
- [x] Связать его с `PLAN.md`, `DEVPLAN.md` и главным benchmark-планом.
- [-] Снять воспроизводимый snapshot версий кода, manifest, моделей и runtime-конфигурации: NIST PMI baseline сохраняет фактическое production assignment, thinking flag, passes и метрики; commit/config/source hashes для общего multi-domain snapshot ещё нужны.
- [-] Прогнать текущий live stack без подстройки на фиксированном наборе mechanical/construction кейсов: завершён truth-linked NIST PMI прогон `27/27` листов; общий mechanical/construction API-набор ещё не завершён.
- [-] Сохранить исходные API payload, audit trace, 3D/BIM, 2D и метрики как immutable baseline: сохранён компактный PMI baseline и полный checkpoint-отчёт; единый immutable bundle для всех доменов ещё нужен.

**Готово, когда:** baseline можно повторить одной командой, а отчёт содержит commit, config hash, source-group hashes и ссылки на артефакты.

## 2. Семантическая PMI-истина — текущий блок

### 2.1. NIST source truth

- [x] Разобрать официальные `NIST-CTC-PMI-Definitions.xlsx` и `NIST-FTC-PMI-Definitions.xlsx` как первичный semantic truth: получено `100` записей (`50 CTC`, `50 FTC`) по `11` деталям.
- [x] Нормализовать идентификаторы CTC/FTC/ATC без потери исходных значений.
- [x] Сохранить для каждой записи категорию, описание test case, specification, measurand/comments и standards mapping.
- [x] Связать semantic record с исходным STEP, PDF, листами IR и element-ID приложениями; официальное page membership найдено для `98/100` записей (`105` связей-кандидатов).
- [x] Не объявлять связь PMI с гранью/кромкой без подтверждения: все `100/100` NIST STEP явно маркированы авторами как `geometry only`.
- [x] Пометить уровень доказательства: сейчас `source_defined`; topology и точный drawing bbox остаются `unresolved`.
- [x] Сформировать воспроизводимые JSONL truth и summary по suite, case и PMI category.

### 2.2. PMI evaluator

- [x] Добавить schema validator с fail-closed проверкой обязательных полей и уникальности semantic ID.
- [x] Сравнивать exact source spelling и отдельное нормализованное представление.
- [x] Считать precision/recall/F1 по semantic record и отдельно topology/drawing associations.
- [x] Считать `invented_pmi_rate`, `missing_pmi_rate`, attachment metrics и `false_exact_rate`.
- [x] Запрещать promotion при пустой истине, дубликатах, неизвестной связи или ложном `verified`.
- [x] Добавить unit/CLI tests, self-pair semantic pass и deliberate-corruption rejection; `18` смежных тестов проходят.
- [x] Зафиксировать честный PMI baseline текущего reader: полный production-прогон обработал все `27` truth-linked листов (`98` официальных записей) без runtime errors. Получено `440` кандидатов, `7` точных записей, semantic precision `0,015909`, recall `0,071429`, F1 `0,026022`, missing `92,86%`, invented `98,41%`, false-exact `0%`; promotion запрещён. Диагностика источников показала `0/228` точных dimension-кандидатов и `7/212` annotation-кандидатов (annotation-only F1 `0,045161`).
- [x] Добавить специализированный structured-PMI проход с явным characteristic, tolerance text и упорядоченными datum refs; пустые/unknown рамки остаются unresolved.
- [x] Исключить заголовок, номер test model, формат/масштаб и пустые annotation из PMI observations.
- [x] Добавить возобновляемый live evaluator с checkpoint после каждого листа и возможностью закрепить reader model.
- [x] Разделить в evaluator кандидаты `dimension`/`annotation`, сохранив основной строгий all-candidate score неизменным.
- [x] Нормализовать неполные profile/hole-pattern inputs в точные `geometry_input_incomplete:*` причины до общей schema validation. Production smoke больше не дал общего schema reject: observations сохранены, `profile=null`, `observation_only=true`, точный blocker доступен пользователю; single-page strict F1 вырос с `0,071429` до `0,153846` за счёт structured PMI.
- [-] Сегментировать/локализовать реальные feature-control frames до VLM и требовать evidence bbox: VLM locator (`2/6` регионов) заменён детерминированной группировкой соседних наклонных ячеек. На первом live-листе CV нашёл `6/6` рамок; crop ↔ whole-view crosscheck дал bbox обоим точным semantic records, не присвоил bbox ошибочным guesses и удалил дубликат. Полный диагностический прогон обработал `27/27` truth-linked листов: `147` регионов, `2` листа без регионов, медиана `5`, диапазон `0–14`. Это не coverage/recall: официальная истина не содержит точных bbox и связей region → semantic record, поэтому promotion остаётся запрещён до ручной/независимой bbox-разметки.
- [x] Добавить отдельные semantic adapters для basic/reference dimensions, datum features/targets, dimension symbols и notes вместо приравнивания любого обычного размера к PMI. Адаптер v2 на сохранённых результатах тех же `27` production-чтений исключил обычные размеры и неклассифицированные notes: кандидаты `440 → 312`, точные записи `7 → 8`, precision `0,015909 → 0,025641`, recall `0,071429 → 0,081633`, F1 `0,026022 → 0,039024`; promotion по-прежнему запрещён.

**Готово, когда:** любой PMI score прослеживается до официальной строки NIST и evidence; неподтверждённая привязка не становится verified.

## 3. Balanced mechanical corpus и feature truth

### 3.1. Покрытие классов

- [ ] Тела вращения: валы, оси, втулки, ступицы, ролики, шпиндели, штуцеры.
- [ ] Призматические детали: плиты, корпуса, кронштейны, траверсы, крышки.
- [ ] Фланцы и круговые массивы.
- [ ] Листовые детали: гибы, отбортовки, вырезы, развёртки, линии сгиба.
- [ ] Литые/кованые детали: уклоны, бобышки, рёбра, переходы, радиусы.
- [ ] Сварные конструкции: профили, швы, разделка, сборочные базы.
- [ ] Advanced: шестерни, шлицы, звёздочки, кулачки, пружины.
- [ ] Сборки: позиции, BOM, отдельные компоненты и сопряжения.
- [ ] Стандартные изделия: резьба, подшипники, шпонки, стопорные элементы.
- [ ] Виды: основные/дополнительные/местные, разрезы, сечения, обрывы, выносные элементы.

### 3.2. Истина и генерация листов

- [ ] Для каждого класса собрать минимум dev и sealed-holdout source groups с ясной лицензией.
- [ ] Извлечь B-Rep topology signature, bbox, area, volume и component structure.
- [ ] Построить feature truth с параметрами, зависимостями и привязками к topology IDs.
- [ ] Детерминированно выпустить ортогональные/аксонометрические виды, HLR и необходимые разрезы.
- [ ] Связать размерные объекты с feature/topology targets.
- [ ] Добавить контролируемые raster/degradation варианты только после source-group split.
- [ ] Сформировать class-balanced manifest; не позволять массовому простому классу скрывать провал редкого.

### 3.3. Mechanical promotion gates

- [ ] B-Rep повторно открывается, manifold/solid/shell status соответствует эталону.
- [ ] Bbox, area, volume, boolean IoU и surface distance проходят class-specific допуски.
- [ ] Feature precision/recall и invented-feature rate проходят пороги.
- [ ] Обязательные виды/разрезы полны и геометрически согласованы с одной 3D-ревизией.
- [ ] Размеры и PMI относятся к правильным topology/feature объектам.
- [ ] Сборка сохраняет компоненты, позиции и сопряжения без слияния.

## 4. Balanced construction corpus и BIM truth

### 4.1. Покрытие классов

- [ ] Архитектура: планы, фасады, разрезы, кровля, помещения, проёмы, лестницы, отделка.
- [ ] Конструкции: оси, фундаменты, колонны, балки, стены, плиты, фермы, армирование, узлы.
- [ ] Генплан: здания, дороги, площадки, отметки, координационные оси.
- [ ] ОВ/ВК: оборудование, воздуховоды, трубы, фитинги, стояки, уклоны, подключения.
- [ ] Электрика/СС: устройства, трассы, цепи, щиты, кабели, условные обозначения.
- [ ] Потолки, полы, кладочные планы и спецификации.
- [ ] Многолистовые ссылки, продолжения сетей и марки узлов.
- [ ] Реконструкция: существующее/демонтаж/новое как отдельные фазы.

### 4.2. Источники и истина

- [ ] Добавить открытые IFC + PDF/DWG комплекты с проверяемой лицензией и общей ревизией.
- [ ] Зафиксировать IFC GUID, class, type, placement, geometry, storey, space, system и containment.
- [ ] Извлечь openings, MEP ports, connectivity graph и phase/status.
- [ ] Получить независимые quantity truth: counts, lengths, areas, volumes.
- [ ] Детерминированно выпустить планы этажей, фасады, разрезы и узлы с GUID evidence.
- [ ] Валидировать межлистовые ссылки и принадлежность одной BIM-ревизии.
- [ ] Исключить unclear-rights, generic-proxy-only и неполные источники из promotion.

### 4.3. Construction promotion gates

- [ ] IFC schema/STEP повторно открывается без GUID duplicates и geometry failures.
- [ ] Совпадают class/type counts без подмены generic proxy.
- [ ] Совпадают site/building/storey/space/opening и containment.
- [ ] Совпадают MEP systems, ports и graph connectivity.
- [ ] Placement, bbox, area/volume и projections проходят допуски.
- [ ] Обязательный drawing set полный и ссылается на одну BIM revision.

## 5. Runtime: чтение и строгие графовые контракты

- [-] Добавить domain router с confidence, evidence и исходами `mechanical/construction/mixed/unknown`: пользователь теперь явно выбирает `auto`, тело вращения, произвольную механическую деталь, сборку, строительную конструкцию, архитектурный чертёж, ОВ/ВК, электрику, гидравлику или P&ID; выбор нормализуется backend-контрактом, сохраняется в manifest/audit и задаёт доменный профиль. Автоматический evidence/confidence router для `auto/mixed/unknown` ещё нужен.
- [ ] Для mixed/unknown запрещать запуск неподходящего генератора без review.
- [ ] Принимать mechanical input только как полный валидный `EngineeringDrawingGraph`.
- [ ] Принимать construction input только как полный валидный BIM/drawing graph.
- [ ] Ввести стабильные entity IDs, связи видов, topology targets, provenance bbox/page и alternatives.
- [ ] Валидировать единицы, масштаб, систему координат, обязательные виды и противоречия размеров.
- [x] Документировать каждый этап: durable timeline хранит последовательность, длительность, модель, thinking flag, prompt/answer SHA, raw/parsed outcome, ошибки и монотонный прогресс; полные prompt/answer/thinking загружаются отдельным ownership-protected API, а точный kernel payload имеет canonical SHA.
- [x] Вернуть для назначений модели явный `thinking=false` и проверить, что провайдер действительно его получает; CAD-вызовы также задают ограниченный `num_ctx` на запрос и детерминированную `temperature=0`.
- [x] Сохранять валидный промежуточный consensus после каждого прохода и продолжать с ним при исчерпании общего reader deadline; не начинать проход, который по измеренной длительности уже не помещается в бюджет.
- [x] Не повторять дорогой OCR/model-swap в каждом consensus-проходе и пропускать noisy whole-view PMI fallback, если PMI уже надёжно локализован.
- [ ] Не запускать 3D/BIM при missing critical parameters; вернуть точный список вопросов/блокеров.
- [x] Не запускать механический spec-генератор для явно выбранных сборок, строительных, архитектурных, MEP и схемных типов; для типа «тело вращения» требовать осевой профиль `main_view.outer` и возвращать точный blocker при несовместимом чтении.

## 6. Генерация 3D/BIM и обязательного 2D

### 6.1. Mechanical

- [ ] Генерировать параметрические операции из validated graph, а не из свободного текста.
- [ ] Поддержать revolve/extrude/cut/hole/pattern/fillet/chamfer/sweep/loft/sheet-metal и assembly components по class roadmap.
- [ ] После каждой операции сохранять topology mapping и проверять инварианты.
- [ ] Выпускать front/top/side по необходимости, sections/details и HLR из фактического B-Rep.
- [ ] Размеры и PMI размещать из semantic graph, не дорисовывать отсутствующие значения.

### 6.2. Construction

- [ ] Генерировать IFC classes/types/relations из BIM graph без generic proxy fallback для verified объектов.
- [ ] Сохранять storeys, spaces, openings, systems, ports, phases и containment.
- [ ] Выпускать планы/фасады/разрезы/узлы из одной сохранённой BIM revision.
- [ ] Проверять межлистовую согласованность и quantity truth после генерации.

### 6.3. Общий выпуск

- [ ] Сохранять STEP/IFC, preview, DXF/SVG/PDF видов, validation report и audit trace.
- [ ] Не маркировать результат `verified`, пока доменные gates не пройдены.
- [ ] Добавить regression tests на прежние блокеры `detal_126.png` и минимум по одному кейсу каждого поддержанного класса.

## 7. Редактор и прозрачность процесса

- [x] Экран «Что прочитала модель»: исходная и исправленная спецификация, сущности, размеры, PMI, виды, unresolved, assumptions и consensus.
- [x] Экран «Что передаётся в генератор»: точный validated kernel JSON, canonical SHA и итог нормализации.
- [-] Side-by-side связь entity ↔ bbox/page ↔ view/topology/BIM object: в CAD-редакторе выбранная immutable operation/Feature раскрывается в assertion, точный source crop/лист/bbox и серверный dependency impact до build operations, topology IDs и artifacts. Остались межвидовые и BIM-проекции в том же UX.
- [x] Отдельно показывать observed, inferred, user-corrected и unresolved: provenance-инспектор не повышает assurance на клиенте и явно маркирует каждый активный assertion.
- [-] Показать блокеры, альтернативы и влияние каждой неопределённости на 3D/виды: критичность target и dependency closure уже показаны; hypothesis alternatives и переход прямо к блокеру ещё не объединены с CAD-редактором.
- [x] Дать безопасное редактирование параметров и связей с повторной локальной валидацией: каждый принятый human GraphPatch возвращает отдельный `dependency_validation` с изменёнными assertions, target-specific closure, критичными assertions и затронутыми operations/topology/artifacts. Отчёт честно фиксирует `geometry_validated=false` до kernel rebuild.
- [-] После правки пересчитывать только зависимые узлы и сохранять audit diff/author/time: immutable audit diff/actor/time и точный dependency closure выполнены. CAD-kernel теперь переиспользует detached BREP неизменённых независимых `body_index` из bounded LRU, повторно валидирует cache hit и пересобирает только изменённые тела; STEP/IGES/STL/topology и общий reopen всё равно выпускаются заново. Внутри одного order-sensitive тела остаётся честный full rebuild — operation-level checkpoints нельзя включать до стабильного контракта промежуточной топологии.
- [x] Отделить конструкторские изменения от исходной оцифровки и добавить immutable undo/redo: восстановление старого дерева создаёт новую design-ревизию, требует причину, повторно компилирует CadIR/3D и инвалидирует подписи.
- [x] Добавить фильтры ошибок, навигацию клавиатурой, масштабирование и удобный выбор мелкой геометрии: дерево фильтруется по операциям с предположениями/без них и точным kernel/critical-assertion кодам, поддерживает `ArrowUp`/`ArrowDown`/`Home`/`End`; 3D-вьюпорт имеет zoom/fit controls и приближение к измеренному kernel bbox. Отдельный topology picker показывает стабильные `face-*`/`edge-*` IDs, тип и площадь/длину, сортирует мелкие элементы первыми, ищет по ID/индексу/типу и подсвечивает выбранную геометрию без необходимости попасть в неё мышью.
- [x] Показывать историю этапов, проценты и текущую операцию; по запросу пользователя показывать полный prompt, answer, thinking и признак успешного JSON-разбора для каждого model call, не раздувая фоновый polling.
- [ ] Добавить download всех диагностических артефактов одним пакетом.

## 8. Итерационный цикл качества

- [ ] Сформировать минимальное число source groups на каждый класс и уровень сложности.
- [ ] Снять class-balanced baseline reader → graph → 3D/BIM → 2D.
- [ ] Кластеризовать ошибки: routing, OCR/symbol, view association, parameter, feature, topology, BIM relation, connectivity, projection, editor.
- [ ] Исправлять общий класс ошибки; запрещена подгонка по имени файла/fixture.
- [ ] После изменения прогонять unit + domain dev + corruption tests.
- [ ] Публиковать before/after по каждому классу и regression delta.
- [ ] Отклонять изменение, улучшающее среднее ценой invented geometry или деградации критического класса.
- [ ] Не запускать sealed holdout до promotion-кандидата.

## 9. Финальная приёмка и production

- [ ] Заморозить candidate commit, config/model hashes и manifests.
- [ ] Однократно прогнать sealed mechanical и construction holdout.
- [ ] Проверить leakage и отсутствие holdout в prompt examples/manual tuning.
- [ ] Проверить все promotion gates и честные `blocked/review_required` исходы.
- [ ] Выполнить `make prod-build`, перезапустить stack и дождаться устойчивого `/health`.
- [ ] Прогнать публичный рабочий URL, а не только health endpoint.
- [ ] Для live-кейсов сохранить исходник, audit trace, прочитанный graph, generator payload, 3D/BIM, 2D и validation report.
- [ ] Дать пользователю живые URL и точные шаги самостоятельной проверки.
- [ ] Зафиксировать итоговый before/after отчёт, commit и push без generated/local noise.

## Definition of Done

- [ ] Оба домена имеют лицензированный source-grouped воспроизводимый корпус и class-balanced отчёт.
- [ ] Каждый promotion-кейс имеет geometry/semantic/drawing truth достаточной силы.
- [ ] Runtime не принимает неполный граф и не заявляет точность без evidence.
- [ ] Пользователь видит, что модель прочитала и что именно передано генератору.
- [ ] Поддерживаемые классы дают валидную 3D/BIM и полный необходимый 2D-комплект.
- [ ] Неподдерживаемые или неоднозначные случаи честно останавливаются с конкретными блокерами.
- [ ] Production-проверка воспроизводима по живым URL и сохранённым артефактам.
