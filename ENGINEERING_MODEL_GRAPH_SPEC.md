# Engineering Model Graph v1

Статус: публичный контракт `emg/1.0`. Нормативная реализация находится в
`backend/app/domain/engineering_model_graph.py`, а проверяемая JSON Schema — в
`schemas/engineering-model-graph-1.0.schema.json`.

## Назначение

`EngineeringModelGraph` (EMG) — канонический граф инженерного смысла комплекта
документов. Он содержит все связи, необходимые для чтения, проверки,
редактирования и построения модели:

- `nodes` — документы, листы, виды, области источника, изделия, компоненты,
  элементы, геометрия, параметры, ограничения, системы, порты, операции,
  артефакты и элементы топологии;
- `edges` — отношения между узлами: состав, принадлежность, представление на
  виде, соответствие между видами, зависимости, ограничения, сопряжения,
  соединения, происхождение и отображение в B-Rep/IFC-топологию;
- `assertions` — значения и ограничения с единицами, происхождением, уровнем
  подтверждения, критичностью и историей замещения;
- `evidence` — трассировка каждого утверждения до документа, точного
  `SourceRegion`, векторной сущности, вычисления, решения человека или
  результата проверки;
- `hypothesis_options`, `hypothesis_sets`, `requirements` и `build_targets` —
  альтернативы, требования и правила выбора допустимого построения.

Слово «все» относится к инженерно значимым и трассировочным связям. EMG не
дублирует каждый пиксель и не превращает близость пикселей в инженерную связь.
Сырые координатные наблюдения остаются в `EngineeringDrawingGraph`; EMG
ссылается на них через `SourceRegion`, `Geometry`, `represented_by`, assertions
и evidence. DXF, STEP, IFC, STL и PDF являются производными артефактами, а не
источником истины.

## Форматы файлов

| Содержимое | Суффикс | Media type | `schema_version` |
|---|---|---|---|
| Снимок ревизии | `.emg.json` | `application/vnd.ptsai.emg+json` | `emg/1.0` |
| Атомарное изменение | `.emg-patch.json` | `application/vnd.ptsai.emg-patch+json` | `emg-patch/1.0` |

JSON должен быть UTF-8. Неизвестные поля запрещены. Доменные расширения
допустимы только в `extension` с зарегистрированным `namespace`, `version` и
SHA-256 схемы в `extension_registry`.

## Корень EMG

Обязательны `graph_id`, `schema_version`, `revision`, `canonical_sha256` и
`profile`. Корень также содержит `parent_revision`, `sources`, `nodes`, `edges`,
`assertions`, `evidence`, гипотезы, требования, цели построения,
`verification`, `reader_manifest` и реестр расширений. `profile` принимает
`mechanical`, `assembly`, `construction`, `mep`, `electrical`, `hydraulic`,
`pid` или `mixed`.

Идентификаторы стабильны внутри истории графа. Все ссылки проверяются:
висячие edge, assertion, evidence, hypothesis, requirement и build-target
ссылки делают файл недействительным.

## Узлы и связи

Закрытый набор типов узлов: `DocumentSet`, `Document`, `Sheet`, `View`,
`SourceRegion`, `Product`, `Component`, `Feature`, `Geometry`, `Material`,
`Parameter`, `Constraint`, `System`, `Port`, `BuildOperation`, `Artifact`,
`TopologyElement`.

Закрытый набор связей: `contains`, `part_of`, `instance_of`, `located_in`,
`represented_by`, `same_object_across_views`, `defines`, `depends_on`,
`constrains`, `applies_to`, `mates_with`, `connects_to`, `opens_in`,
`generated_by`, `maps_to_topology`.

## Assertions

`value` — discriminated union по `kind`: точное `exact`, интервал `interval`,
набор `enum_set`, выражение `expression` или `unknown`. Assertion хранит
`unit`, систему координат, evidence, confidence, impacts, hypothesis и ссылку
на замещаемое утверждение.

`origin`: `observed`, `traced`, `derived`, `standard`, `assumed`, `human`.
`assurance`: `proposed`, `observed`, `corroborated`,
`constraint_validated`, `human_approved`, `contradicted`. Reader, tracer и
visual verifier не вправе назначать `constraint_validated` или
`human_approved`. Визуальное совпадение traced-геометрии само по себе не делает
её точной или сертифицированной.

Критичность рассчитывается для конкретного `build_target` по impacts и графу
зависимостей. Тип элемента сам по себе не означает некритичность.

## Ревизии, hash и GraphPatch

Ревизии неизменяемы. Для вычисления `canonical_sha256` модель нормализуется со
всеми default-полями, `canonical_sha256` заменяется пустой строкой, JSON
сериализуется с сортировкой ключей и разделителями `,` и `:` без ASCII-escape,
после чего берётся SHA-256 от UTF-8 байтов.

`GraphPatch` указывает `base_revision` и `base_sha256`, producer, pass,
idempotency key, добавления, supersede/retract и нерешённые противоречия.
Patch к устаревшей базе или с повторным idempotency key отклоняется целиком и
журналируется; подтверждённые данные нельзя молча потерять.

## Производные представления и выпуск

`EngineeringDrawingGraph` — координатные наблюдения. Старый
`EngineeringDrawingSpec` — только compatibility-view. FeatureTree, CadIR и
kernel payload детерминированно компилируются из выбранной ревизии EMG.
Provisional-артефакт может содержать явно маркированные assumed/traced
элементы. Production export и certification блокируются, пока остаются
неподтверждённые build-critical assertions.

## Проверка и примеры

```bash
make emg-schema-check
make emg-validate
```

Обновление схем после изменения Pydantic-контракта: `make emg-schema`.
Примеры находятся в `examples/emg/`. В Studio последняя владельчески доступная
ревизия скачивается через
`GET /api/image-gen/{generation_id}/model-graph/download`; semantic hash и номер
ревизии также возвращаются в заголовках
`X-Engineering-Graph-SHA256` и `X-Engineering-Graph-Revision`.
