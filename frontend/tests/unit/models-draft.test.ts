import { describe, expect, it } from "vitest";
import { buildDraftPayload } from "@/lib/models/draft";

describe("черновик назначения моделей", () => {
  it("слот без дополнительных решений несёт только модель", () => {
    expect(
      buildDraftPayload({ cad_spec_read: "qwen3_vl_30b_a3b_ollama" }),
    ).toEqual({
      cad_spec_read: { model: "qwen3_vl_30b_a3b_ollama" },
    });
  });

  it("решение об облаке едет вместе с моделью, а не отдельным запросом", () => {
    const payload = buildDraftPayload(
      { cad_spec_read: "claude_sonnet_anthropic" },
      { cad_spec_read: true },
    );
    expect(payload.cad_spec_read).toEqual({
      model: "claude_sonnet_anthropic",
      allow_cloud: true,
    });
  });

  it("«не использовать» — отдельное поле, модель при этом сохраняется", () => {
    // Выключение не должно требовать снятия модели: включить слот обратно
    // нужно уметь, ничего не выбирая заново.
    const payload = buildDraftPayload(
      { cad_text_ocr: "glm_ocr_ollama" },
      {},
      { cad_text_ocr: true },
    );
    expect(payload.cad_text_ocr).toEqual({
      model: "glm_ocr_ollama",
      disabled: true,
    });
  });

  it("нетронутые решения не попадают в запрос", () => {
    // Разница между «выключил» и «не трогал» существенна: отправив
    // disabled:false там, где решения не принимали, черновик молча включил бы
    // слот, выключенный когда-то раньше.
    const payload = buildDraftPayload({ cad_text_ocr: "glm_ocr_ollama" });
    expect(payload.cad_text_ocr).not.toHaveProperty("disabled");
    expect(payload.cad_text_ocr).not.toHaveProperty("allow_cloud");
  });

  it("явное включение обратно отправляется, а не опускается", () => {
    const payload = buildDraftPayload(
      { cad_text_ocr: "glm_ocr_ollama" },
      {},
      { cad_text_ocr: false },
    );
    expect(payload.cad_text_ocr.disabled).toBe(false);
  });

  it("слоты правятся независимо друг от друга", () => {
    const payload = buildDraftPayload(
      {
        cad_text_ocr: "glm_ocr_ollama",
        cad_spec_read: "qwen3_vl_30b_a3b_ollama",
      },
      { cad_spec_read: false },
      { cad_text_ocr: true },
    );
    expect(payload).toEqual({
      cad_text_ocr: { model: "glm_ocr_ollama", disabled: true },
      cad_spec_read: { model: "qwen3_vl_30b_a3b_ollama", allow_cloud: false },
    });
  });

  it("снятая модель остаётся null — это сброс к дефолту реестра", () => {
    expect(
      buildDraftPayload({ cad_text_ocr: null }).cad_text_ocr.model,
    ).toBeNull();
  });
});
