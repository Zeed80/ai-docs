# Датасет для cleanup-LoRA (Qwen-Image-Edit-2511)

Конвейер генерации обучающих пар для LoRA, усиливающей режим «Очистить
чертёж» графической студии: (грязное фото чертежа → чистый ЕСКД-чертёж).

## Ключевые проектные решения

- **Пары строятся в обратную сторону**: не «чистим плохое фото» (это и есть
  нерешённая задача), а «портим идеальный рендер». Target и control — один и
  тот же чертёж по построению, геометрическая согласованность гарантирована.
- **Control проходит продакшен-препроцессинг**: на инференсе diffusion
  никогда не видит сырое фото — его сначала обрабатывает
  `drawing_cleanup.enhance_source_for_diffusion` (dewarp перспективы, deskew,
  CLAHE, стирание переплёта). Поэтому control = `enhance(simulate_photo(target))`.
  Трейн и инференс совпадают по построению; остаточные дефекты (тени, шум,
  недостёртый переплёт, лёгкий наклон) — ровно то, что LoRA должна убирать.
- **Синтетика не конфиденциальна**: пары из `synth_techdraw.py` можно
  тренировать на арендованном облачном GPU. Пары из реальных DWG предприятия —
  только локально (правило Dual AI).
- **Капшены**: инструкция + краткое описание содержимого от VLM. Живое
  сравнение (2026-07-02): `qwen3.6:35b` — лучший локальный капшенер (читает
  надписи с листа), но требует даунскейла входа до 800px (vision-энкодер
  OOM'ит 24GB на больших изображениях); фолбэк — `gemma4:31b`.
  При внедрении LoRA в прод cleanup-воркфлоу должен начать подклеивать такое
  же описание к инструкции (симметрия трейна и инференса).

## Шаги

```bash
SP=/path/to/workdir

# 1. Эталоны из реальных DWG (требуется dwg2dxf из LibreDWG — собирается из
#    исходников: configure --disable-bindings && make CFLAGS="-O2 -Wno-error")
python3 render_dwg.py --src cleanup_test_files --out $SP/targets

# 2. Синтетические эталоны (неограниченно, не конфиденциально)
python3 synth_techdraw.py --count 500 --out $SP/targets_synth

# 3. Деградация → control-изображения (импортирует backend, запускать при
#    установленных зависимостях backend; --raw отключает прод-препроцессинг)
python3 degrade.py --src $SP/targets --out $SP/controls --per-image 3
python3 degrade.py --src $SP/targets_synth --out $SP/controls_synth --per-image 2

# 4. Капшены (локальный Ollama; qwen3.6:35b@800px, фолбэк gemma4:31b)
python3 caption.py --src $SP/targets --ollama http://localhost:11434
python3 caption.py --src $SP/targets_synth --ollama http://localhost:11434

# 5. Сборка в формат ai-toolkit (QA: ink fraction, аспект, пустые контролы)
python3 build_dataset.py --targets $SP/targets --controls $SP/controls \
    --out datasets/lora-cleanup
python3 build_dataset.py --targets $SP/targets_synth --controls $SP/controls_synth \
    --out datasets/lora-cleanup
```

Выход: `datasets/lora-cleanup/{images,control}/*.png` + `images/*.txt`
(промпт = инструкция cleanup + описание содержимого).

## Обучение (ai-toolkit, RTX 3090 24GB)

`train_config.example.yaml` рядом — стартовая точка. Перед запуском свериться
со свежим `config/examples/` в ai-toolkit (точный `arch` для 2511 и низко-VRAM
флаги меняются между версиями). На 24GB обязательны: квантование модели и
текст-энкодера, gradient checkpointing, кэш эмбеддингов, разрешение 768.
GPU на время обучения освободить от Ollama/vLLM (`ollama stop`, остановить
llamacpp/vllm контейнеры).

Валидация: `cleanup_test_files/` — 19 реальных фото + 10 DWG тех же объектов
(золотой сет, В ТРЕЙН НЕ КЛАСТЬ; рендеры DWG в трейне выше — допустимый
компромисс при малом наборе, но фото должны остаться чистым тестом).

## Ограничения

- LoRA улучшает стилевую точность и снижает галлюцинации геометрии, но НЕ
  заменяет пост-обработку: текст по-прежнему вставляет `text_preserve.py`,
  математическую прямизну линий гарантирует `drawing_vectorize.py`.
- `render_dwg.py` чинит два артефакта конвертации DWG (анонимные блоки,
  MTEXT-шрифты) — на других DWG-архивах возможны новые; смотреть на рендеры
  глазами перед масштабированием.
