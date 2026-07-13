# План развития «Оцифровка в DXF» и CAD-контура

**Статус:** действующий подробный план. **Сверка:** 2026-07-13.
**Источники:** `deep-research-report.md`, `dorabotka_eskd.txt`, текущая реализация.
`[x]` выполнено и проверено; `[-]` частично; `[ ]` не выполнено.

## ⚠️ ПРИОРИТЕТ 2026-07-13: сначала — реально рабочая оцифровка и редактор

Пользователь (2026-07-13) указал: **оцифровка (векторизация) и UI редактора на деле НЕ выполняют свои функции — пользоваться нельзя**, а метрики покрытия (recall 0.96 и т.п.) «с потолка» и не отражают реальность. Пока ядро (растр→годный редактируемый чертёж) не работает по-настоящему, дальнейший марш по разделам roadmap бессмысленен. Проверять качество НЕ по coverage-метрикам, а собственным ВИЗУАЛЬНЫМ сравнением векторизованного результата с исходником на живых примерах. Разделы D3/E3-E5/F/G/H — **отложены**, вернёмся после того, как оцифровка+редактор станут пригодны к работе (см. блок «Надёжная оцифровка raster/PDF» ниже — фактический статус хуже, чем отмеченные `[x]`, т.к. отметки опирались на те самые метрики).

### Что уже сделано за сессию 2026-07-13 (задеплоено, но качество ядра под вопросом)
- B0/B1/B3, B2/B6 (оцифровка/семантика/review UX) — **числа хорошие, реальная пригодность НЕ подтверждена глазом** → перепроверить.
- I1-I3 (раздел `/cad`, импорт DXF, декомпозиция монолита, командная строка, multi-select).
- C2-C5 (профиль ЕСКД, штамп, аннотации, воспроизводимый release).
- E2 (UI проекций), D1/D2 (correspondence graph, provenance 3D), D4-слайс (B-Rep/mass/IGES в ядре).

### Отложено (вернёмся позже)
D3 (revolve/sweep/loft/shell/threads), D4-хвост (self-intersection, ассоц. проекции), E3 (change mgmt), E4 (EBOM/MBOM), E5-хвост (exact interference, exploded), F1-F4 (расчёты/FEA/DFM), G1-G4 (агент/доступ/конфиденциальность/observability), H1-H4 (регрессия/E2E/visual/pilot).

## Целевой результат

Оцифровка должна создавать не пиксельный trace, а проверяемую инженерную модель: исходник -> сегментация и распознавание -> CAD IR -> ручное редактирование/параметризация -> validation/review -> неизменяемый выпуск DXF/PDF/STEP, связанный с инженерной ревизией. CAD IR является источником истины; DXF, SVG, PNG, DWG и 3D являются производными артефактами.

Инварианты: все правки версионируются; ИИ не принимает выпуск; каждое замечание имеет объект и причину; инженерный проект связывается с конкретным CAD-снимком, а не с «последней версией»; конфиденциальный исходник не покидает локальный контур без policy/gate.

## Выполненный фундамент

- [x] CAD IR, история `cad_ir_revisions`, детерминированный повторный рендер PNG/SVG/DXF и инвалидирование approval после правки.
- [x] Scan/image -> CAD IR -> DXF, ручной пустой лист, DXF/DWG ingress и существующий анализ чертежей.
- [x] 2D CAD: selection, pan, line/circle/text/dimension/polyline/hatch, mirror, fillet/chamfer, snapping к точкам/центрам/пересечениям/ортогонали/касательности, undo/redo, слои и review queue.
- [x] CAD IR v3: параметры, ограничения, серверные `set_parameters`/`set_constraints`, детерминированный evaluator и численный solver.
- [x] UI Studio: сохранение параметров, horizontal/vertical constraint для выбранного отрезка, явная команда «Перестроить».
- [x] Многовидовые кандидаты и feature tree; явное построение 3D через CAD kernel/FreeCAD и preview.
- [x] Engineering projects, неизменяемые revision, projections, materials, assemblies, release validation и approval gate.
- [x] Связь project revision с `drawing`, `cad_ir_revision`, `bom`, `manufacturing_process_plan`; выпуск блокируется на непринятом CAD IR.
- [x] Расчетные cases; аналитический `axial_stress`, предел текучести и коэффициент запаса; failed case блокирует validation.
- [x] UI `/engineering` и `/engineering/{projectId}`: проекты, ревизии, материалы, расчеты и выпускная проверка.
- [x] Agent capability manifest/router для инженерного контура.
- [x] Unit/API subset: CAD constraints, multiview и engineering -- `12 passed`; чистые миграции до `20260712_0005`; frontend lint/build; production health smoke.

