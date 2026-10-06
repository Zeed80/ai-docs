// E22: a requested pause waits for the executing step to finish; "paused"
// is the acknowledged safe boundary. The UI must tell the two apart.
export type PausableOrder = {
  status: string;
  metadata?: { pause?: { requested_at?: string; acknowledged_at?: string } } | null;
};

export function pauseLabel(order: PausableOrder): string | null {
  if (order.status === "paused") return "Приостановлено безопасно: новые шаги не запускаются";
  if (order.metadata?.pause) return "Пауза запрошена — ждём завершения текущего шага";
  return null;
}
