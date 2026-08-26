const TOKEN_KEY = "reeloom.token";

export type RunState =
  | "pending"
  | "identifying"
  | "comparing"
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
  subtitles_moved: number;
  subtitles_acquired: number;
  subtitles_embedded: number;
  subtitle_note: string;
  replaced: string[];
  discarded: string[];
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
  created_at: string | null;
  updated_at: string | null;
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
  extra_base?: string | null;
}

export interface EpisodeSpan {
  season: number;
  episode_start: number;
  episode_end: number;
}

export interface ReplaceExistingFile {
  root: string;
  extra_base: string | null;
  relative_path: string;
  size_bytes: number;
  span: EpisodeSpan | null;
}

export interface ReplaceComparison {
  span: EpisodeSpan | null;
  candidate_id: string;
  incoming_bytes: number;
  existing: ReplaceExistingFile[];
  existing_bytes: number;
}

export type ReplaceVerdict = "import" | "replace" | "discard" | "manual";

export interface ReplaceGroup {
  season: number | null;
  verdict: ReplaceVerdict;
  ratio: number | null;
  quality: "better" | "same" | "worse" | "unknown";
  overlap: ReplaceComparison[];
  new_episodes: number[];
  reason: string;
}

export interface ReplaceDecision {
  groups: ReplaceGroup[];
  existing_subtitles: string[];
  needs_confirmation: boolean;
  resolution: string | null;
}

export type ReplaceAction = "replace" | "discard_incoming" | "keep_both";

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
  replace_decision: ReplaceDecision | null;
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
  subtitle_variant: "chs" | "cht";
  notify: boolean;
  replace_enabled: boolean;
  replace_extra_dirs: string[];
  replace_auto_ratio: number;
}

export interface DirListing {
  path: string;
  parent: string | null;
  dirs: string[];
}

export interface Settings {
  llm_base_url: string;
  llm_model: string;
  /** "" means provider default: the parameter is never sent. */
  llm_reasoning_effort: string;
  telegram_chat_id: string;
  telegram_pin_alerts: boolean;
  trash_retention_days: number;
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

/** Fired when any request comes back 401, so the shell can re-ask for the
 * token instead of leaving every page stuck on an error. */
export const UNAUTHORIZED_EVENT = "reeloom:unauthorized";

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: {
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        Authorization: `Bearer ${getToken()}`,
        ...init.headers,
      },
    });
  } catch {
    // fetch only rejects on network-level failures; the English TypeError
    // message would leak into the UI otherwise.
    throw new ApiError(0, "无法连接服务器");
  }
  if (response.status === 401) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
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
  resolveReplace: (id: string, action: ReplaceAction) =>
    post<{ state: string }>(`/runs/${id}/replace`, { action }),
  discard: (id: string) => post<{ state: string }>(`/runs/${id}/discard`),
  deleteRun: (id: string) =>
    request<{ deleted: boolean }>(`/runs/${id}`, { method: "DELETE" }),

  testLlm: () =>
    post<{ ok: boolean; reply?: string; error?: string }>(
      "/settings/test-llm",
    ),
  testTelegram: () =>
    post<{ ok: boolean; error?: string }>("/settings/test-telegram"),
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
  putSettings: (body: Record<string, string | boolean>) =>
    request<{ updated: boolean }>("/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};

/** Human wording for the run error codes an operator actually encounters.
 * Unlisted codes fall back to the raw code, same as before. */
const ERROR_LABEL: Record<string, string> = {
  replace_confirmation: "等待洗版确认",
  tmdb_not_found: "TMDB 未找到匹配条目",
  tmdb_unauthorized: "TMDB API Key 无效",
  tmdb_rate_limited: "TMDB 请求过于频繁",
  tmdb_unreachable: "无法连接 TMDB",
  tmdb_timeout: "TMDB 请求超时",
  tmdb_error: "TMDB 返回错误",
  missing_tmdb_key: "未配置 TMDB API Key",
  missing_model: "未配置模型",
  model_empty_response: "模型返回为空",
  config_deleted: "监控配置已删除",
  destination_collision: "目标位置已存在同名文件",
  unresolved_manual_groups: "洗版分组未确认",
  watch_root_missing: "监控目录不存在",
  unexpected: "意外错误",
};

export function errorLabel(error: Record<string, unknown>): string {
  const code = typeof error.code === "string" ? error.code : "";
  return ERROR_LABEL[code] ?? (code || "错误");
}

export const STATE_LABEL: Record<RunState, string> = {
  pending: "等待处理",
  identifying: "识别中",
  comparing: "对比版本",
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
  "comparing",
  "executing",
  "acquiring_subs",
  "reverting",
  "discarding",
];
