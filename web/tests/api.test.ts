import { z } from "zod";

import { ApiClient } from "../src/api";

test("API client sends authenticated DELETE requests", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  const api = new ApiClient("admin-token", () => undefined);

  await api.request("/api/v1/runs/run-1", z.object({ ok: z.literal(true) }), {
    method: "DELETE",
    headers: { "Idempotency-Key": "key-1" },
  });

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/v1/runs/run-1",
    expect.objectContaining({
      method: "DELETE",
      headers: expect.objectContaining({
        Authorization: "Bearer admin-token",
        "Idempotency-Key": "key-1",
      }),
    }),
  );
});