## Детальный план оставшихся работ

### A. Производственный 2D CAD

- [-] **A1. Constraints UX.** Ядро выполнено 2026-07-13 (`ConstraintsPanel`).
  - [x] Выбор одной/двух сущностей; ссылки на `p1`/`p2`/`center` (coincident/distance — авто по ближайшим опорным точкам `nearestRefs`).
  - [x] UI для coincident, parallel, perpendicular, concentric, equal, distance, angle, radius, diameter, horizontal, vertical — палитра по арности/типу выбора; размерные берут значение. `tangent` не выставлен (нет реализации в солвере).
  - [x] Список ограничений: статус выполнено/нарушено (`GET /ir/constraints/evaluate`, без solve), включение/удаление, подсветка затронутой геометрии по клику, Перестроить (solver). Живьём: parallel+perpendicular на рамке → «остаток 0», solve converged.
  - [ ] Осталось: явные степени свободы (DOF) и конфликт-репорт; expressions, таблицы параметров, конфигурации и driving/driven dimensions.
- [-] **A2. Эскизные операции.** Ядро выполнено 2026-07-13: trim, extend (отказывается укорачивать), offset (отрезок/окружность/дуга, сторона вторым кликом), pattern_linear и pattern_polar (`transform.py` + PATCH-ops + инструменты тулбара/командной строки; `SketchOpError`→422). Живьём на пустом A3: trim 0..200, offset +1, linear +2, polar +3, extend-ошибка 422. Осталось: split/join, construction geometry (флаг + исключение из экспорта/coverage), blocks/insert.
- [ ] **A3. Профессиональный выбор и свойства.** Multi-select, window/crossing, фильтры, property grid, copy/paste, полные keyboard flows.
- [ ] **A4. Слои и стили.** Lock/freeze, lineweight/linetype, именование по ЕСКД, DXF layer mapping.

**Приемка A:** параметризованный эскиз редактируется без JSON; solver объясняет конфликт; DXF открывается независимым CAD без потери основной геометрии и слоев.

### I. Самодостаточный CAD-редактор (/cad)

Сейчас редактор -- монолит `frontend/components/studio/VectorWorkspace.tsx` (~2450 строк, ~40 useState), открывается только полноэкранным оверлеем внутри `/studio` по клику на vectorize-генерацию; ввод значений через `window.prompt`; отдельного маршрута, командной строки, multi-select и открытия произвольного DXF нет. Цель: отдельный AutoCAD-подобный раздел приложения, для которого оцифровка -- лишь один из источников документов. Backend IR API (`/api/image-gen/{id}/ir` + patch/revert/solve/full-check) уже покрывает все нужные операции.

