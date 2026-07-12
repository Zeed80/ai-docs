# План развития «Оцифровка в DXF» и CAD-контура

**Статус:** действующий подробный план. **Сверка:** 2026-07-12.
**Источники:** `deep-research-report.md`, `dorabotka_eskd.txt`, текущая реализация.
`[x]` выполнено и проверено; `[-]` частично; `[ ]` не выполнено.

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

- [-] **A1. Constraints UX.** База и два типа в UI готовы.
  - [ ] Выбор одной/двух сущностей и ссылок на `p1`/`p2`/`center`.
  - [ ] UI для coincident, parallel, perpendicular, tangent, concentric, equal, distance, angle, radius, diameter.
  - [ ] Список ограничений: residual, включение/удаление, подсветка затронутой геометрии, конфликт и степени свободы.
  - [ ] Expressions, таблицы параметров, конфигурации и driving/driven dimensions.
- [ ] **A2. Эскизные операции.** Trim/extend, offset, split/join, construction geometry, rectangular/polar pattern, blocks/insert.
- [ ] **A3. Профессиональный выбор и свойства.** Multi-select, window/crossing, фильтры, property grid, copy/paste, полные keyboard flows.
- [ ] **A4. Слои и стили.** Lock/freeze, lineweight/linetype, именование по ЕСКД, DXF layer mapping.

**Приемка A:** параметризованный эскиз редактируется без JSON; solver объясняет конфликт; DXF открывается независимым CAD без потери основной геометрии и слоев.

### B. Надежная оцифровка raster/PDF

- [-] **B1. Ingest и предобработка.** Есть входные форматы и базовый pipeline; завершить deskew, denoise, DPI/scale evidence и нормализацию страниц.
- [-] **B2. Семантическая сегментация.** Довести frame/title block, views, text, dimensions, hatch и symbols до измеряемого качества.
- [-] **B3. Геометрия.** Усилить primitive fitting, centerline tracing и topology repair вместо outline tracing.
- [ ] **B4. Corpus и метрики.** Эталонные реальные сканы/синтетические дефекты, IR/DXF ground truth, entity precision/recall, topology correctness, OCR/dimension accuracy, DXF opening rate, review effort и false-accept rate.
- [ ] **B5. Active learning.** Изолировать принятые правки как обучающие примеры; никаких автоматических production-изменений без review.

**Приемка B:** каждый recognized entity имеет provenance/confidence; критичные размеры не принимаются автоматически; качество публикуется на golden set.

### C. ЕСКД, нормоконтроль и выпуск

- [-] **C1. Базовые deterministic checks** уже есть; расширить покрытие.
- [ ] **C2. Машиночитаемый профиль ЕСКД.** Версионировать правила ГОСТ 2.301, 2.302, 2.303, 2.304, 2.307, 2.109; в замечании хранить правило, объект и путь исправления.
- [ ] **C3. Лист и штамп.** Редактор рамки, основной надписи, формата, масштаба, зон, обозначения, материала, массы и подписей.
- [ ] **C4. Структурные аннотации.** Размерные стили, допуски/посадки, базы, шероховатость, сварка и резьбы как CAD-сущности.
- [ ] **C5. Release package.** DXF R2010/R2013, SVG/PDF, DWG conversion report, manifest hashes, CAD IR revision, validation report и approval trail.

**Приемка C:** выпуск воспроизводим по hash и блокируется на обязательном ЕСКД-нарушении.

### D. Многовидовая реконструкция и 3D

- [-] **D1. Candidates/3D preview** готовы; расширить correspondence graph: оси, скрытые линии, диаметры, сечения и масштабы между видами.
- [ ] **D2. Связность 2D <-> 3D.** Propagation параметров и явное происхождение каждого 3D-размера.
- [ ] **D3. Операции.** Revolve, sweep, loft, shell, draft, pattern, threads; не разрушать последнюю валидную ревизию при ошибке.
- [ ] **D4. Exact geometry.** B-Rep validation, manifold/self-intersection, mass properties, STEP/IGES export и ассоциативные проекции/сечения.

**Приемка D:** нехватка данных дает несколько кандидатов с предположениями; утвержденная модель воспроизводимо экспортируется в STEP.

### E. PDM/PLM и сборки

- [-] **E1. Revision foundation** и CAD snapshot gate готовы.
- [ ] **E2. UI проекций.** В проекте выбрать и показать CAD IR/Drawing/BOM/technology, current/stale, manifest и acceptance evidence.
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

1. E2: UI связей инженерной ревизии с конкретными CAD/Drawing/BOM/technology артефактами.
2. A1: полный constraint editor и диагностика solver.
3. C2-C4: формализованный ЕСКД-профиль, штамп и структурные аннотации.
4. B4-B5/H1: golden corpus и измеримые метрики до расширения AI pipeline.
5. D1-D4: многовидовая увязка, B-Rep и STEP после стабилизации 2D-регрессии.
6. E3-E5, затем F1-F4, G1-G4 и H2-H4.

## Доказательства текущего состояния

- Миграции: `20260712_0001`--`20260712_0005`, чистая база до head.
- Последние внедрения: `3a2e3a1`, `30d4804`, `fd2dffd`, `8685fa1`.
- Production backend содержит `CAD_IR_NOT_APPROVED`; health endpoint отвечает.
