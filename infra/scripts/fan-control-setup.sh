#!/usr/bin/env bash
# Motherboard fan control: diagnose the host, and install the driver when the
# host is one of the cases this script knows how to handle.
#
# Usage:
#   bash infra/scripts/fan-control-setup.sh            # diagnose only (default)
#   bash infra/scripts/fan-control-setup.sh --install  # install the DKMS driver
#   bash infra/scripts/fan-control-setup.sh --verify   # check the result after reboot
#   bash infra/scripts/fan-control-setup.sh --uninstall
#
# Flags: --yes (skip the confirmation prompt), --force (install despite an
# unrecognised chip — you are on your own), --help.
#
# THE DEFAULT MODE CHANGES NOTHING. Fan hardware differs wildly between boards:
# what is needed here (an out-of-tree module for a Nuvoton NCT6687D on an MSI
# board) may be wrong, unnecessary, or harmful on yours. Диагностика сначала,
# установка только после того, как вы прочли её вывод.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DRIVER_REPO="https://github.com/Fred78290/nct6687d"
BLACKLIST_FILE="/etc/modprobe.d/blacklist-nct6683.conf"
AUTOLOAD_FILE="/etc/modules-load.d/nct6687.conf"
OPTIONS_FILE="/etc/modprobe.d/nct6687.conf"
STAMP="$(date +%Y%m%d-%H%M%S)"
# Overridable so the classification can be exercised against fixture trees for
# boards this machine does not have.
HWMON_ROOT="${HWMON_ROOT:-/sys/class/hwmon}"

MODE="check"
ASSUME_YES=0
FORCE=0

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }

usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --install)   MODE="install" ;;
    --uninstall) MODE="uninstall" ;;
    --verify)    MODE="verify" ;;
    --check)     MODE="check" ;;
    --yes|-y)    ASSUME_YES=1 ;;
    --force)     FORCE=1 ;;
    --help|-h)   usage ;;
    *) err "неизвестный аргумент: $1"; exit 2 ;;
  esac
  shift
done

# --- helpers ---------------------------------------------------------------