- [x] **I1. Раздел `/cad` и документная модель.** Выполнено 2026-07-12 (playwright-verified на проде): `/cad` -- список чертежей (оцифровки/листы/импорт, поиск, статусы, миниатюры), создание листа, импорт DXF (новый endpoint `POST /api/image-gen/import-dxf` + адаптер `from_dxf.py` -- точная инверсия dxf_render: слои, единицы, y-flip, углы дуг, INSERT-блоки); `/cad/[id]` -- standalone-редактор; студия/очередь/push ведут в `/cad/{id}`.
- [x] **I2. Декомпозиция монолита.** Выполнено 2026-07-12: `frontend/components/cad/` -- geometry.ts (чистые хелперы), EntityShape, CommandLine, StatusBar, ReviewPanel, ValidationPanel, Cad3dPanel (своё состояние feature tree/3D); ядро CadWorkspace с единственным путём мутаций `apply(IrPatchOp[])`; студия показывает ссылку «Открыть в CAD-редакторе» вместо оверлея; старый VectorWorkspace удалён.
- [-] **I3. AutoCAD-подобный UX.** 2026-07-12: командная строка (алиасы RU/EN, undo/redo/delete/confirm/fit, координаты `100,50`/`@50,0`/`@50<45`, history, pending-запросы значений вместо всех `window.prompt`); статус-бар (координаты в мм, OSNAP/жёсткий ORTHO); multi-select (Shift+клик, window/crossing рамка, груповые confirm/delete/line_class, пакетное перемещение стрелками -- закрывает основу A3).
  - [ ] Контекстное меню правой кнопки; автодополнение команд; GRID.
- [x] **I4. Слои и печать.** Выполнено 2026-07-13. `LayersPanel` над фиксированными классами линий ЕСКД: у каждого слоя видны DXF-слой/тип/толщина (`geometry.LAYER_CATALOG`, зеркало backend `LINE_CLASS_LAYERS` + `_LAYER_DEFS`) и три независимых состояния — видимость, lock (рисуется, но инертен и остаётся snap-целью), freeze (не рисуется и не выбирается). Рендер канвы и оба пути выбора (клик, рамка) учитывают lock/freeze; `EntityShape` выключает pointer-events на locked. Печать: новый artifact `pdf` — печатный PDF из мастер-DXF через matplotlib-бэкенд ezdxf (те же слои/типы/толщины), лениво-кэш как DWG; правка ревизии инвалидирует `dwg_path`/`pdf_path`. Закрывает практический A4.
- [x] **I5. Жизненный цикл документа.** Выполнено 2026-07-13. Создание с нуля (`create_blank_sheet` + форма на `/cad`), правка и ревизии (`PATCH /ir` + `cad_ir_store`), экспорт DXF/SVG/PDF без оцифровки (пустой лист получает `dxf_path` при сохранении ревизии, PDF деривируется) — уже работали. Добавлено недостающее: переименование и метаданные — `PATCH /image-gen/{id}/meta` (`title`→`prompt`, `project`/`object`→params), `updateGenerationMeta` + affordance ✎ на карточке `/cad` (не навигирует, `window.prompt`, перезагрузка списка).

**Приемка I:** редактор открывается по прямому URL без студии; чертеж создается с нуля и выпускается в DXF; результат оцифровки открывается по ссылке из уведомления; command line выполняет полный цикл построения без мыши; Playwright smoke на desktop/mobile.

Примечание: A1--A4 выполняются уже внутри нового `/cad`, а не в studio-оверлее.

### B. Надежная оцифровка raster/PDF

Текущее состояние: пиксельное покрытие на golden-файлах приемлемое (technical-vectorizer + CV-арбитраж, 5/5 test_vector_files проходят порог coverage), но практический результат неудовлетворителен: рваная/фрагментированная геометрия («мусор» вместо чертежа), отсутствие семантики (типы линий, размеры), полные отказы на части файлов (5/19 фото baseline) и неудобная проверка. Пункты конкретизированы под эти дефекты.

