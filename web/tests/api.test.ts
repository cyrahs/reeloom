import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, clearToken, getToken, setToken } from "../src/api";

function mockFetch(response: unknown, status = 200) {
  const fetchMock = vi.fn(async (path: string, init: RequestInit) => ({
    ok: status < 400,
    status,
    statusText: "Error",
    url: path,
    method: init.method,
    json: async () => response,
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("token storage", () => {
  beforeEach(() => clearToken());

  it("round-trips through localStorage", () => {
    expect(getToken()).toBe("");
    setToken("abc");
    expect(getToken()).toBe("abc");
    clearToken();
    expect(getToken()).toBe("");
  });
});

describe("api client", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    setToken("secret");
  });

  it("sends the bearer token on every request", async () => {
    const fetchMock = mockFetch({ runs: [] });

    await api.listRuns();

    const [, init] = fetchMock.mock.calls[0];
    expect(
      (init.headers as Record<string, string>).Authorization,
    ).toBe("Bearer secret");
  });

  it("sets a JSON content type only when there is a body", async () => {
    const fetchMock = mockFetch({ state: "pending" });

    await api.retry("run-1");
    const [, noBody] = fetchMock.mock.calls[0];
    expect((noBody.headers as Record<string, string>)["Content-Type"]).toBe(
      undefined,
    );

    await api.revise("run-1", "season 2");
    const [, withBody] = fetchMock.mock.calls[1];
    expect((withBody.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
  });

  it("surfaces the server's error detail", async () => {
    mockFetch({ detail: "run_is_busy" }, 409);

    await expect(api.revise("run-1", "x")).rejects.toThrowError(ApiError);
    await expect(api.revise("run-1", "x")).rejects.toThrowError("run_is_busy");
  });
});
