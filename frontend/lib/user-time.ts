"use client";

import { useCurrentUser } from "./auth-context";

/**
 * Даты в одном часовом поясе на всё приложение.
 *
 * Каждый экран звал `toLocaleString()` без зоны, то есть показывал время
 * устройства. Для человека на площадке это почти всегда одно и то же — но
 * бухгалтерия в другом городе видела не время события на производстве, а
 * своё, и «письмо пришло в 9:15» у двух коллег означало разные моменты.
 *
 * Зона живёт в профиле (`users.timezone`). Здесь она продублирована в
 * модульную переменную по одной причине: форматируют даты не только React-
 * компоненты, но и утилиты, где хука нет. `AuthProvider` кладёт её сюда при
 * загрузке профиля; не задана — поведение прежнее, по устройству.
 */
let currentTimeZone: string | undefined;

export function setUserTimeZone(tz: string | null | undefined): void {
  currentTimeZone = tz ?? undefined;
}

/** Зона профиля для форматирования. `undefined` = как решит браузер. */
export function tz(): string | undefined {
  return currentTimeZone;
}

/** То же самое как хук — чтобы компонент перерисовался, когда профиль
 *  догрузился. */
export function useUserTimeZone(): string | undefined {
  const user = useCurrentUser();
  return user?.timezone ?? currentTimeZone;
}

export function formatDateTime(
  value: string | Date | null | undefined,
  timeZone: string | undefined = currentTimeZone,
  opts?: Intl.DateTimeFormatOptions,
): string {
  if (!value) return "";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, { timeZone, ...opts });
}

export function formatDate(
  value: string | Date | null | undefined,
  timeZone: string | undefined = currentTimeZone,
  opts?: Intl.DateTimeFormatOptions,
): string {
  if (!value) return "";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { timeZone, ...opts });
}

export function formatTime(
  value: string | Date | null | undefined,
  timeZone: string | undefined = currentTimeZone,
  opts?: Intl.DateTimeFormatOptions,
): string {
  if (!value) return "";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    ...opts,
  });
}

/** «Сегодня» и «вчера» тоже относительны зоне: в UTC+10 день сменился, а на
 *  устройстве в UTC+3 ещё нет. */
export function dayKey(value: string | Date, timeZone: string | undefined = currentTimeZone): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return date.toLocaleDateString("en-CA", { timeZone });
}
