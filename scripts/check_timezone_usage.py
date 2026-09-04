#!/usr/bin/env python3
"""Даты в интерфейсе показываются в часовом поясе профиля, а не устройства.

Зона живёт в ``users.timezone`` и попадает во фронтенд через
``lib/user-time.ts`` (``tz()``). Правило простое: любой ``toLocale*String`` над
датой обязан получить ``timeZone``. Без этого экран снова начнёт показывать
время устройства, и «письмо пришло в 9:15» у двух коллег из разных городов
опять будет означать разные моменты — а заметить такое глазами почти
невозможно, ошибка тихая.

Проверяются только заведомо датовые вызовы: тот же метод форматирует числа
(суммы, количества), и их трогать не нужно.

Запуск: python3 scripts/check_timezone_usage.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "frontend"
SCAN = ("app", "components")
# lib/user-time.ts — сам источник форматирования, там timeZone параметр.
SKIP = {"lib/user-time.ts"}

CALL = re.compile(r"\.toLocale(?:Date|Time)?String\(")
DATE_HINT = re.compile(r"new Date|_at\b|At\b|date|Date|timestamp", re.IGNORECASE)


def main() -> int:
    misses: list[str] = []
    checked = 0
    for folder in SCAN:
        for path in sorted((ROOT / folder).rglob("*.tsx")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in SKIP:
                continue
            src = path.read_text()
            for match in CALL.finditer(src):
                window = src[max(0, match.start() - 90) : match.start() + 160]
                if not DATE_HINT.search(window):
                    continue  # число, а не дата
                checked += 1
                if "timeZone" in window:
                    continue
                line = src[: match.start()].count("\n") + 1
                misses.append(f"{rel}:{line}")

    if misses:
        print("Дата форматируется без часового пояса профиля (нужен timeZone: tz()):")
        for m in misses:
            print(f"  {m}")
        print("\nСм. frontend/lib/user-time.ts")
        return 1

    print(f"OK: {checked} датовых вызовов, все с часовым поясом профиля")
    return 0


if __name__ == "__main__":
    sys.exit(main())
