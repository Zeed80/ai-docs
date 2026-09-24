# Тестовый набор реальных чертежей — источники и лицензии

Набор для проверки/доработки создания и редактирования в графической студии
(diffusion-путь: generate/edit/cleanup через ComfyUI). Только для внутреннего
QA, не для публикации/редистрибуции. Файлы НЕ коммитятся в git (см.
`.gitignore`) — используйте `fetch.sh` для повторной загрузки.

## Детали (parts)

| Файл | Источник | Лицензия | Сложность |
|---|---|---|---|
| part_01_shaft_wikimedia.jpg | [Wikimedia](https://commons.wikimedia.org/wiki/File:Shaft_drawing.jpg) | CC BY-SA 4.0 | простая (вал, 1 вид) |
| part_02_val_gost.png | [cadinstructor.org](https://cadinstructor.org/eg/lectures/9-chertegi-detaley-sborochniy-cherteg/) | учебный пример (открытая публикация) | простая (вал, ЕСКД, 2 сечения) |
| part_03_vtulka_gost.png | cadinstructor.org | учебный пример | простая (втулка) |
| part_04_planka_gost.png | cadinstructor.org | учебный пример | простая (плоская деталь) |
| part_05_krishka_gost.png | cadinstructor.org | учебный пример | средняя (литая крышка, обработка) |
| part_06_gear_gost.png | cadinstructor.org | учебный пример | средняя (зубчатое колесо + таблица параметров) |
| part_07_korpus_gost.png | cadinstructor.org | учебный пример | высокая (корпус, много видов/сечений) |
| part_08_nikon_fmount_wikimedia.png | [Wikimedia](https://commons.wikimedia.org/wiki/File:Nikon_F-mount_mechDwg.png) | CC BY 3.0 | экстремальная (плотная разноцветная простановка размеров) |

## Сборки (assemblies)

| Файл | Источник | Лицензия | Сложность |
|---|---|---|---|
| asm_01_adapter_sleeve_wikimedia.png | [Wikimedia](https://commons.wikimedia.org/wiki/File:Adapter-sleeve_DIN5415_complete_ex.png) | CC BY-SA 2.5 | простая (3 детали, 3D-рендер, без текста размеров) |
| asm_02_bicycle_headset_wikimedia.png | [Wikimedia](https://commons.wikimedia.org/wiki/File:Bicycle_headset_(threadless)_exploded_view-en.png) | CC BY-SA 3.0 / GFDL | средняя (exploded view, подписи-выноски) |
| asm_03_sborka_gost.png + asm_03_spec_gost.png | cadinstructor.org | учебный пример | высокая (сборочный чертёж «Лубрикатор», 4 вида + спецификация 19 позиций) |
| asm_04_compas_levage_wikimedia.svg(+png) | [Wikimedia](https://commons.wikimedia.org/wiki/File:Compas_levage_ST_lohr_industrie_BTS_CPI_E51_2011_DR1.svg) | public domain (франц. гос. экзамен) | средняя (механизм, схема) |

## Повторная загрузка

```bash
bash fetch.sh
```

## Схемы (systems) — первая проверка `system_reader` на реальных листах (2026-09-25)

| Файл | Источник | Лицензия | Что на листе |
|---|---|---|---|
| sys_01_pump_tank_pid_wikimedia.svg(+png) | [Wikimedia](https://commons.wikimedia.org/wiki/File:Pump_with_tank_pid_en.svg) | см. страницу файла | P&ID: бак T001 (N1–N6, M1), насос P001 с двигателем, предохранительный клапан, L0011/L0012/PI0013, арматура, переходы 100/50 |
| sys_02_ri_sample_iso_wikimedia.png | [Wikimedia](https://commons.wikimedia.org/wiki/File:RI_Sample_ISO.png) | см. страницу файла | P&ID ISO: бак B-0001, теплообменник W-0001, насосы P-1001/P-1002, регулирующие клапаны, PSV |
| sys_03_pid_plant_wikimedia.jpg | [Wikimedia](https://commons.wikimedia.org/wiki/File:P%26ID.JPG) | см. страницу файла | P&ID установки (итал. подписи), JPEG |
