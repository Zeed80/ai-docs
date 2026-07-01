#!/usr/bin/env bash
# Повторная загрузка тестового набора реальных чертежей (см. SOURCES.md).
set -euo pipefail
cd "$(dirname "$0")"

UA="Mozilla/5.0 (research; internal QA test set)"

declare -A FILES=(
  ["part_01_shaft_wikimedia.jpg"]="https://upload.wikimedia.org/wikipedia/commons/c/ce/Shaft_drawing.jpg"
  ["part_02_val_gost.png"]="https://cadinstructor.org/wp-content/uploads/val_cherteg_2.png"
  ["part_03_vtulka_gost.png"]="https://cadinstructor.org/wp-content/uploads/vtulka_2.png"
  ["part_04_planka_gost.png"]="https://cadinstructor.org/wp-content/uploads/planka_list_425_600.png"
  ["part_05_krishka_gost.png"]="https://cadinstructor.org/wp-content/uploads/Krishka_2_600_423.png"
  ["part_06_gear_gost.png"]="https://cadinstructor.org/wp-content/uploads/2021/01/tcil_zub_koleso.png"
  ["part_07_korpus_gost.png"]="https://cadinstructor.org/wp-content/uploads/2021/01/01_01_korpus.png"
  ["part_08_nikon_fmount_wikimedia.png"]="https://upload.wikimedia.org/wikipedia/commons/0/0e/Nikon_F-mount_mechDwg.png"
  ["asm_01_adapter_sleeve_wikimedia.png"]="https://upload.wikimedia.org/wikipedia/commons/8/88/Adapter-sleeve_DIN5415_complete_ex.png"
  ["asm_02_bicycle_headset_wikimedia.png"]="https://upload.wikimedia.org/wikipedia/commons/f/f0/Bicycle_headset_%28threadless%29_exploded_view-en.png"
  ["asm_03_sborka_gost.png"]="https://cadinstructor.org/wp-content/uploads/sb1_2.png"
  ["asm_03_spec_gost.png"]="https://cadinstructor.org/wp-content/uploads/sb2_2.png"
  ["asm_04_compas_levage_wikimedia.svg"]="https://upload.wikimedia.org/wikipedia/commons/8/86/Compas_levage_ST_lohr_industrie_BTS_CPI_E51_2011_DR1.svg"
)

for name in "${!FILES[@]}"; do
  url="${FILES[$name]}"
  echo "fetching $name ..."
  curl -sL -A "$UA" -o "$name" "$url"
done

echo "Done. Convert the SVG separately: cairosvg asm_04_compas_levage_wikimedia.svg -o asm_04_compas_levage_wikimedia.png --output-width 1200"
