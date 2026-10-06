import { describe, expect, it } from "vitest";
import { pauseLabel } from "@/lib/work-order-pause";

describe("подписи паузы", () => {
  it("различает запрошенную и подтверждённую паузу", () => {
    expect(pauseLabel({ status: "running", metadata: { pause: { requested_at: "t" } } }))
      .toBe("Пауза запрошена — ждём завершения текущего шага");
    expect(pauseLabel({ status: "paused", metadata: { pause: { acknowledged_at: "t" } } }))
      .toBe("Приостановлено безопасно: новые шаги не запускаются");
    expect(pauseLabel({ status: "running", metadata: {} })).toBeNull();
  });
});
