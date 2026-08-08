# Универсальный benchmark чтения и восстановления чертежей

**Статус:** выполняется.  
**Цель:** доказуемо восстанавливать редактируемую геометрию из машиностроительных и строительных чертежей, строить проверяемую 3D/BIM-модель и выпускать необходимые 2D-виды без ложных заявлений точности.

> Подробный последовательный список оставшихся работ, критериев и evidence находится в [`CAD_UNIVERSAL_REMAINING_TODO.md`](./CAD_UNIVERSAL_REMAINING_TODO.md).

Обозначения: `[x]` завершено и проверено, `[-]` выполняется/частично, `[ ]` не начато.

## 1. Граница задачи

- [x] Отказаться от benchmark только для тел вращения.
- [x] Разделить конвейер на два домена с общим ingestion/evidence/audit слоем:
  - `mechanical` — деталь, сборочная единица и машиностроительный чертёж;
  - `construction` — здание, конструкция, сеть и комплект строительных чертежей.
- [x] Не смешивать mechanical feature tree и BIM-граф в одну ослабленную схему.
- [x] Общими оставить: определение листов/видов, OCR, геометрические примитивы, provenance, координаты evidence, версии правок, статусы `blocked/review_required/verified` и журнал процесса.
- [-] Добавить domain router с измеряемой уверенностью и явным отказом при смешанном или неизвестном документе. Источники уже получают строгие `domain/drawing_class`; runtime routing ещё предстоит расширить.

## 2. Таксономия машиностроения

Каждый класс должен иметь реальные holdout-примеры и эталонный STEP/B-Rep либо независимо проверенную параметрическую спецификацию.

- [ ] Тела вращения: валы, оси, втулки, ступицы, ролики, шпиндели, штуцеры.
- [ ] Призматические детали: плиты, корпуса, кронштейны, траверсы, крышки.
- [ ] Фланцы и детали с круговыми массивами.
- [ ] Листовые детали: гибы, отбортовки, вырезы, развёртки и линии сгиба.
- [ ] Литые и кованые детали: уклоны, бобышки, рёбра, переходы и радиусы.
- [ ] Сварные конструкции: профили, швы, разделка, сборочные базы.
- [ ] Зубчатые колёса, шлицы, звёздочки, кулачки и пружины — отдельный advanced-класс, не аппроксимируемый гладкими телами.
- [ ] Сборочные чертежи: позиции, состав, сопряжения и деталировка без слияния компонентов в одно тело.
- [ ] Стандартные изделия: резьбовые соединения, подшипники, шпонки, стопорные элементы.
- [ ] Виды: основные, дополнительные, местные, разрезы, сечения, обрывы и выносные элементы.
- [-] PMI: из официальных NIST CTC/FTC workbook построено `100` semantic records по `11` деталям, сохранены точное написание и `98/100` page-membership кандидатов. Все приложенные STEP прямо помечены `geometry only`, поэтому topology association и точный drawing bbox честно остаются unresolved, а promotion закрыт.

### Mechanical ground truth

- `geometry_truth`: STEP/B-Rep и топологическая подпись.
- `feature_truth`: операции, параметры, привязки к граням/кромкам и массивы.
- `drawing_truth`: виды, направления, разрезы, видимость линий и размеры.
- `semantic_truth`: PMI, позиции, связи и точное исходное написание.

### Mechanical promotion gate

- [ ] Валидное число solid/shell и замкнутый manifold B-Rep.
- [ ] Габариты, площадь, объём и surface-distance к эталону в заданном допуске.
- [ ] Полнота/точность feature-операций без выдуманных элементов.
- [ ] Полнота необходимых видов и разрезов.
- [ ] Размеры относятся к правильным объектам и не противоречат модели.
- [ ] Сборка сохраняет отдельные компоненты и сопряжения.

## 3. Таксономия строительства

Каждый BIM-кейс должен иметь исходный IFC и детерминированно полученные листы; реальные PDF/DWG используются как отдельный domain holdout, если для них доступна полная семантическая истина.

- [ ] Архитектура: поэтажные планы, фасады, разрезы, кровли, помещения, проёмы, лестницы и отделка.
- [ ] Конструкции: оси, фундаменты, колонны, балки, стены, плиты, фермы, армирование и узлы.
- [ ] Генплан: здания, дороги, площадки, отметки и координационные оси.
- [ ] ОВ/ВК: оборудование, воздуховоды, трубопроводы, фитинги, стояки, уклоны и подключения.
- [ ] Электрика/СС: устройства, трассы, цепи, щиты, кабели и условные обозначения.
- [ ] Планы потолков, полов, кладочные планы и спецификации — отдельные типы представления.
- [ ] Многолистовые комплекты: марки, ссылки на узлы, разрезы и продолжения сетей между листами.
- [ ] Реконструкция: существующее/демонтируемое/новое как разные фазы, а не один контур.

