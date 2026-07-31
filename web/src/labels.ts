export type StatusKind =
  | "run"
  | "phase"
  | "folder"
  | "interaction"
  | "archive"
  | "disposition"
  | "settlement";

export type StatusTone = "neutral" | "success" | "warning" | "danger";

const labels: Record<StatusKind, Record<string, string>> = {
  run: {
    registered: "已登记",
    running: "进行中",
    awaiting_approval: "等待审批",
    applying: "正在执行",
    completed: "已完成",
    failed: "失败",
    rolled_back: "已回滚",
    stopped: "已停止",
  },
  phase: {
    bootstrap: "初始化",
    identify_series: "识别剧集",
    map_episodes: "映射集数",
    identify_movie: "识别电影",
    map_movie: "映射电影",
    build_plan: "生成计划",
    awaiting_approval: "等待审批",
    applying: "正在执行",
    completed: "已完成",
    failed: "失败",
    rolled_back: "已回滚",
    stopped: "已停止",
  },
  folder: {
    settling: "稳定中",
    active: "处理中",
    blocked: "已阻断",
    settled: "已收尾",
  },
  interaction: {
    active: "进行中",
    completed: "已完成",
    failed: "失败",
  },
  archive: {
    checked: "浏览完整",
    incomplete: "浏览不完整",
  },
  disposition: {
    planned: "待执行",
    prepared: "已准备",
    renamed: "已改名",
    completed: "已完成",
    blocked: "已阻断",
    recovery_required: "需要恢复",
  },
  settlement: {
    completed: "已完成",
    rolled_back: "已回滚",
  },
};

const tones: Record<StatusKind, Record<string, StatusTone>> = {
  run: {
    completed: "success",
    awaiting_approval: "warning",
    rolled_back: "warning",
    failed: "danger",
  },
  phase: {
    completed: "success",
    awaiting_approval: "warning",
    rolled_back: "warning",
    failed: "danger",
  },
  folder: {
    settled: "success",
    settling: "warning",
    blocked: "danger",
  },
  interaction: {
    completed: "success",
    failed: "danger",
  },
  archive: {
    checked: "success",
    incomplete: "warning",
  },
  disposition: {
    completed: "success",
    planned: "warning",
    recovery_required: "danger",
    blocked: "danger",
  },
  settlement: {
    completed: "success",
    rolled_back: "warning",
  },
};

/** Untrusted server vocabulary falls back to the raw value, never to a guess. */
export function statusLabel(kind: StatusKind, value: string): string {
  return labels[kind][value] ?? value;
}

export function statusTone(kind: StatusKind, value: string): StatusTone {
  return tones[kind][value] ?? "neutral";
}

export function compactRunId(value: string): string {
  const characters = Array.from(value);
  if (characters.length <= 24) return value;
  return [
    ...characters.slice(0, 12),
    "…",
    ...characters.slice(-8),
  ].join("");
}

/** Keeps long opaque values (hashes, IDs) from dominating dense event rows. */
export function compactValue(value: string): string {
  const characters = Array.from(value);
  if (characters.length <= 32) return value;
  return [
    ...characters.slice(0, 10),
    "…",
    ...characters.slice(-6),
  ].join("");
}
