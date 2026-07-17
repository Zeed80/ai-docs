# B7: fine-tune технического векторизатора (Deep Vectorization line model)

Инфраструктура дообучения line-модели (Egiazarian et al., ECCV 2020) на
собственном корпусе. Полный цикл воспроизводим за минуты:

```bash
# 1. Датасет: DWG/DXF/CAD-IR → деградированные патчи 64×64 + line-таргеты
#    (формат PreprocessedDataset; каналы/нормировка = serve._preprocess_patches)
docker cp tools/vectorizer-finetune/make_dataset.py infra-backend-1:/tmp/
docker cp cleanup_test_files infra-backend-1:/app/cleanup_test_files
docker exec infra-backend-1 python /tmp/make_dataset.py \
    --dwg-dir /app/cleanup_test_files --out /tmp/vf-ours --variants 4

# 2. Fine-tune ОТ прод-чекпоинта (в контейнере векторизатора: torch 1.7.1
#    совпадает с чекпоинтом; CUDA там недоступна для RTX 3090 (sm_86 > cu110),
#    но модель компактная — 3 эпохи на CPU ≈ 3 минуты)
docker exec infra-technical-vectorizer-1 python /tmp/finetune.py \
    --data /tmp/vf-ours --out /tmp/model_lines_ft.weights --epochs 3

# 3. ГЕЙТ (обязателен): кандидат-сервис + eval на реальном корпусе;
#    принимать ТОЛЬКО при улучшении против baseline-neural
docker run -d --name vectorizer-candidate --network infra_app \
    -v <dir-с-кандидатом>:/models:ro \
    -e TECHNICAL_VECTORIZER_CHECKPOINT=/models/model_lines_ft.weights \
    infra-technical-vectorizer:latest
docker exec -e TECHNICAL_VECTORIZER_URL=http://vectorizer-candidate:8091 \
    infra-backend-1 sh -c 'cd /app && python scripts/eval_vectorize.py \
    --dir cleanup_test_files --recognizer neural --out /tmp/eval_candidate.json'
```

## Результат эксперимента 2026-07-17 — КАНДИДАТ НЕ ПРИНЯТ

Обучение: 8684 патча из 10 DWG (×4 деградированных варианта), 3 эпохи,
lr 1e-5; val-loss на СВОЕЙ синтетике 6.54 → 0.76. Но гейт на реальном
корпусе (19 фото + 10 DWG-рендеров):

| метрика (photos)      | baseline | кандидат |
|-----------------------|----------|----------|
| mean_recall           | 0.863    | 0.836 ↓  |
| mean_precision        | 0.933    | 0.915 ↓  |
| coverage_ok_rate      | 0.737    | 0.526 ↓  |
| mean_fragmentation    | 1.63     | 2.16 ↑   |
| mean_duplicate_rate   | 0.034    | 0.158 ↑  |

Классическое катастрофическое забывание: узкий корпус (10 листов, в
основном архитектурные фасады/планы) + суррогатный matched-loss смещают
модель от оригинального обучающего распределения. Прод-чекпоинт оставлен.

## Что пробовать дальше (в порядке ожидаемой отдачи)
1. **Реальные пары из принятых правок (B5)**: вход — реальный бинаризованный
   ink С ФОТО (не синтетическая деградация), таргет — принятая человеком
   геометрия ревизии. Это устраняет главный разрыв домена.
2. **Replay против забывания**: смешивать свои патчи с патчами из
   оригинального распределения (SESYD/synthetic-handcrafted) 1:3.
3. Меньше шагов/ниже LR (1e-6), ранняя остановка ПО ГЕЙТУ (а не по
   val-loss на своей же синтетике — он доказанно не коррелирует).
4. Расширить вариативность деградации (толщина, контраст, текстуры бумаги).