### Construction ground truth

- `bim_truth`: IFC entities, GUID, classes, placement, shape, storey and system relations.
- `spatial_truth`: site/building/storey/space containment and openings.
- `connectivity_truth`: MEP ports, systems and graph connectivity.
- `drawing_truth`: plans, elevations, sections, details, grids and annotations.
- `quantity_truth`: counts, lengths, areas and volumes derived independently from IFC.

### Construction promotion gate

- [x] IFC проходит schema/STEP validation и повторно открывается: IfcOpenShell 0.8.5 прочитал `23/23` официальных файлов IFC4/IFC4X3, GUID-дубликатов и parse failures нет.
- [ ] Совпадают классы и количество основных BIM-объектов без подмены generic proxy.
- [ ] Совпадают этажи, помещения, проёмы и containment-связи.
- [ ] Сохраняется связность инженерных систем.
- [ ] Геометрия проверяется по placement, bbox, площади/объёму и проекциям.
- [ ] Все обязательные планы, фасады, разрезы и узлы присутствуют и ссылаются на одну ревизию BIM.

## 4. Источники и разделение данных

- [x] `nist_mbe_pmi`: экспертно проверенные mechanical 2D/PMI + CAD/STEP, только holdout.
- [x] `freecad_parts_library`: лицензированные STEP/FcStd-модели для mechanical train/dev; разделение по семейству детали.
- [x] `qcad_open_library`: только DXF с разрешённой лицензией в sidecar RDF.
- [x] `engineering_drawings_as1100`: raster-domain holdout; группировка по базовой детали, а не по варианту дефекта.
- [x] `buildingsmart_sample_test_files`: официальный IFC seed; разделение по исходной сцене/GUID-набору.
- [x] Лицензированный TriView-CAD зарегистрирован как `approved_staged` только для вспомогательной проверки согласованности 2D-видов; отсутствие B-Rep явно запрещает считать его 3D ground truth.
- [ ] Подобрать реальные открытые architectural/structural/MEP комплекты с IFC + PDF/DWG и зафиксированной лицензией.
- [ ] Исключать источники без ясных прав, provenance или проверяемого ground truth.
- [ ] Хешировать source group до любых raster/degradation вариантов; один объект/здание не может попасть в разные split.
- [ ] Навсегда закрыть final holdout от обучения, prompt-примеров и ручной подгонки.

## 5. Матрица качества

Метрики публикуются отдельно по домену, классу и сложности; один средний score запрещён.

| Слой | Mechanical | Construction |
|---|---|---|
| Чтение | параметры, PMI, связи видов | объекты, уровни, оси, марки, связи листов |
| 3D | B-Rep topology/features/surface distance | IFC classes/relations/geometry/connectivity |
| 2D | виды, разрезы, линии, размеры | планы, фасады, разрезы, узлы, аннотации |
| Ложная геометрия | invented feature rate | invented object/relation rate |
| Пропуски | missing feature rate | missing object/system rate |
| Выпуск | verified part/assembly | validated BIM/drawing set |

## 6. Последовательность реализации

