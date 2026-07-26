import { parseSseFrames, streamRunEvents } from "../src/sse";

test("parses framed events and preserves a fragmented remainder", () => {
  const first = parseSseFrames(
    'id: 4\nevent: run_event\ndata: {"event_type":"plan_built","data":{"plan_hash":"sha256:x"}}\n\nid: 5\ndata: {"event_',
  );
  expect(first.events).toEqual([
    {
      event_id: 4,
      event_type: "plan_built",
      data: { plan_hash: "sha256:x" },
    },
  ]);
  expect(first.remainder).toContain("id: 5");

  const second = parseSseFrames(
    `${first.remainder}type":"run_completed","data":{"applied_count":2}}\n\n`,
  );
  expect(second.events[0]).toMatchObject({
    event_id: 5,
    event_type: "run_completed",
  });
});

test("ignores comments and malformed untrusted data", () => {
  const result = parseSseFrames(
    ': keepalive\n\nid: nope\ndata: <script>alert(1)</script>\n\n',
  );
  expect(result.events).toEqual([]);
});

test("reconnects after a mid-stream read failure", async () => {
  vi.useFakeTimers();
  const controller = new AbortController();
  let calls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      calls += 1;
      if (calls === 1) {
        return new Response(
          new ReadableStream({
            start(stream) {
              stream.error(new Error("connection dropped"));
            },
          }),
          { status: 200 },
        );
      }
      controller.abort();
      return new Response(null, { status: 204 });
    }),
  );

  const running = streamRunEvents({
    runId: "run-1",
    token: "admin-token",
    signal: controller.signal,
    onEvents: () => undefined,
    onCursorAhead: async () => 0,
    onUnauthorized: () => undefined,
  });
  await vi.runAllTimersAsync();
  await running;

  expect(calls).toBe(2);
  vi.useRealTimers();
});

test("backs off repeated clean stream endings exponentially", async () => {
  vi.useFakeTimers();
  const controller = new AbortController();
  const calledAt: number[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      calledAt.push(Date.now());
      if (calledAt.length === 3) controller.abort();
      return new Response(new ReadableStream({ start(stream) {
        stream.close();
      } }), { status: 200 });
    }),
  );

  const running = streamRunEvents({
    runId: "run-1",
    token: "admin-token",
    signal: controller.signal,
    onEvents: () => undefined,
    onCursorAhead: async () => 0,
    onUnauthorized: () => undefined,
  });
  await vi.runAllTimersAsync();
  await running;

  expect(calledAt[1] - calledAt[0]).toBe(500);
  expect(calledAt[2] - calledAt[1]).toBe(1_000);
  vi.useRealTimers();
});
