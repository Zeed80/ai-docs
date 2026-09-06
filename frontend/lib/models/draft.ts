/**
 * Сборка черновика назначения моделей в форму, которую ждёт сервер.
 *
 * Жила внутри AssignmentBoard и росла вместе с ним: сначала слот нёс только имя
 * модели, потом к нему добавилось решение об облаке, теперь — «не использовать».
 * Каждое из этих решений применяется ОДНОЙ кнопкой вместе с моделью, и правило,
 * какое поле когда попадает в запрос, стоит держать отдельно от разметки — его
 * легко проверить и трудно случайно сломать при правке вёрстки.
 */

/** Черновик одного слота на проводе. Поля опциональны намеренно. */
export interface SlotDraftPayload {
  model: string | null;
  /** Разрешение облака для конфиденциального слота. */
  allow_cloud?: boolean;
  /** «Не использовать»: стадия не выполняется вовсе. */
  disabled?: boolean;
}

/**
 * Собрать тело запроса из трёх независимых карт правок.
 *
 * Поля, которых оператор не касался, в запрос НЕ попадают: разница между
 * «выключил» и «не трогал» здесь существенна. Отправив `disabled: false` там,
 * где решения не принимали, черновик молча включил бы слот, выключенный
 * когда-то раньше.
 */
export function buildDraftPayload(
  models: Record<string, string | null>,
  cloud: Record<string, boolean> = {},
  disabled: Record<string, boolean> = {},
): Record<string, SlotDraftPayload> {
  return Object.fromEntries(
    Object.entries(models).map(([slot, model]) => [
      slot,
      {
        model,
        ...(cloud[slot] === undefined ? {} : { allow_cloud: cloud[slot] }),
        ...(disabled[slot] === undefined ? {} : { disabled: disabled[slot] }),
      },
    ]),
  );
}
