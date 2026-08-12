import { useEffect, useState } from "react";

import {
  ACTIVE_STATES,
  STATE_LABEL,
  api,
  type IntakeFolder,
  type RunSummary,
} from "../api";
import { usePoll } from "../usePoll";

function stateClass(run: RunSummary): string {
  if (run.state === "needs_attention" || run.state === "failed") return "warn";
  if (ACTIVE_STATES.includes(run.state)) return "busy";
  return "ok";
}

function summary(run: RunSummary): string {
  if (run.error) {
    const code = (run.error as { code?: string }).code ?? "error";
    return code;
  }
  if (!run.result) return `${run.file_count} 个文件`;
  const parts = [`移动 ${run.result.moved}`];
  if (run.result.replaced.length)
    parts.push(`洗版 ${run.result.replaced.length}`);
  // Discarded incoming files are duplicates of what the library already
  // has, so they count as 重复 alongside hash duplicates (same as detail).
  const duplicates =
    run.result.discarded.length + run.result.duplicates.length;
  if (duplicates) parts.push(`重复 ${duplicates}`);
  if (run.result.missing.length) parts.push(`缺失 ${run.result.missing.length}`);
  if (run.result.archived) parts.push(`归档 ${run.result.archived}`);
  if (run.result.subtitles_moved)
    parts.push(`字幕 ${run.result.subtitles_moved}`);
  if (run.result.subtitles_acquired)
    parts.push(`下载字幕 ${run.result.subtitles_acquired}`);
  if (run.result.subtitles_embedded)
    parts.push(`内封 ${run.result.subtitles_embedded}`);
  return parts.join(" · ");
}

const SKIP_REASON: Record<string, string> = {
  no_video: "没有视频文件",
  unchanged: "内容与上次任务相同",
};

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.ceil(seconds));
  const minutes = Math.floor((total % 3600) / 60);
  const rest = `${String(minutes).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  return total >= 3600 ? `${Math.floor(total / 3600)}:${rest}` : rest;
}

function intakeBadge(folder: IntakeFolder): { label: string; cls: string } {
  if (folder.status === "settling") return { label: "静置中", cls: "busy" };
  if (folder.status === "empty") return { label: "等待文件", cls: "" };
  return { label: "已跳过", cls: "" };
}

function IntakeRow({
  folder,
  deadlineMs,
  nowMs,
}: {
  folder: IntakeFolder;
  deadlineMs: number | null;
  nowMs: number;
}) {
  const badge = intakeBadge(folder);
  let detail: React.ReactNode;
  if (folder.status === "settling" && deadlineMs !== null) {
    const remaining = (deadlineMs - nowMs) / 1000;
    detail =
      remaining > 0 ? (
        <>
          <span className="countdown">{formatCountdown(remaining)}</span>{" "}
          后开始处理
        </>
      ) : (
        "即将开始处理"
      );
  } else if (folder.status === "empty") {
    detail = "文件夹为空，等待文件写入";
  } else {
    detail = SKIP_REASON[folder.reason ?? ""] ?? folder.reason ?? "已跳过";
  }

  return (
    <li>
      <div className="intake-row">
        <span className="run-main">
          <span className="folder">{folder.folder_name}</span>
          <span className="title">
            {folder.config_name} · {folder.file_count} 个文件 ·{" "}
            {formatBytes(folder.total_bytes)}
          </span>
        </span>
        <span className="run-side">
          <span className={`badge ${badge.cls}`}>{badge.label}</span>
          <span className="summary">{detail}</span>
        </span>
      </div>
    </li>
  );
}

export function RunsPage() {
  const { data, error, loading } = usePoll(() => api.listRuns(), 4000);
  // Polled separately so an older server without /intake still shows runs.
  const intake = usePoll(async () => {
    const report = await api.listIntake();
    return { ...report, fetchedAt: Date.now() };
  }, 4000);

  const folders = intake.data?.folders ?? [];
  const hasCountdown = folders.some((folder) => folder.status === "settling");
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!hasCountdown) return;
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [hasCountdown]);

  if (loading && !data) return <p className="loading">载入中…</p>;
  if (error) return <p className="error">{error}</p>;

  // remaining_seconds was measured at scanned_at on the server clock; shift
  // it to now (also server clock), then anchor the deadline to the local
  // clock at fetch time so skew between the two never distorts it.
  const deadline = (folder: IntakeFolder): number | null => {
    if (folder.remaining_seconds === null || !intake.data) return null;
    const stale = intake.data.now - folder.scanned_at;
    return (
      intake.data.fetchedAt + (folder.remaining_seconds - stale) * 1000
    );
  };

  const runs = data?.runs ?? [];
  const attention = runs.filter(
    (run) => run.state === "needs_attention" || run.state === "failed",
  );

  return (
    <>
      <h1>任务</h1>
      {attention.length > 0 && (
        <p className="banner">{attention.length} 个任务需要处理</p>
      )}
      {folders.length > 0 && (
        <section className="intake">
          <h2>静置文件夹</h2>
          <ul className="runs">
            {folders.map((folder) => (
              <IntakeRow
                key={`${folder.config_id}/${folder.folder_name}`}
                folder={folder}
                deadlineMs={deadline(folder)}
                nowMs={Math.max(nowMs, intake.data?.fetchedAt ?? nowMs)}
              />
            ))}
          </ul>
        </section>
      )}
      {runs.length === 0 && folders.length === 0 && (
        <p className="empty">还没有任务。</p>
      )}
      <ul className="runs">
        {runs.map((run) => (
          <li key={run.id}>
            <a href={`#/runs/${run.id}`}>
              <span className="run-main">
                <span className="folder">
                  {run.title ? `${run.title} (${run.year})` : run.folder_name}
                </span>
                {run.title && (
                  <span className="title">{run.folder_name}</span>
                )}
              </span>
              <span className="run-side">
                <span className={`badge ${stateClass(run)}`}>
                  {STATE_LABEL[run.state]}
                </span>
                <span className="summary">{summary(run)}</span>
              </span>
            </a>
          </li>
        ))}
      </ul>
    </>
  );
}