- [x] **B0. Диагностика на golden-наборе прежде улучшений.** Выполнено 2026-07-12: `eval_vectorize.py --report-dir` (side-by-side src/vec/diff + index.html), отчет `docs/vectorize-b0-diagnosis-2026-07-12.md`. Корневые причины: фрагментация в обоих распознавателях (медиана сегмента 6-14 px), fragmentation-guard арбитража выбирал худший результат (CV recall 0.62 vs neural 0.98), DWG-часть eval была мертва (слои off от dwg2dxf + SORTENTSTABLE).
- [-] **B1. Ingest, предобработка и устранение полных отказов.**
  - [x] Каскад бинаризации Otsu -> adaptive -> Sauvola (ximgproc, только если чище и непусто).
  - [x] Деградация в review-черновик вместо отказа «лист слишком плотный или пустой»: пустой арбитраж -> raster passthrough + рамка/текст + issue `RECOGNITION_EMPTY` (error, блокирует приемку, исчезает после правок); жесткий отказ только на патологии (ink 0 или > 0.85). Попутно исправлена потеря `NEURAL_UNAVAILABLE`/`RECOGNIZER_DISCREPANCY` (validate_ir затирал pipeline-issues).
  - [ ] Dewarp/автоповорот фото, DPI/scale evidence и нормализация страниц.
  - Цель достигнута на корпусе: 0 полных отказов на 29 файлах (2026-07-12).
- [-] **B2. Семантическая сегментация и семантика чертежа.** Frame/title block и OCR-текст есть; довести:
  - [x] Детерминированная классификация типов линий (2026-07-13, `topology._recognize_dash_patterns`): регулярные ряды коллинеарных штрихов -> `hidden` (штриховая) / `axis` (штрихпунктирная, чередование длинных и коротких), нерегулярный разрыв (проём в стене) остаётся отдельными контурами. Не opt-in VLM, покрыто тестами.
  - [x] Реконструкция размеров (2026-07-13, `cad_recognize/dimensions.py`): числовая OCR-метка (Ø/R/M-префикс, допуск-суффикс) паруется с тонкой/короткой размерной линией под ней (перпендикулярная дистанция + проекция в пролёт) -> `DimensionEntity`; расхождение значения с измеренной длиной -> флаг на review, не молчаливая правка; длинные контуры не поглощаются, мусорный OCR (букв больше цифр) отвергается.
  - [ ] Детекция стрелок/выносных линий как отдельных примитивов; штриховка как паттерн (угол/шаг/тип) вместо raster passthrough; symbols (шероховатость, сварка).
- [-] **B3. Чистая геометрия вместо мусора.**
  - [x] Topology repair pass (`backend/app/ai/cad_recognize/topology.py`, применяется к ОБОИМ распознавателям до скоринга в `arbitrate_recognition`): слияние коллинеарных цепочек (union-find по spatial hash, зазор 6 px не сваривает штриховые), удаление дублей/шумовых сегментов < 2 px.
  - [x] Пере-фиттинг цепочек коротких сегментов в дуги/окружности (Kåsa + проверка середин ребер против ложных окружностей на прямоугольниках) и слияние ко-циркулярных дуг в длинные дуги/полные окружности.
  - [x] Fragmentation-guard арбитража исправлен: срабатывает только когда CV сам проходит полный порог покрытия; ratio по собственным сущностям нейросети до CV-supplement. Итог 2026-07-12: порог проходят 29/29 файлов (было 5/19 фото и 0/10 DWG), средний recall фото 0.72 -> 0.95, DWG -> 0.95; neural выбирается в 24/29.
  - [ ] Остаток: окружности с сильным джиттером хорд все еще частично живут дуговыми фрагментами; повторная оценка curve-модели или Deep Sketch Vectorization как второго кандидата.
  - [ ] Метрика успеха -- entity-level (число и тип сущностей против ground truth), не только пиксельный recall (совместно с B4).