# Whether the DRIVER published this attribute as writable.
#
# `[ -w file ]` is useless here: this script is normally run under sudo, and
# root bypasses the permission bits, so the test answers "yes" even for a
# read-only sysfs attribute. hwmon drivers encode their intent in the mode
# itself — 0644 for a pwm they will drive, 0444 for one they refuse to.
sysfs_writable() {
  local path="$1" mode
  [ -e "$path" ] || return 1
  mode="$(stat -c '%a' "$path" 2>/dev/null || echo 000)"
  (( (8#$mode & 8#200) != 0 ))
}

# Channel indices actually present on a chip. Numbering is the driver's, and it
# is not contiguous — an NCT6687 reports fan1-4 alongside fan13-16 — so the list
# is read from the filenames instead of guessed with a range.
chip_channels() {
  local base="$1"
  ls "$base" 2>/dev/null \
    | sed -n 's/^\(fan\|pwm\)\([0-9]\+\).*/\2/p' \
    | sort -n -u
}

# `cmd | grep -q` is a trap under `set -o pipefail`: grep exits at the first
# match, the writer gets SIGPIPE, and the pipeline reports failure even though
# the pattern matched. These read the whole input instead.
module_loaded() {
  lsmod | grep '^nct6687' > /dev/null
}

dkms_registered() {
  dkms status 2>/dev/null | grep '^nct6687' > /dev/null
}

confirm() {
  [ "$ASSUME_YES" = "1" ] && return 0
  local answer
  printf '%s [y/N] ' "$1"
  read -r answer </dev/tty || answer=""
  [[ "$answer" =~ ^[Yy]$ ]]
}

require_root() {
  [ "$(id -u)" = "0" ] && return 0
  err "нужны права root: запустите через sudo"
  exit 1
}

backup_file() {
  local f="$1"
  [ -e "$f" ] || return 0
  cp -a "$f" "${f}.bak-${STAMP}"
  echo "  сохранена копия ${f}.bak-${STAMP}"
}

# --- diagnosis -------------------------------------------------------------

CHIPS=()          # driver names that expose fans or pwm
WRITABLE_PWM=0
READONLY_PWM=0

scan_hwmon() {
  CHIPS=(); WRITABLE_PWM=0; READONLY_PWM=0
  local base name has_fan has_pwm i
  for base in "$HWMON_ROOT"/hwmon*; do
    [ -e "$base/name" ] || continue
    name="$(cat "$base/name" 2>/dev/null || echo '?')"
    has_fan=0; has_pwm=0
    for i in $(chip_channels "$base"); do
      [ -e "$base/fan${i}_input" ] && has_fan=1
      if [ -e "$base/pwm${i}" ]; then
        has_pwm=1
        if sysfs_writable "$base/pwm${i}" && [ -e "$base/pwm${i}_enable" ]; then
          WRITABLE_PWM=$((WRITABLE_PWM + 1))
        else
          READONLY_PWM=$((READONLY_PWM + 1))
        fi
      fi
    done
    [ "$has_fan" = "0" ] && [ "$has_pwm" = "0" ] && continue
    CHIPS+=("$name|$base|$has_fan|$has_pwm")
  done
}

print_diagnosis() {
  bold "== Железо =="
  echo "  ядро:              $(uname -r)"
  echo "  материнская плата: $(cat /sys/class/dmi/id/board_vendor 2>/dev/null || echo '?') $(cat /sys/class/dmi/id/board_name 2>/dev/null || echo '?')"
  if [ -e /dev/ipmi0 ] || [ -e /dev/ipmi/0 ]; then
    echo "  IPMI:              есть (/dev/ipmi0) — на серверных платах вентиляторами"
    echo "                     обычно распоряжается BMC через ipmitool, а не hwmon"
  fi
  echo

  bold "== Датчики и вентиляторы (hwmon) =="
  if [ ${#CHIPS[@]} -eq 0 ]; then
    warn "  Ни одного чипа с вентиляторами не найдено."
    echo "  Обычно это значит, что драйвер Super-I/O не загружен. Попробуйте"
    echo "  'sudo sensors-detect' (пакет lm-sensors) и перезагрузиться."
    echo
    return
  fi
  local entry name base has_fan has_pwm i mode enable line
  for entry in "${CHIPS[@]}"; do
    IFS='|' read -r name base has_fan has_pwm <<<"$entry"
    echo "  чип ${name}  (${base})"
    for i in $(chip_channels "$base"); do
      [ -e "$base/pwm${i}" ] || [ -e "$base/fan${i}_input" ] || continue
      line="    канал ${i}: "
      if [ -e "$base/fan${i}_input" ]; then
        line+="$(cat "$base/fan${i}_input" 2>/dev/null || echo '?') об/мин"
      else
        line+="тахометра нет"
      fi
      if [ -e "$base/pwm${i}" ]; then
        mode="$(stat -c '%a' "$base/pwm${i}")"
        enable="нет"
        [ -e "$base/pwm${i}_enable" ] && enable="есть"
        if sysfs_writable "$base/pwm${i}" && [ -e "$base/pwm${i}_enable" ]; then
          line+="   pwm: управляем (режим ${mode}, pwm_enable ${enable})"
        else
          line+="   pwm: ТОЛЬКО ЧТЕНИЕ (режим ${mode}, pwm_enable ${enable})"
        fi
      else
        line+="   pwm: канала нет"
      fi
      echo "$line"
    done
  done
  echo
}

# Prints the case name on stdout.
classify() {
  local entry name names=""
  for entry in "${CHIPS[@]}"; do
    IFS='|' read -r name _ _ _ <<<"$entry"
    names+="$name "
  done

  if [ "$WRITABLE_PWM" -gt 0 ]; then echo "already-writable"; return; fi
  if [ ${#CHIPS[@]} -eq 0 ];     then echo "no-chip"; return; fi
  case "$names" in
    *nct6683*|*nct6687*) echo "nct6687d" ;;
    *nct677*|*nct679*)   echo "nct6775" ;;
    *it87*|*it86*)       echo "it87" ;;
    *)                   echo "unknown" ;;
  esac
}

print_verdict() {
  local verdict="$1"
  bold "== Что делать =="
  case "$verdict" in
    already-writable)
      ok "  Драйвер уже отдаёт ${WRITABLE_PWM} PWM-канал(ов) на запись."
      echo "  Ставить ничего не нужно — включите управление в интерфейсе:"
      echo "  Настройки → Охлаждение, две галочки в разделе «Управление»."
      if module_loaded && ! grep -qs 'msi_fan_brute_force=1' "$OPTIONS_FILE"; then
        echo
        warn "  ВНИМАНИЕ: модуль nct6687 без параметра msi_fan_brute_force=1."
        echo "  На платах MSI 800-й серии системные вентиляторы при этом принимают"
        echo "  запись, но не подчиняются ей. Проверьте: sudo bash $0 --verify"
      fi
      ;;
    nct6687d)
      warn "  Чип Nuvoton NCT6687D под штатным драйвером nct6683: pwm только на чтение."
      echo "  Штатный драйвер разрешает запись лишь платам Mitac и не создаёт"
      echo "  pwm_enable, без которого вентилятор нечем вернуть прошивке."
      echo
      echo "  Нужен внешний DKMS-модуль nct6687d:"
      echo "      sudo bash infra/scripts/fan-control-setup.sh --install"
      echo "  Это ЕДИНСТВЕННЫЙ случай, который умеет ставить этот скрипт."
      ;;
    nct6775)
      warn "  Чип семейства NCT677x/679x. Его штатный драйвер nct6775 запись поддерживает,"
      echo "  но здесь pwm отдан только на чтение — внешний модуль ставить НЕ нужно."
      echo "  Обычная причина: ACPI держит порты чипа. Попробуйте загрузиться с"
      echo "  параметром ядра acpi_enforce_resources=lax, а в BIOS выключить"
      echo "  Smart Fan Mode. Скрипт этот случай не автоматизирует: параметры ядра —"
      echo "  слишком общая правка, чтобы делать её за вас."
      ;;
    it87)
      warn "  Чип семейства ITE IT86xx/IT87xx (частый у ASRock и Gigabyte)."
      echo "  Многие ревизии штатный драйвер не пишет; для них есть отдельный"
      echo "  внешний драйвер it87 (github.com/frankcrawford/it87)."
      echo "  Этот скрипт его не ставит: случай не проверялся."
      ;;
    no-chip)
      warn "  Чипа с вентиляторами не видно — управлять пока нечем."
      ;;
    *)
      warn "  Чип найден, но этот случай скрипту неизвестен."
      echo "  Ищите свою плату в вики lm-sensors или в списках CoolerControl."
      echo "  Цель всегда одна: получить в /sys/class/hwmon пару pwmN + pwmN_enable"
      echo "  с режимом 0644. Как только она появится, приложение подхватит канал"
      echo "  само — менять его код не нужно."
      ;;
  esac
  echo
  echo "  Вентиляторов видеокарты NVIDIA всё это не касается: они работают через"
  echo "  NVML и никаких драйверов сверх штатного не требуют."
}

