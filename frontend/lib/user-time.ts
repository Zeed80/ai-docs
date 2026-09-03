"use client";

import { useCurrentUser } from "./auth-context";

/**
 * Даты в одном часовом поясе на всё приложение.
 *
 * До этого каждый экран звал `toLocaleString()` без зоны, то есть показывал
 * время устройства. Для человека, сидящего на площадке, это почти всегда одно
 * и то же — но бухгалтерия в другом городе видела не время события на
 * производстве, а своё, и «письмо пришло в 9:15» у двух коллег означало
 * разные моменты. Зона берётся из профиля (users.timezone); не задана —
 * поведение прежнее, по устройству.
 */
export function useUserTimeZone(): string | undefined {
  const user = useCurrentUser();
  return user?.timezone ?? undefined;
}

export function formatDateTime(
  value: string | Date | null | undefined,
  timeZone?: string,
  opts?: Intl.DateTimeFormatOptions,
): string {
  if (!value) return "";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, { timeZone, ...opts });
}

export function formatDate(
  value: string | Date | null | undefined,
  timeZone?: string,
  opts?: Intl.DateTimeFormatOptions,
): string {
  if (!value) return "";
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { timeZone, ...opts });
}

export function formatTime(
  value: string | Date | null | undefined,
  timeZone?: string,
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
export function dayKey(value: string | Date, timeZone?: string): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return date.toLocaleDateString("en-CA", { timeZone });
}
