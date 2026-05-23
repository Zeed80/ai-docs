"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useState } from "react";
import { mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const NIL_UUID = "00000000-0000-0000-0000-000000000000";

interface CalendarEvent {
  id: string;
  title: string;
  event_date: string;
  event_type: string;
  entity_type: string | null;
  entity_id: string | null;
  source: string;
  created_at: string;
}

interface Reminder {
  id: string;
  entity_type: string;
  entity_id: string;
  remind_at: string;
  message: string;
  is_sent: boolean;
}

const EVENT_TYPE_LABELS: Record<string, string> = {
  due_date: "Срок оплаты",
  payment: "Оплата",
  delivery: "Доставка",
  meeting: "Встреча",
  invoice_date: "Дата счёта",
};

const EVENT_TYPE_COLORS: Record<string, string> = {
  due_date: "bg-red-500",
  payment: "bg-green-500",
  delivery: "bg-blue-500",
  meeting: "bg-purple-500",
  invoice_date: "bg-slate-400",
};

const MONTH_NAMES = [
  "Январь",
  "Февраль",
  "Март",
  "Апрель",
  "Май",
  "Июнь",
  "Июль",
  "Август",
  "Сентябрь",
  "Октябрь",
  "Ноябрь",
  "Декабрь",
];
const DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

export default function CalendarPage() {
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(30);
  const [view, setView] = useState<"list" | "month">("month");
  const today = new Date();
  const [monthOffset, setMonthOffset] = useState(0);

  const [showReminderForm, setShowReminderForm] = useState(false);
  const [reminderMsg, setReminderMsg] = useState("");
  const [reminderAt, setReminderAt] = useState("");
  const [reminderSaving, setReminderSaving] = useState(false);

  useEffect(() => {
    setLoading(true);
    mutFetch(`${API}/api/calendar/upcoming?days=${days}`)
      .then((r) => r.json())
      .then((data) => {
        setEvents(data.events ?? []);
        setReminders(data.reminders ?? []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [days]);

  const handleMarkSent = async (id: string) => {
    await mutFetch(`${API}/api/calendar/reminders/${id}/mark-sent`, {
      method: "POST",
    });
    setReminders((prev) => prev.filter((r) => r.id !== id));
  };

  const handleGenerateFollowup = async (id: string) => {
    const res = await mutFetch(
      `${API}/api/calendar/reminders/${id}/generate-followup`,
      { method: "POST" },
    );
    if (res.ok) {
      const data = await res.json();
      window.open(`/email/drafts/${data.draft_id}`, "_blank");
    }
  };

  const handleCreateReminder = async () => {
    if (!reminderMsg.trim() || !reminderAt) return;
    setReminderSaving(true);
    try {
      const res = await mutFetch(`${API}/api/calendar/reminders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entity_type: "note",
          entity_id: NIL_UUID,
          remind_at: new Date(reminderAt).toISOString(),
          message: reminderMsg.trim(),
        }),
      });
      if (res.ok) {
        const created: Reminder = await res.json();
        setReminders((prev) => [created, ...prev]);
        setReminderMsg("");
        setReminderAt("");
        setShowReminderForm(false);
      }
    } finally {
      setReminderSaving(false);
    }
  };

  if (loading) return <div className="p-6 text-slate-400">Загрузка...</div>;

  // Group events by ISO date string (YYYY-MM-DD)
  const grouped: Record<string, CalendarEvent[]> = {};
  for (const e of events) {
    const dateKey = new Date(e.event_date).toLocaleDateString("ru-RU");
    if (!grouped[dateKey]) grouped[dateKey] = [];
    grouped[dateKey].push(e);
  }

  // Build month grid cells
  const displayMonth = new Date(
    today.getFullYear(),
    today.getMonth() + monthOffset,
    1,
  );
  const firstDow = (displayMonth.getDay() + 6) % 7; // Mon=0
  const daysInMonth = new Date(
    displayMonth.getFullYear(),
    displayMonth.getMonth() + 1,
    0,
  ).getDate();
  // iso date → events map
  const byIso: Record<string, CalendarEvent[]> = {};
  for (const e of events) {
    const d = new Date(e.event_date);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    if (!byIso[iso]) byIso[iso] = [];
    byIso[iso].push(e);
  }

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold">Календарь</h1>
        <div className="flex items-center gap-2">
          <a
            href={`${API}/api/calendar/export.ics?days=${days}`}
            download="calendar.ics"
            className="px-3 py-1.5 text-sm bg-slate-700 text-slate-200 rounded hover:bg-slate-600"
          >
            iCal ↓
          </a>
          <button
            onClick={() => setShowReminderForm((v) => !v)}
            className="px-3 py-1.5 text-sm bg-amber-600 text-white rounded hover:bg-amber-700"
          >
            + Напоминание
          </button>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="bg-slate-800 border border-slate-600 text-slate-200 rounded px-2 py-1 text-sm"
          >
            <option value={7}>7 дней</option>
            <option value={14}>14 дней</option>
            <option value={30}>30 дней</option>
            <option value={60}>60 дней</option>
          </select>
          <div className="flex gap-1">
            {(["month", "list"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-2.5 py-1 text-xs rounded transition-colors ${view === v ? "bg-slate-600 text-slate-100" : "bg-slate-800 text-slate-400 hover:text-slate-200"}`}
              >
                {v === "month" ? "Месяц" : "Список"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Create reminder form */}
      {showReminderForm && (
        <div className="mb-5 bg-slate-800 border border-amber-700/40 rounded-lg p-4 space-y-3">
          <h3 className="text-sm font-semibold text-amber-300">
            Новое напоминание
          </h3>
          <textarea
            autoFocus
            value={reminderMsg}
            onChange={(e) => setReminderMsg(e.target.value)}
            placeholder="Текст напоминания..."
            rows={2}
            className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-amber-500 resize-none"
          />
          <input
            type="datetime-local"
            value={reminderAt}
            onChange={(e) => setReminderAt(e.target.value)}
            className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 rounded outline-none focus:border-amber-500"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => setShowReminderForm(false)}
              className="px-3 py-1.5 text-xs text-slate-400"
            >
              Отмена
            </button>
            <button
              onClick={handleCreateReminder}
              disabled={reminderSaving || !reminderMsg.trim() || !reminderAt}
              className="px-4 py-1.5 text-xs bg-amber-600 text-white rounded hover:bg-amber-700 disabled:opacity-50"
            >
              {reminderSaving ? "Сохраняю..." : "Создать"}
            </button>
          </div>
        </div>
      )}

      {/* Pending reminders */}
      {reminders.length > 0 && (
        <div className="mb-6">
          <h2 className="text-sm font-bold text-slate-400 mb-2">
            Напоминания ({reminders.length})
          </h2>
          <div className="space-y-2">
            {reminders.map((r) => (
              <div
                key={r.id}
                className="flex items-center justify-between bg-amber-950/30 border border-amber-700/40 rounded-lg px-4 py-2"
              >
                <div>
                  <div className="text-sm font-medium text-amber-300">
                    {r.message}
                  </div>
                  <div className="text-xs text-amber-500">
                    {new Date(r.remind_at).toLocaleString("ru-RU")}
                  </div>
                </div>
                <div className="flex gap-1.5">
                  {r.entity_type === "invoice" && (
                    <button
                      onClick={() => handleGenerateFollowup(r.id)}
                      className="px-2 py-1 text-xs bg-slate-700 text-slate-300 rounded hover:bg-slate-600"
                      title="Создать черновик follow-up письма"
                    >
                      Follow-up ↗
                    </button>
                  )}
                  <button
                    onClick={() => handleMarkSent(r.id)}
                    className="px-2 py-1 text-xs bg-amber-600 text-white rounded hover:bg-amber-700"
                  >
                    Выполнено
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Month grid view */}
      {view === "month" && (
        <div className="mb-6">
          <div className="flex items-center gap-3 mb-3">
            <button
              onClick={() => setMonthOffset((o) => o - 1)}
              className="px-2 py-1 text-sm bg-slate-700 text-slate-300 rounded hover:bg-slate-600"
            >
              ←
            </button>
            <span className="text-sm font-semibold text-slate-200 w-36 text-center">
              {MONTH_NAMES[displayMonth.getMonth()]}{" "}
              {displayMonth.getFullYear()}
            </span>
            <button
              onClick={() => setMonthOffset((o) => o + 1)}
              className="px-2 py-1 text-sm bg-slate-700 text-slate-300 rounded hover:bg-slate-600"
            >
              →
            </button>
            {monthOffset !== 0 && (
              <button
                onClick={() => setMonthOffset(0)}
                className="text-xs text-slate-500 hover:text-slate-300"
              >
                Сегодня
              </button>
            )}
          </div>
          <div className="grid grid-cols-7 gap-px bg-slate-700 rounded-lg overflow-hidden border border-slate-700">
            {DAY_NAMES.map((d) => (
              <div
                key={d}
                className="bg-slate-800 text-center text-[10px] font-semibold text-slate-500 py-1"
              >
                {d}
              </div>
            ))}
            {Array.from({ length: firstDow }).map((_, i) => (
              <div
                key={`empty-${i}`}
                className="bg-slate-900/50 min-h-[60px]"
              />
            ))}
            {Array.from({ length: daysInMonth }).map((_, i) => {
              const day = i + 1;
              const iso = `${displayMonth.getFullYear()}-${String(displayMonth.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
              const dayEvents = byIso[iso] ?? [];
              const isToday =
                today.getFullYear() === displayMonth.getFullYear() &&
                today.getMonth() === displayMonth.getMonth() &&
                today.getDate() === day;
              return (
                <div
                  key={day}
                  className={`bg-slate-900 min-h-[60px] p-1 ${isToday ? "ring-1 ring-inset ring-blue-500" : ""}`}
                >
                  <div
                    className={`text-[11px] font-medium mb-0.5 w-5 h-5 flex items-center justify-center rounded-full ${isToday ? "bg-blue-600 text-white" : "text-slate-400"}`}
                  >
                    {day}
                  </div>
                  <div className="space-y-0.5">
                    {dayEvents.slice(0, 3).map((e) => (
                      <div
                        key={e.id}
                        title={e.title}
                        className={`text-[9px] px-1 rounded truncate ${EVENT_TYPE_COLORS[e.event_type]?.replace("bg-", "bg-") ?? "bg-slate-500"} text-white`}
                      >
                        {e.title}
                      </div>
                    ))}
                    {dayEvents.length > 3 && (
                      <div className="text-[9px] text-slate-500">
                        +{dayEvents.length - 3}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Events by date (list view) */}
      {view === "list" &&
        (Object.keys(grouped).length === 0 ? (
          <div className="text-slate-400 text-sm">
            Нет событий на ближайшие {days} дней
          </div>
        ) : (
          <div className="space-y-4">
            {Object.entries(grouped).map(([date, dateEvents]) => (
              <div key={date}>
                <h3 className="text-sm font-bold text-slate-400 mb-2">
                  {date}
                </h3>
                <div className="space-y-1.5">
                  {dateEvents.map((e) => (
                    <div
                      key={e.id}
                      className="flex items-center gap-3 bg-slate-800 border border-slate-700 rounded-lg px-4 py-2.5"
                    >
                      <span
                        className={`w-2.5 h-2.5 rounded-full shrink-0 ${
                          EVENT_TYPE_COLORS[e.event_type] ?? "bg-slate-400"
                        }`}
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium truncate">
                          {e.title}
                        </div>
                        <div className="text-xs text-slate-500">
                          {EVENT_TYPE_LABELS[e.event_type] ?? e.event_type}
                          {e.source !== "manual" && (
                            <span className="ml-2 text-slate-400">
                              ({e.source})
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="text-xs text-slate-400 shrink-0">
                        {new Date(e.event_date).toLocaleTimeString("ru-RU", {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ))}
    </div>
  );
}
