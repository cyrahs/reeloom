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

test("API client preserves bounded executor error context", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        error: {
          code: "recovery_required",
          context: {
            candidate_id: "video:1",
            source_state: "absent",
            ignored: 123,
          },
        },
      }),
      {
        status: 409,
        headers: { "Content-Type": "application/json" },
      },
    ),
  );
  const api = new ApiClient("admin-token", () => undefined);

  await expect(api.request("/test", z.never())).rejects.toMatchObject({
    status: 409,
    code: "recovery_required",
    context: {
      candidate_id: "video:1",
      source_state: "absent",
    },
  });
});
