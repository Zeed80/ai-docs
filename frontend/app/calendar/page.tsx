"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useState } from "react";

const API = getApiBaseUrl();

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

export default function CalendarPage() {
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(14);

  useEffect(() => {
    setLoading(true);
    fetch(`${API}/api/calendar/upcoming?days=${days}`)
      .then((r) => r.json())
      .then((data) => {
        setEvents(data.events ?? []);
        setReminders(data.reminders ?? []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [days]);

  const handleMarkSent = async (id: string) => {
    await fetch(`${API}/api/calendar/reminders/${id}/mark-sent`, {
      method: "POST",
    });
    setReminders((prev) => prev.filter((r) => r.id !== id));
  };

  if (loading) return <div className="p-6 text-slate-400">Загрузка...</div>;

  // Group events by date
  const grouped: Record<string, CalendarEvent[]> = {};
  for (const e of events) {
    const dateKey = new Date(e.event_date).toLocaleDateString("ru-RU");
    if (!grouped[dateKey]) grouped[dateKey] = [];
    grouped[dateKey].push(e);
  }

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-bold">Календарь</h1>
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
      </div>

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
                <button
                  onClick={() => handleMarkSent(r.id)}
                  className="px-2 py-1 text-xs bg-amber-600 text-white rounded hover:bg-amber-700"
                >
                  Выполнено
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Events by date */}
      {Object.keys(grouped).length === 0 ? (
        <div className="text-slate-400 text-sm">
          Нет событий на ближайшие {days} дней
        </div>
      ) : (
        <div className="space-y-4">
          {Object.entries(grouped).map(([date, dateEvents]) => (
            <div key={date}>
              <h3 className="text-sm font-bold text-slate-400 mb-2">{date}</h3>
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
      )}
    </div>
  );
}
