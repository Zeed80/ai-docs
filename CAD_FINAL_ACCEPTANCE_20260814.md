# Финальная CAD/BIM-приёмка — 2026-08-14

## Вердикт

Фаза 11 выполнена, но frozen candidate `dbb7a9a7fd4018337f444f71dbe6da48c058d4f7`
**не допускается к exact-promotion**. Dev promotion smoke из фазы 10 прошёл, однако
оба независимых sealed holdout не достигли entity-level порогов. Production
обязан сохранять текущий fail-closed/review-gated режим; результаты нельзя
трактовать как универсально точную оцифровку.

Машиночитаемый receipt:
`tools/cad-dataset/baselines/cad_final_acceptance_20260814.json`.

## Зафиксированный кандидат

- commit: `dbb7a9a7fd4018337f444f71dbe6da48c058d4f7`;
- tree: `625e347937f9d3acecac11ed020e9b4954ba0943`;
- dev manifest, candidate baseline, source registry, source manifest и оба
  holdout manifests зафиксированы SHA-256 в freeze report;
- evaluator: deterministic `cv`, entity tolerance `0.0025`;
- promotion gates: precision/recall `>= 0.995`, exact-sheet `>= 0.99`,
  false-exact `= 0`.

До чтения aggregate holdout-метрик выполнен leakage-аудит. Он подтвердил:

- mechanical: 30 листов, 11 source groups;
- construction: 24 листа, 2 source groups;
- отсутствуют пересечения holdout groups с train/val и точные упоминания этих
  groups в `backend/app/ai`, `aiagent` и dev promotion fixture;
- источники зарегистрированы и имеют разрешающие лицензии.

Аудит выявил и до sealed-прогона устранил общий дефект corpus builder: IFC
drawing split пересчитывался независимо от канонического source manifest.
Теперь downstream-листы наследуют source split, а несовместимые варианты одной
source group отклоняются.

## Однократный sealed holdout

| Домен | Листы | Source groups | Precision | Recall | F1 | Exact-sheet | False-exact | Итог |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Mechanical / NIST PMI | 30 | 11 | 0.009210 | 0.000778 | 0.001434 | 0.000000 | 1.000000 | rejected |
| Construction / IFC HLR | 24 | 2 | 0.162244 | 0.039752 | 0.063858 | 0.083333 | 0.916667 | rejected |

`false_exact` здесь означает старую pixel-coverage заявку `score.ok` при
непройденной entity truth. Это специально сохранённый диагностический сигнал,
а не утверждение, что release gate пропустил результат: EMG/release-контур
остаётся approval-gated и блокирует неполный граф.

Сильнейший construction-класс (`structural`) также недостаточен: precision
`0.666667`, recall `0.092671`, exact-sheet `0.222222`. Поэтому усреднение или
выбор удобного класса не может изменить финальный отказ.

## Что принято, а что отклонено

Приняты и могут оставаться в production:

- deterministic spec/import paths с доказанными STEP/IFC/DXF reopen;
- canonical EMG, atomic corrections, diagnostics и approval gates;
- честные `blocked`, `review_required` и `not_applicable` исходы;
- class-balanced dev regression как smoke/regression gate.

Не приняты как exact-capability:

- raster/PDF → CAD для произвольных mechanical-листов;
- raster construction HLR → полный entity-equivalent CAD;
- pixel coverage как основание для promotion;
- любые заявления об универсальной точности CAD-оцифровки.

Повторный holdout для этого кандидата запрещён. Следующий кандидат должен быть
обучен и выбран только по новым train/dev evidence, получить новый commit/config
hash и проходить новый заранее объявленный sealed набор, не раскрытый в этом
цикле.

## Воспроизведение безопасной части

Freeze и leakage можно повторять до holdout только для нового кандидата:

```bash
make cad-final-freeze
make cad-final-leakage
```

Существующий sealed receipt является окончательным для `dbb7a9a`; команда
`finalize` намеренно не перезаписывает существующий файл.

## Production-проверка

- `make prod-build` выполнен после финальных изменений; backend и cad-kernel
  имеют `healthy` status;
- `https://192.168.1.246/health` возвращает `{"status":"ok"}`;
- `https://192.168.1.246/cad` через публичный Traefik route переводит на
  `/auth/login?next=%2Fcad` и после входа открывает CAD workspace;
- неавторизованный `GET /api/image-gen/{id}/model-graph/download` возвращает
  `401`;
- production multi-domain regression прошла `9/9` (mechanical, assembly,
  construction, system), но подтверждает только deterministic spec/import
  capability, а не отклонённую raster exact-capability;
- `test-results/emg_live_artifacts/<case>/manifest.json` фиксирует SHA-256
  source, graph, generator payload, 3D/BIM/diagram, 2D и validation/audit
  evidence. Все девять manifests имеют `complete=true`.

Самостоятельная проверка: открыть `https://192.168.1.246/cad`, войти, создать
детерминированный поддерживаемый spec/import case и скачать diagnostics package.
Raster/PDF результат с `review_required` нельзя принимать как exact; это
ожидаемое поведение после отрицательного sealed verdict.
