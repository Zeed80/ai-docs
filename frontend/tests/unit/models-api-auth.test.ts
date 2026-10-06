import { afterEach, describe, expect, it, vi } from "vitest";

const redirectToLogin = vi.fn();
const refreshSession = vi.fn();
vi.mock("@/lib/auth", () => ({
  csrfHeaders: () => ({}),
  redirectToLogin: () => redirectToLogin(),
  refreshSession: () => refreshSession(),
}));

import { ApiError, strataConfig } from "@/lib/models/api";

const body = { model: "IQ2_XS", context: 32768, vision: true };
const json = (status: number, data: unknown) =>
  new Response(JSON.stringify(data), { status });

describe("models API on an expired session", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    redirectToLogin.mockReset();
    refreshSession.mockReset();
  });

  it("renews the session and repeats the request", async () => {
    refreshSession.mockResolvedValue(true);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(401, { detail: "Not authenticated" }))
      .mockResolvedValueOnce(
        json(200, { ok: true, restarted: true, status: {} }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(strataConfig(body)).resolves.toMatchObject({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(redirectToLogin).not.toHaveBeenCalled();
  });

  it("sends the browser to login when there is nothing to renew from", async () => {
    refreshSession.mockResolvedValue(false);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(401, { detail: "Not authenticated" })),
    );
    await expect(strataConfig(body)).rejects.toBeInstanceOf(ApiError);
    expect(redirectToLogin).toHaveBeenCalledTimes(1);
  });

  it("leaves other errors to the caller", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(409, { detail: "Мало места" })),
    );
    await expect(strataConfig(body)).rejects.toThrow("Мало места");
    expect(refreshSession).not.toHaveBeenCalled();
    expect(redirectToLogin).not.toHaveBeenCalled();
  });
});