- [x] Инвентаризировать источники и построить manifest v2 с доменом, классом, лицензией, truth layers и source group. Первый reproducible snapshot: `239` assets в `230` логических source groups (`127 mechanical`, `102 construction`, `10 mixed`), включая `181 STEP`, `35 DXF`, `23 IFC`; IFC4/IFC4X3 варианты одной сцены принудительно находятся в одном split, validator подтвердил отсутствие leakage.
- [x] Добавить production IFC parser/evaluator: `23/23` файлов разобраны, `11445` IFC entities, `984` products, `717` represented products, `40` storeys, `4` spaces, `55` distribution elements.
- [x] Спроецировать STEP ground truth в ортогональные листы: `176/181` моделей, `518` листов из `175` source groups, `71153` семантических сущности; 5 импортов и 10 непригодных видов отклонены.
- [x] Снять честный исходный CV baseline на 45 holdout-листах: precision `0,190324`, recall `0,094941`, entity F1 `0,126686`, exact sheet `0%`, false exact `100%`. Этот результат запрещает promotion текущего распознавателя.
- [x] Добавить разрез baseline по доменам и классам: `mechanical F1=0,115228`, `construction components F1=0,212648`; худший измеренный класс — bearings `F1=0,038371`.
- [x] Построить IFC semantic projections с сохранением GUID и IFC-класса каждого ребра: `23/23` сцен, `717` геометрических products, geometry failures `0`; получены plan/front/side ground-truth observations. Треугольные рёбра пока не являются готовым чертёжным представлением и должны пройти hidden-line/silhouette reduction до raster benchmark.
- [x] Удалить coplanar mesh diagonals и оставить boundary/feature/silhouette edges: объём IFC observations снижен с `420565` до `142699` сегментов без geometry failures.
- [x] Сформировать source-grouped IFC raster corpus по пространственным контейнерам: одна каноническая версия на логическую сцену, `177` листов, `40286` сущностей, split `153/12/12`; все листы одного здания остаются в одном split.
- [x] Снять construction baseline: holdout `12` листов, precision `0,230461`, recall `0,082378`, F1 `0,121372`, exact sheet `25%`, false exact `75%`; promotion запрещён низкой полнотой и ложным exact.
- [x] Реализовать и проверить строительное представление: triangle depth-buffer HLR по общей IFC-сцене, три независимых прогона видимости на ребро, сечение каждого этажа на `+1,2 м`, объединение коллинеарных граней сечения и семантические стили IFC-классов. Граничный ray-cast был отклонён живым прогоном (`0` hidden) и заменён пакетной проверкой глубины. Итог: `23/23` IFC, geometry failures `0`, `74802` visible, `79741` hidden, `188` секущих сегментов; обычная проекция сверху не принимается за план этажа при наличии `plan_section`.
- [x] Пересобрать construction corpus и baseline после HLR: `169` листов, `23516` сущностей, split `147/12/10`; holdout precision `0,419580`, recall `0,058309`, F1 `0,102389`, false exact `70%`. Promotion по-прежнему запрещён.
- [ ] Сформировать минимальный balanced baseline по каждому классу.
- [x] Добавить mechanical B-Rep evaluator и construction IFC evaluator. Mechanical pair evaluator проверяет B-Rep validity, topology, bounding box, volume/area, exact boolean volume IoU и двунаправленное surface sampling; production segfault в `Part.makeCompound` устранён. Добавлен перебор 24 proper axis orientations с topology-preserving `transformShape`: живой повёрнутый STEP восстановлен с bbox error `0`, IoU `1,0` и совпавшей topology. Construction pair evaluator проверяет IFC validity, multiset классов, этажи/пространства, containment, geometry failures, axis-invariant bbox и агрегированный объём; self-pair прошёл, чужая модель отклонена четырьмя независимыми gates.
- [x] Добавить NIST PMI truth builder и evaluator: официальные workbook дали `100` записей (`50 CTC`, `50 FTC`), semantic self-pair F1 `1,0`; evaluator отдельно считает missing/invented/false-exact и fail-closed блокирует promotion из-за отсутствующих topology/exact-region associations.
- [x] Снять PMI baseline production reader: truth-linked production-прогон завершён на `27` листах и `98` официальных записях без runtime errors. Строгий результат: `440` кандидатов, `7` точных, precision `0,015909`, recall `0,071429`, F1 `0,026022`, missing `92,86%`, invented `98,41%`, false-exact `0%`; promotion запрещён. Все `7` совпадений пришли из annotation-канала; dimension-канал дал `0/228`, что зафиксировано как следующий общий класс ошибки.
- [ ] Прогнать текущий live stack без подстройки и сохранить исходный baseline.
- [ ] Дорабатывать reader/schema/generator/editor по классам ошибок, а не по именам файлов.
- [ ] После каждой итерации прогонять dev; final holdout запускать только на promotion-кандидате.
- [ ] Развернуть кандидата в production и дать пользователю набор живых URL с исходником, журналом, 3D/BIM и 2D-результатом.

## 7. Definition of Done

- [ ] Есть лицензированный, source-grouped и воспроизводимый корпус обоих доменов.
- [ ] У каждого promotion-кейса есть достаточная семантическая истина, а не только похожая картинка.
- [ ] Mechanical и construction имеют независимые схемы, валидаторы и release gates.
- [ ] Редактор показывает прочитанные сущности, связи, доказательства и точный payload генератора.
- [ ] Неподдерживаемый класс или неполный источник завершается честным `blocked/review_required`.
- [ ] Для поддерживаемых классов рабочий стек создаёт проверяемую 3D/BIM-модель и достаточный комплект 2D-видов.
- [ ] Отчёт до/после содержит результаты по каждому классу, false-positive/false-exact rate и ссылки на воспроизводимые артефакты.
