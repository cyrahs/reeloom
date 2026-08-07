import type { ZodType } from "zod";

export const TOKEN_STORAGE_KEY = "reeloom.admin_bearer.v1";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly context: Readonly<Record<string, string>> = {},
  ) {
    super(code);
    this.name = "ApiError";
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

export class ApiClient {
  constructor(
    readonly token: string,
    private readonly onUnauthorized: () => void,
  ) {}

  async request<T>(
    path: string,
    schema: ZodType<T>,
    options: RequestOptions = {},
  ): Promise<T> {
    let response: Response;
    try {
      response = await fetch(path, {
        method: options.method ?? "GET",
        body:
          options.body === undefined
            ? undefined
            : JSON.stringify(options.body),
        signal: options.signal,
        headers: {
          Authorization: `Bearer ${this.token}`,
          ...(options.body === undefined
            ? {}
            : { "Content-Type": "application/json" }),
          ...options.headers,
        },
      });
    } catch {
      throw new ApiError(0, "network_uncertain");
    }
    if (response.status === 401) {
      this.onUnauthorized();
    }
    if (!response.ok) {
      const error = await safeError(response);
      throw new ApiError(response.status, error.code, error.context);
    }
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      throw new ApiError(response.status, "invalid_response");
    }
    const parsed = schema.safeParse(value);
    if (!parsed.success) {
      throw new ApiError(response.status, "invalid_response");
    }
    return parsed.data;
  }
}

async function safeError(
  response: Response,
): Promise<{ code: string; context: Readonly<Record<string, string>> }> {
  try {
    const value: unknown = await response.json();
    if (
      typeof value === "object" &&
      value !== null &&
      "error" in value &&
      typeof value.error === "object" &&
      value.error !== null &&
      "code" in value.error &&
      typeof value.error.code === "string"
    ) {
      const context: Record<string, string> = {};
      if (
        "context" in value.error &&
        typeof value.error.context === "object" &&
        value.error.context !== null
      ) {
        for (const [key, item] of Object.entries(value.error.context)) {
          if (typeof item === "string") {
            context[key] = item;
          }
        }
      }
      return { code: value.error.code, context };
    }
  } catch {
    // A server error body is untrusted and optional.
  }
  return { code: `http_${response.status}`, context: {} };
}

export function idempotencyKey(): string {
  return `ui-v1-${crypto.randomUUID()}`;
}

export function byteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}
