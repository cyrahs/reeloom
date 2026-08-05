export type StatusKind =
  | "run"
  | "phase"
  | "folder"
  | "interaction"
  | "archive"
  | "disposition"
  | "settlement"
  | "event"
  | "reason";

export type StatusTone = "neutral" | "success" | "warning" | "danger";

const labels: Record<StatusKind, Record<string, string>> = {
  run: {
    registered: "已登记",
    running: "进行中",
    needs_attention: "需要处理",
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
  /** Durable event vocabulary; unknown types keep their raw name. */
  event: {
    run_started: "运行开始",
    run_completed: "运行完成",
    run_failed: "运行失败",
    run_stopped: "运行停止",
    candidate_snapshot_created: "扫描完成",
    subtitle_variant_detected: "识别字幕版本",
    embedded_subtitles_inspected: "检查内嵌字幕",
    subtitle_search_observed: "记录字幕搜索",
    subtitle_search_failed: "字幕搜索失败",
    subtitle_selection_submitted: "提交字幕判断",
    tmdb_candidates_observed: "查询 TMDB",
    tmdb_season_catalog_observed: "读取季度目录",
    series_selected: "选定剧集",
    movie_selected: "选定电影",
    existing_inventory_observed: "读取媒体库现状",
    archive_search_observed: "检索历史归档",
    archive_directory_listed: "浏览历史目录",
    mapping_submitted: "提交映射",
    movie_mapping_submitted: "提交电影映射",
    mapping_rejected: "映射被拒绝",
    mapping_review_captured: "记录映射说明",
    plan_built: "生成计划",
    approval_requested: "请求审批",
    plan_approved: "计划已审批",
    apply_started: "开始执行",
    apply_completed: "执行完成",
    apply_failed: "执行失败",
    apply_rolled_back: "执行已回滚",
    move_applied: "文件已移动",
    move_rolled_back: "移动已回滚",
    rollback_started: "开始回滚",
    rollback_completed: "回滚完成",
    execution_settled: "执行已结算",
    folder_rename_started: "开始文件夹改名",
    folder_completed: "文件夹收尾完成",
    media_completed: "媒体处理完成",
    interaction_completed: "交互完成",
    model_usage_recorded: "记录模型用量",
    tool_requested: "请求工具调用",
    tool_rejected: "工具调用被拒绝",
  },
  /** Short forms of the failure codes; the full guidance lives in errorMessage. */
  reason: {
    atomic_move_unsupported: "挂载不支持原子移动",
    permission_denied: "权限不足",
    transient_io: "目录访问暂时失败",
    state_ambiguous: "结果无法确认",
    recovery_required: "需要恢复",
    interaction_budget_exhausted: "预算已耗尽",
  },
};

const tones: Record<StatusKind, Record<string, StatusTone>> = {
  run: {
    completed: "success",
    awaiting_approval: "warning",
    needs_attention: "warning",
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
  event: {},
  reason: {},
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