- [-] **B4. Corpus и метрики.** Есть `eval_vectorize.py` (cv|neural|arbitrate, DWG ground truth через dwg2dxf) и baseline `test-results/eval_vectorize_baseline.json`; довести: IR/DXF ground truth для test_vector_files, entity precision/recall, topology correctness, OCR/dimension accuracy, DXF opening rate, review effort, false-accept rate и автоматический regression-прогон.
- [ ] **B5. Active learning.** Изолировать принятые правки как обучающие примеры; никаких автоматических production-изменений без review.
- [-] **B6. Review UX в редакторе.** 2026-07-13: слайдер прозрачности исходного растра под вектором; подтверждение масштаба одним шагом -- кнопки A4..A0 (`set_sheet_format` PATCH-op вычисляет мм/px из формата и pixel-span рамки `sheet.frame_px`); очередь review с подсветкой на канвасе (ReviewPanel из I2).
  - [ ] Приём/отклонение сущностей рамкой по области (window/crossing уже есть для select -- добавить bulk confirm/delete из этого выбора в очередь review).
- [ ] **B7. Дообучение technical-vectorizer.** Fine-tune предобученной line-модели на собственном корпусе реальных фото (источник примеров -- B5); только после измеримой базы B0/B4.

**Приемка B:** каждый recognized entity имеет provenance/confidence; критичные размеры не принимаются автоматически; на golden-корпусе 0 полных отказов; entity-level precision/recall публикуются; результат на чистом цифровом чертеже (`detal_126.png`) визуально неотличим от исходника; типы линий и размеры присутствуют как сущности, а не как текст поверх отрезков.

### C. ЕСКД, нормоконтроль и выпуск

- [-] **C1. Базовые deterministic checks** уже есть; расширить покрытие.
- [x] **C2. Машиночитаемый профиль ЕСКД.** Выполнено 2026-07-13: версионируемый реестр `backend/app/ai/eskd_profile.py` (`ESKD_PROFILE_VERSION`) — каждое правило хранит стабильный `rule_id` (напр. `ESKD.2.303.line_weight`), ГОСТ с годом+пунктом, level, severity, `fix_hint` (путь исправления). `cad_validate.eskd_issue()` штампует rule_id/fix_hint/цитату на каждое ЕСКД-замечание; версия профиля пишется в отчёт (`validation.eskd_profile_version`). Покрыты ГОСТ 2.301/2.302/2.303/2.104/2.109/2789 + новые 2.304 (высота шрифта из ряда, scale-aware) и 2.307 (размер обязан иметь числовое значение). UI: панель валидации показывает цитату и подсказку исправления. `norm_ref` остаётся голой цитатой (совместимость с резолвером корпуса). 40+ тестов.
- [-] **C3. Лист и штамп.** Выполнено 2026-07-13 (осн. надпись): `backend/app/ai/cad_ir/title_block.py` + PATCH-op `set_title_block` — структурированные поля формы 1 ГОСТ 2.104 (обозначение, наименование, материал, масштаб, масса, литера, лист/листов, подписи, предприятие) хранятся в `ir.sheet.title_block.fields` и рендерятся в ячейки штампа (пропорционально региону — работает и на пустом листе, и на скане); рисует рамку+сетку штампа, если их нет; идемпотентно. ЕСКД-чек полноты штампа судит по обозначение+наименование. Frontend: сворачиваемая панель `TitleBlockPanel`. Осталось: редактор зон листа.
- [x] **C4. Структурные аннотации.** Выполнено 2026-07-13: единый `AnnotationEntity` (kind: roughness/thread/tolerance/datum/weld) вместо свободного текста; `cad_ir/annotations.py` строит каноничный текст (ряд Ra, глифы ГОСТ 2.308, рамка для допуска/базы) и валидирует каждый kind по стандарту (Ra ГОСТ 2789, резьба ГОСТ 8724, символ допуска ГОСТ 2.308, база — одна буква). Рендер во всех трёх целях (PNG пропускает как текст; SVG/DXF — текст+выноска+рамка) + EntityShape; правило профиля `ESKD.2.308.annotation`. Frontend: AnnotationsPanel добавляет через существующий add-op, экспорт в DXF.
- [x] **C5. Release package.** Выполнено 2026-07-13: `backend/app/services/cad_release.py` + endpoints `GET /release-manifest` (409 до приёмки/при блокирующих) и `GET /release-package` (zip DXF/SVG/CAD IR + manifest.json). Манифест связывает CAD IR ревизию + её hash, hash артефактов с детерминированным re-render чеком, отчёт валидации (+eskd_profile_version), approval trail — под одним manifest_sha256. DXF сделан байт-детерминированным (фикс GUID + нормализация timestamp ezdxf) → воспроизводимость реальна. Frontend: кнопка выпуска + сводка воспроизводимости/приёмки.

