import { eventsSchema, type RunEvent } from "./schemas";
import { ApiError } from "./api";

export const cursorKey = (runId: string) =>
  `reeloom.event_cursor.v1.${runId}`;

export function parseSseFrames(
  buffer: string,
): { events: RunEvent[]; remainder: string } {
  const parts = buffer.split("\n\n");
  const remainder = parts.pop() ?? "";
  const events: RunEvent[] = [];
  for (const frame of parts) {
    let id: number | null = null;
    let data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("id:")) id = Number(line.slice(3).trim());
      if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (id === null || !Number.isSafeInteger(id) || !data) continue;
    try {
      const payload: unknown = JSON.parse(data);
      if (
        typeof payload === "object" &&
        payload !== null &&
        "event_type" in payload &&
        "data" in payload
      ) {
        const parsed = eventsSchema.shape.items.element.safeParse({
          event_id: id,
          event_type: payload.event_type,
          data: payload.data,
        });
        if (parsed.success) events.push(parsed.data);
      }
    } catch {
      // Ignore malformed untrusted frames; REST resync remains authoritative.
    }
  }
  return { events, remainder };
}

export async function readAllEvents(
  apiRequest: (
    path: string,
    schema: typeof eventsSchema,
  ) => Promise<{ items: RunEvent[] }>,
  runId: string,
): Promise<RunEvent[]> {
  const result: RunEvent[] = [];
  let after = 0;
  for (let page = 0; page < 100; page += 1) {
    const next = await apiRequest(
      `/api/v1/runs/${encodeURIComponent(runId)}/events?after=${after}&limit=100`,
      eventsSchema,
    );
    result.push(...next.items);
    if (next.items.length < 100) return result;
    after = next.items.at(-1)?.event_id ?? after;
  }
  throw new ApiError(409, "event_history_too_large");
}

type StreamOptions = {
  runId: string;
  token: string;
  signal: AbortSignal;
  onEvents: (events: RunEvent[]) => void;
  onCursorAhead: () => Promise<number>;
  onUnauthorized: () => void;
};

export async function streamRunEvents(options: StreamOptions): Promise<void> {
  let delay = 500;
  while (!options.signal.aborted) {
    const cursor = Number(
      window.localStorage.getItem(cursorKey(options.runId)) ?? 0,
    );
    let response: Response;
    try {
      response = await fetch(
        `/api/v1/runs/${encodeURIComponent(options.runId)}/events/stream`,
        {
          headers: {
            Authorization: `Bearer ${options.token}`,
            "Last-Event-ID": String(
              Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : 0,
            ),
          },
          signal: options.signal,
        },
      );
    } catch {
      delay = await retryAfter(delay, options.signal);
      continue;
    }
    if (response.status === 401) {
      options.onUnauthorized();
      return;
    }
    if (response.status === 409) {
      try {
        const resynced = await options.onCursorAhead();
        window.localStorage.setItem(cursorKey(options.runId), String(resynced));
        delay = 500;
      } catch {
        delay = await retryAfter(delay, options.signal);
      }
      continue;
    }
    if (!response.ok || response.body === null) {
      delay = await retryAfter(delay, options.signal);
      continue;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (!options.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder
          .decode(value, { stream: true })
          .replaceAll("\r\n", "\n");
        const parsed = parseSseFrames(buffer);
        buffer = parsed.remainder;
        const current = Number(
          window.localStorage.getItem(cursorKey(options.runId)) ?? 0,
        );
        const fresh = parsed.events.filter(
          (event) => event.event_id > current,
        );
        if (fresh.length) {
          window.localStorage.setItem(
            cursorKey(options.runId),
            String(fresh.at(-1)?.event_id ?? current),
          );
          delay = 500;
          options.onEvents(fresh);
        }
      }
    } catch {
      if (options.signal.aborted) return;
    } finally {
      reader.releaseLock();
    }
    delay = await retryAfter(delay, options.signal);
  }
}

async function retryAfter(
  delay: number,
  signal: AbortSignal,
): Promise<number> {
  await backoff(delay, signal);
  return Math.min(delay * 2, 10_000);
}

function backoff(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}
