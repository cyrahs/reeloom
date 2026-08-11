import { ACTIVE_STATES, STATE_LABEL, api, type RunSummary } from "../api";
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
  if (run.result.duplicates.length)
    parts.push(`重复 ${run.result.duplicates.length}`);
  if (run.result.missing.length) parts.push(`缺失 ${run.result.missing.length}`);
  if (run.result.archived) parts.push(`归档 ${run.result.archived}`);
  if (run.result.subtitles_acquired)
    parts.push(`字幕 ${run.result.subtitles_acquired}`);
  return parts.join(" · ");
}

export function RunsPage() {
  const { data, error, loading } = usePoll(() => api.listRuns(), 4000);

  if (loading && !data) return <p className="loading">载入中…</p>;
  if (error) return <p className="error">{error}</p>;

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
      {runs.length === 0 && <p className="empty">还没有任务。</p>}
      <ul className="runs">
        {runs.map((run) => (
          <li key={run.id} className={stateClass(run)}>
            <a href={`#/runs/${run.id}`}>
              <span className="folder">{run.folder_name}</span>
              <span className="title">
                {run.title ? `${run.title} (${run.year})` : "—"}
              </span>
              <span className="state">{STATE_LABEL[run.state]}</span>
              <span className="summary">{summary(run)}</span>
            </a>
          </li>
        ))}
      </ul>
    </>
  );
}
