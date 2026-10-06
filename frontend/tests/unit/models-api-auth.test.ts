import { afterEach, describe, expect, it, vi } from "vitest";

const redirectToLogin = vi.fn();
vi.mock("@/lib/auth", () => ({
  csrfHeaders: () => ({}),
  redirectToLogin: () => redirectToLogin(),
}));

import { ApiError, strataConfig } from "@/lib/models/api";

describe("models API on an expired session", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    redirectToLogin.mockReset();
  });

  it("sends the browser to login instead of a button-specific error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "Not authenticated" }), {
            status: 401,
          }),
      ),
    );
    await expect(
      strataConfig({ model: "IQ2_XS", context: 32768, vision: true }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(redirectToLogin).toHaveBeenCalledTimes(1);
  });

  it("leaves other errors to the caller", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "Мало места" }), {
            status: 409,
          }),
      ),
    );
    await expect(
      strataConfig({ model: "IQ2_XS", context: 32768, vision: true }),
    ).rejects.toThrow("Мало места");
    expect(redirectToLogin).not.toHaveBeenCalled();
  });
});