**Приемка C:** выпуск воспроизводим по hash и блокируется на обязательном ЕСКД-нарушении. ✅ достигнуто (C5).

### D. Многовидовая реконструкция и 3D

- [x] **D1. Candidates/3D preview + correspondence graph.** Выполнено 2026-07-13: `backend/app/ai/cad_ir/correspondence.py` строит граф соответствий между ортогональными видами по 4 признакам — выравнивание осей (спереди↔сверху вертикаль, спереди↔сбоку горизонталь), Ø-метка↔окружность в ортогональном виде, скрытый контур↔видимое отверстие, согласованность масштаба (расхождение — в issues). Подтверждённые соответствия поднимают score кандидата, рассогласования осей/масштаба → missing_data. Ноты в `MultiViewCandidate.correspondences` (endpoint reconstruction-candidates); адаптер читает выравнивание из bbox+section_type, что уже хранят строки видов. Тесты 12.
- [x] **D2. Связность 2D <-> 3D.** Выполнено 2026-07-13: каждый параметр `Feature3D` несёт `ParamProvenance` (origin: measured/stated/guessed/propagated + source_entity_id/source_parameter). Глубина «с чертежа» трассируется к своей размерной сущности; диаметр совпадающий с именованным параметром эскиза → propagated (2D-параметр правит 3D); эвристика → guessed. 3D-панель показывает цветной бейдж происхождения у глубины выдавливания. Тесты 14.
- [ ] **D3. Операции.** Revolve, sweep, loft, shell, draft, pattern, threads; не разрушать последнюю валидную ревизию при ошибке. (Пока: extrude/hole/boss/pocket/fillet/chamfer в cad-kernel; revolve/sweep/loft/shell/threads — не начаты, требуют новых kind в схеме + FreeCAD-операции.)
- [-] **D4. Exact geometry.** Выполнено 2026-07-13 (ядро): cad-kernel вместо hardcoded `valid=True` даёт честную проверку OpenCascade — `brep_valid` (Shape.isValid), `manifold` (isClosed/watertight) + массовые характеристики (площадь, центр масс, масса по плотности материала из справочника); экспорт IGES рядом со STEP (декодер принимает опционально, скачивается как artifact kind). 3D-панель показывает массу и бейдж валидности B-Rep. Осталось: явная self-intersection-проверка отдельно от isValid, ассоциативные проекции/сечения.

**Приемка D:** нехватка данных дает несколько кандидатов с предположениями (✅ D1/D2); утвержденная модель воспроизводимо экспортируется в STEP (✅), теперь с честной B-Rep-валидацией и IGES (✅ D4).

### E. PDM/PLM и сборки

- [-] **E1. Revision foundation** и CAD snapshot gate готовы.
- [x] **E2. UI проекций.** Выполнено 2026-07-13 (playwright-verified): `frontend/components/engineering/ProjectionsPanel.tsx` на странице проекта -- список проекций ревизии (CAD IR/чертёж/BOM/техпроцесс) с бейджем актуальна/устарела, коротким id, датой; deep-link «Открыть» для CAD IR (через `metadata.generation_id`) и чертежей; форма связывания артефакта с непринятой ревизией. Backend был готов.
- [ ] **E3. Change management.** Change request/order, причина, impact analysis, affected revisions, reviewers, signatures, supersession.
- [ ] **E4. EBOM/MBOM.** Positions, quantity, units, variants, substitutes, reference designators, where-used и mapping к технологии/закупке.
- [-] **E5. Assemblies.** Есть instances/mates и AABB collision; добавить exact B-Rep interference, mate solve, DOF, exploded view и спецификацию.

