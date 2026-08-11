const TOKEN_KEY = "reeloom.token";

export type RunState =
  | "pending"
  | "identifying"
  | "executing"
  | "acquiring_subs"
  | "reverting"
  | "discarding"
  | "done"
  | "needs_attention"
  | "discarded"
  | "failed";

export interface RunResult {
  moved: number;
  duplicates: string[];
  missing: string[];
  archived: number;
  subtitles_acquired: number;
  subtitle_note: string;
}

export interface RunSummary {
  id: string;
  config_id: string;
  folder_name: string;
  state: RunState;
  title: string | null;
  year: number | null;
  tmdb_id: number | null;
  file_count: number;
  move_count: number;
  result: RunResult | null;
  error: Record<string, unknown> | null;
  attempts: number;
}

export interface IntakeFolder {
  config_id: string;
  config_name: string;
  folder_name: string;
  file_count: number;
  total_bytes: number;
  status: "settling" | "empty" | "skipped";
  reason: string | null;
  /** Seconds left in the stability window, measured at scanned_at. */
  remaining_seconds: number | null;
  scanned_at: number;
}

export interface IntakeReport {
  /** Server clock at response time, for turning remaining_seconds into a deadline. */
  now: number;
  folders: IntakeFolder[];
}

export interface Move {
  kind: string;
  source_root: string;
  source_path: string;
  dest_root: string;
  dest_path: string;
  candidate_id: string | null;
}

export interface RunDetail extends RunSummary {
  snapshot: {
    candidate_id: string;
    relative_path: string;
    kind: string;
    size_bytes: number;
    variant: string | null;
  }[];
  plan: {
    identity: { title: string; year: number; tmdb_id: number };
    moves: Move[];
    unmapped: string[];
    notes: string;
  } | null;
  executed_moves: { move: Move; outcome: string }[];
  logs: { ts: string; level: string; message: string }[];
  interactions: { role: string; content: string; ts: string }[];
}

export interface WatchConfig {
  id: string;
  name: string;
  inbound_root: string;
  library_root: string;
  media_type: "anime" | "tv" | "movie";
  enabled: boolean;
  stability_seconds: number;
  acquire_subtitles: boolean;
  notify: boolean;
}

export interface DirListing {
  path: string;
  parent: string | null;
  dirs: string[];
}

export interface Settings {
  llm_base_url: string;
  llm_model: string;
  telegram_chat_id: string;
  tmdb_api_key_set: boolean;
  llm_api_key_set: boolean;
  telegram_bot_token_set: boolean;
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(value: string): void {
  localStorage.setItem(TOKEN_KEY, value);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      Authorization: `Bearer ${getToken()}`,
      ...init.headers,
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      // Keep the status text.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const api = {
  listRuns: () => request<{ runs: RunSummary[] }>("/runs"),
  listIntake: () => request<IntakeReport>("/intake"),
  getRun: (id: string) => request<RunDetail>(`/runs/${id}`),
  ask: (id: string, message: string) =>
    post<{ answer: string }>(`/runs/${id}/ask`, { message }),
  revise: (id: string, message: string) =>
    post<{ state: string }>(`/runs/${id}/revise`, { message }),
  retry: (id: string) => post<{ state: string }>(`/runs/${id}/retry`),
  discard: (id: string) => post<{ state: string }>(`/runs/${id}/discard`),
  deleteRun: (id: string) =>
    request<{ deleted: boolean }>(`/runs/${id}`, { method: "DELETE" }),

  listConfigs: () => request<{ configs: WatchConfig[] }>("/configs"),
  createConfig: (body: Partial<WatchConfig>) =>
    post<WatchConfig>("/configs", body),
  updateConfig: (id: string, body: Partial<WatchConfig>) =>
    request<WatchConfig>(`/configs/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteConfig: (id: string) =>
    request<{ deleted: boolean }>(`/configs/${id}`, { method: "DELETE" }),

  listDirs: (path: string) =>
    request<DirListing>(`/fs/dirs?path=${encodeURIComponent(path)}`),

  getSettings: () => request<Settings>("/settings"),
  putSettings: (body: Record<string, string>) =>
    request<{ updated: boolean }>("/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};

export const STATE_LABEL: Record<RunState, string> = {
  pending: "等待处理",
  identifying: "识别中",
  executing: "整理中",
  acquiring_subs: "获取字幕",
  reverting: "复原中",
  discarding: "放弃中",
  done: "完成",
  needs_attention: "需要处理",
  discarded: "已放弃",
  failed: "失败",
};

export const ACTIVE_STATES: RunState[] = [
  "pending",
  "identifying",
  "executing",
  "acquiring_subs",
  "reverting",
  "discarding",
];