# --- install ---------------------------------------------------------------

do_install() {
  require_root
  local verdict="$1"
  if [ "$verdict" != "nct6687d" ] && [ "$FORCE" != "1" ]; then
    err "Диагностика говорит «${verdict}», а скрипт умеет ставить только модуль"
    err "nct6687d для чипов NCT6683/NCT6687. Прочтите раздел «Что делать» выше."
    err "Если вы уверены — повторите с --force."
    exit 1
  fi

  bold "== Будет сделано =="
  echo "  1. проверка пакетов: dkms, build-essential, linux-headers-$(uname -r), git"
  echo "  2. сборка и установка модуля nct6687d из ${DRIVER_REPO} (через DKMS)"
  echo "  3. запись ${BLACKLIST_FILE}  — вытеснить штатный nct6683"
  echo "  4. запись ${AUTOLOAD_FILE}   — загружать nct6687 при старте"
  echo "  5. запись ${OPTIONS_FILE}   — msi_fan_brute_force=1 (см. ниже)"
  echo "  Существующие файлы сохраняются с суффиксом .bak-${STAMP}."
  echo "  Отменить всё: sudo bash $0 --uninstall"
  echo
  warn "  Это правка ядра хоста. Модуль внешний: при обновлении ядра DKMS"
  warn "  пересоберёт его сам, а если сборка упадёт — плата вернётся под"
  warn "  управление BIOS. Это безопасно, но управление пропадёт до починки."
  echo
  confirm "Продолжить?" || { echo "отменено"; exit 0; }

  bold "== 1/4 Пакеты =="
  local missing=()
  command -v dkms >/dev/null || missing+=("dkms")
  command -v git  >/dev/null || missing+=("git")
  command -v make >/dev/null || missing+=("build-essential")
  [ -d "/lib/modules/$(uname -r)/build" ] || missing+=("linux-headers-$(uname -r)")
  if [ ${#missing[@]} -gt 0 ]; then
    if command -v apt-get >/dev/null; then
      echo "  ставлю: ${missing[*]}"
      apt-get update -qq
      apt-get install -y "${missing[@]}"
    else
      err "  не хватает: ${missing[*]}"
      err "  Пакетный менеджер не apt — установите их сами и повторите."
      exit 1
    fi
  else
    ok "  всё на месте"
  fi

  bold "== 2/4 Модуль =="
  if dkms_registered; then
    ok "  модуль уже зарегистрирован в DKMS, сборку пропускаю"
  else
    local build_dir
    build_dir="$(mktemp -d)"
    trap 'rm -r -f "$build_dir"' EXIT
    echo "  клонирую ${DRIVER_REPO}"
    git clone --depth 1 "$DRIVER_REPO" "$build_dir/nct6687d"
    ( cd "$build_dir/nct6687d" && make dkms/install )
    ok "  собран и установлен"
  fi

  bold "== 3/4 Вытеснение штатного драйвера =="
  backup_file "$BLACKLIST_FILE"
  echo 'blacklist nct6683' > "$BLACKLIST_FILE"
  ok "  записан $BLACKLIST_FILE"

  bold "== 4/5 Автозагрузка =="
  backup_file "$AUTOLOAD_FILE"
  echo 'nct6687' > "$AUTOLOAD_FILE"
  ok "  записан $AUTOLOAD_FILE"

  bold "== 5/5 Параметр драйвера =="
  # Measured on an MSI MAG B850M MORTAR: without this, CPU_FAN and PUMP_FAN
  # obey while SYS_FAN1-4 silently ignore every write — the value reads back
  # as whatever the board firmware wants, and writes intermittently fail with
  # EIO. With it, all channels converge on the commanded duty.
  backup_file "$OPTIONS_FILE"
  echo 'options nct6687 msi_fan_brute_force=1' > "$OPTIONS_FILE"
  ok "  записан $OPTIONS_FILE (msi_fan_brute_force=1)"
  echo "  Без этого параметра на платах MSI 800-й серии слушаются только"
  echo "  CPU_FAN и PUMP_FAN, а системные вентиляторы молча игнорируют запись."

  echo
  bold "== Дальше — руками =="
  echo "  1. Зайдите в BIOS: ВЫКЛЮЧИТЕ Smart Fan Mode, а Fan Type Auto Detect"
  echo "     включите для всех подключённых вентиляторов. Иначе прошивка будет"
  echo "     перебивать наши записи, и приложение уведёт канал в аварию."
  echo "  2. Перезагрузитесь."
  echo "  3. Проверьте: sudo bash $0 --verify"
  echo "  4. Включите в infra/.env:  FAN_CONTROL_ENABLED=1"
  echo "                             FAN_CONTROL_ALLOW_HWMON=1"
  echo "     и перезапустите: docker compose -f infra/docker-compose.yml \\"
  echo "                        -f infra/docker-compose.prod.yml up -d gpu-temp-helper"
}

do_uninstall() {
  require_root
  bold "== Откат =="
  if dkms_registered; then
    local ver
    ver="$(dkms status | sed -n 's/^nct6687[,/ ]\+\([^,: ]*\).*/\1/p' | head -1)"
    echo "  удаляю модуль из DKMS (версия ${ver:-?})"
    dkms remove -m nct6687 -v "${ver}" --all \
      || warn "  dkms remove вернул ошибку — проверьте 'dkms status'"
  else
    echo "  модуля в DKMS нет"
  fi
  local f
  for f in "$BLACKLIST_FILE" "$AUTOLOAD_FILE" "$OPTIONS_FILE"; do
    if [ -e "$f" ]; then
      backup_file "$f"
      rm -f "$f"
      echo "  удалён $f"
    fi
  done
  modprobe -r nct6687 2>/dev/null || true
  ok "  готово. После перезагрузки вернётся штатный nct6683."
  echo "  Не забудьте вернуть FAN_CONTROL_ALLOW_HWMON=0 в infra/.env,"
  echo "  а в BIOS — Smart Fan Mode, если выключали."
}

do_verify() {
  scan_hwmon
  print_diagnosis
  bold "== Итог проверки =="
  if module_loaded; then
    ok "  модуль nct6687 загружен"
  else
    err "  модуль nct6687 НЕ загружен"
    echo "  Смотрите: dkms status ; modprobe nct6687 ; dmesg | grep -i nct"
  fi
  if grep -qs 'msi_fan_brute_force=1' "$OPTIONS_FILE"; then
    ok "  параметр msi_fan_brute_force=1 задан"
  else
    warn "  параметр msi_fan_brute_force=1 НЕ задан"
    echo "  На платах MSI 800-й серии без него системные вентиляторы принимают"
    echo "  запись, но не подчиняются ей. Лечится файлом ${OPTIONS_FILE}:"
    echo "      echo 'options nct6687 msi_fan_brute_force=1' | sudo tee ${OPTIONS_FILE}"
    echo "      sudo modprobe -r nct6687 && sudo modprobe nct6687"
  fi
  if [ "$WRITABLE_PWM" -gt 0 ]; then
    ok "  PWM-каналов на запись: ${WRITABLE_PWM} — управление возможно"
    echo "  Осталось включить управление в интерфейсе: Настройки → Охлаждение."
  else
    err "  ни одного записываемого PWM-канала"
    echo "  Частая причина — не выключен Smart Fan Mode в BIOS."
  fi
}

# --- main ------------------------------------------------------------------

cd "$REPO_ROOT"
case "$MODE" in
  check)
    scan_hwmon
    print_diagnosis
    print_verdict "$(classify)"
    echo
    echo "Ничего не изменено: это режим диагностики."
    ;;
  install)   scan_hwmon; print_diagnosis; do_install "$(classify)" ;;
  uninstall) do_uninstall ;;
  verify)    do_verify ;;
esac