**Приемка E:** нельзя выпустить DXF или техпроцесс из stale/непринятого источника; change order показывает impact до approval.

### F. Расчеты и технологичность

- [-] **F1. Axial stress** готов; добавить изгиб, кручение, устойчивость, контакт, тепловое расширение с units/assumptions.
- [ ] **F2. Solver jobs.** Immutable input snapshot, версия solver/mesh/material card, artifacts, cancel/retry.
- [ ] **F3. FEA.** Loads, restraints, mesh, convergence, stress/displacement views; failed/non-converged result блокирует выпуск.
- [ ] **F4. DFM/DFA.** Толщины, радиусы инструмента, сверление, резьбы, доступ инструмента, допуски, заготовка, КИМ и workflow исправления в технологии.

### G. Агент, безопасность и эксплуатация

- [ ] **G1. Agent scenarios.** Dry-run/trace для ingest -> digitize -> review -> parameterize -> validate -> draft release.
- [ ] **G2. Access policy.** Роли конструктора, нормоконтролера, технолога, расчетчика и руководителя; scope проекта/revision lock.
- [ ] **G3. Confidentiality.** Local-only source processing, redaction/export gate для внешних моделей, audit reason каждого tool call.
- [ ] **G4. Observability/DR.** Pipeline/solver/export metrics, очереди, backup/restore rehearsal, object storage versioning и hash audit.

### H. Тестирование и rollout

- [ ] **H1. Golden regression.** scan -> IR -> DXF -> independent parse -> ESKD validation.
- [ ] **H2. Playwright E2E.** Keyboard CAD, review acceptance, parameterization, release gate, project workflow, desktop/mobile.
- [ ] **H3. Visual regression.** CAD canvas/3D viewport nonblank, framing, selection/overlays на desktop/mobile.
- [ ] **H4. Performance/pilot.** Большие листы, PDF, сборки, parallel solver jobs; rollout read-only import -> editable draft -> limited production release.

## Очередность следующих работ

Приоритет пересмотрен 2026-07-12 по фактической боли: оцифровка дает «мусор» вместо чертежа, редактор не самодостаточен.

1. ✅ B0 + B1 + B3: диагностика на golden-наборе, устранение полных отказов и чистая геометрия.
2. ✅ I1-I3: самодостаточный `/cad` -- маршруты, декомпозиция монолита, командная строка и multi-select.
3. ✅ B2 + B6: семантика чертежа (типы линий, размеры, штриховка) и review UX в редакторе.
4. ✅ I4-I5 + A1 (ядро): слои/печать (lock/freeze + PDF), жизненный цикл документа (rename/метаданные), constraint editor (палитра+список+evaluate+solve). Осталось от A1: DOF/конфликты, expressions/параметрика.
5. ✅ E2: UI связей инженерной ревизии (проекции current/stale) — выполнено 2026-07-13.
6. ✅ C2-C4 (+C5): формализованный ЕСКД-профиль, штамп и структурные аннотации, воспроизводимый release.
7. **СЛЕДУЮЩЕЕ** — A1-хвост (DOF/expressions) + A2 (эскизные операции: trim/extend, offset, pattern) + B4/B7/H1: entity-level метрики, дообучение technical-vectorizer и golden regression.
8. D1-D4 (✅ D1/D2/D4 ядро) + D3 (revolve/sweep/loft/shell/threads): многовидовая увязка, B-Rep и STEP.
9. E3-E5, затем F1-F4, G1-G4 и H2-H4.

## Доказательства текущего состояния

- Миграции: `20260712_0001`--`20260712_0005`, чистая база до head.
- Последние внедрения: `3a2e3a1`, `30d4804`, `fd2dffd`, `8685fa1`.
- Production backend содержит `CAD_IR_NOT_APPROVED`; health endpoint отвечает.
