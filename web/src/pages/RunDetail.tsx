import { useState } from "react";

import { ACTIVE_STATES, STATE_LABEL, api, type RunDetail } from "../api";
import { usePoll } from "../usePoll";

function archivedSummary(run: RunDetail): string[] {
  if (!run.plan) return [];
  const unmapped = new Set(run.plan.unmapped);
  // A first-level subfolder whose files are all unmapped is archived whole,
  // so it collapses to "folder/"; partial folders list their leftover files.
  const folders = new Map<string, { total: number; unmapped: string[] }>();
  const looseFiles: string[] = [];
  for (const item of run.snapshot) {
    const slash = item.relative_path.indexOf("/");
    if (slash === -1) {
      if (unmapped.has(item.candidate_id)) looseFiles.push(item.relative_path);
      continue;
    }
    const folder = item.relative_path.slice(0, slash);
    const group = folders.get(folder) ?? { total: 0, unmapped: [] };
    group.total += 1;
    if (unmapped.has(item.candidate_id)) group.unmapped.push(item.relative_path);
    folders.set(folder, group);
  }
  const shown: string[] = [];
  for (const [folder, group] of folders) {
    if (group.unmapped.length === 0) continue;
    if (group.unmapped.length === group.total) shown.push(`${folder}/`);
    else shown.push(...group.unmapped);
  }
  return [...shown, ...looseFiles];
}

function Moves({ run }: { run: RunDetail }) {
  if (!run.plan) return null;
  const archived = archivedSummary(run);
  // Acquired subtitles are planned only after execution, so their renames
  // live in the executed ledger rather than in plan.moves.
  const acquired = run.executed_moves.filter(
    (item) => item.move.kind === "acquired_subtitle" && item.outcome === "moved",
  );
  return (
    <section>
      <h2>计划</h2>
      <p className="identity">
        {run.plan.identity.title} ({run.plan.identity.year}) · tmdb-
        {run.plan.identity.tmdb_id}
      </p>
      {run.plan.notes && <p className="notes">{run.plan.notes}</p>}
      <ul className="moves">
        {run.plan.moves.map((move, index) => (
          <li key={index}>
            <code className="from">{move.source_path}</code>
            <code className="to">{move.dest_path}</code>
            {move.kind !== "media" && <span className="tag">{move.kind}</span>}
          </li>
        ))}
        {acquired.map((item, index) => (
          <li key={`acquired-${index}`}>
            <code className="from">
              {item.move.source_path.split("/").pop()}
            </code>
            <code className="to">{item.move.dest_path}</code>
            <span className="tag">字幕</span>
          </li>
        ))}
      </ul>
      {archived.length > 0 && (
        <p className="unmapped">未映射（将归档）：{archived.join("、")}</p>
      )}
    </section>
  );
}

function Result({ run }: { run: RunDetail }) {
  if (!run.result) return null;
  const { duplicates, missing, subtitle_note: note } = run.result;
  return (
    <section>
      <h2>结果</h2>
      <p>
        移动 {run.result.moved} · 归档 {run.result.archived} · 字幕{" "}
        {run.result.subtitles_acquired}
      </p>
      {duplicates.length > 0 && (
        <p className="warn-text">重复（已放入 fail）：{duplicates.join(", ")}</p>
      )}
      {missing.length > 0 && (
        <p className="warn-text">缺失：{missing.join(", ")}</p>
      )}
      {note && <p className="warn-text">字幕：{note}</p>}
    </section>
  );
}

export function RunDetailPage({ runId }: { runId: string }) {
  const { data: run, error, refresh } = usePoll(
    () => api.getRun(runId),
    4000,
    [runId],
  );
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setActionError("");
    try {
      await action();
      refresh();
    } catch (thrown) {
      setActionError((thrown as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (error) return <p className="error">{error}</p>;
  if (!run) return <p className="loading">载入中…</p>;

  const active = ACTIVE_STATES.includes(run.state);

  return (
    <>
      <p className="crumb">
        <a href="#/">← 任务</a>
      </p>
      <div className="run-head">
        <h1>{run.folder_name}</h1>
        <span
          className={`badge ${
            run.state === "needs_attention" || run.state === "failed"
              ? "warn"
              : active
                ? "busy"
                : "ok"
          }`}
        >
          {STATE_LABEL[run.state]}
        </span>
      </div>
      {run.error && (
        <p className="error">
          {typeof run.error.code === "string" ? run.error.code : "错误"}
          {typeof run.error.detail === "string" && ` · ${run.error.detail}`}
        </p>
      )}

      <Moves run={run} />
      <Result run={run} />

      <section>
        <h2>交流</h2>
        <p className="muted">
          记录始终保留，并在提问和重新识别（重试 / 修订重做）时完整提供给
          Agent 作为上下文。
        </p>
        <div className="chat-log">
          {run.interactions.map((item, index) => (
            <p
              key={index}
              className={`chat ${item.role === "agent" ? "agent" : "user"}`}
            >
              <strong>{item.role === "agent" ? "Agent" : "你"}</strong>
              {item.role === "revision" && <span className="tag">修订</span>}
              {item.content}
            </p>
          ))}
        </div>
        <textarea
          value={message}
          placeholder="提问，或说明该怎么改"
          onChange={(event) => setMessage(event.target.value)}
        />
        <div className="actions">
          <button
            disabled={busy || !message.trim()}
            onClick={() =>
              act(async () => {
                await api.ask(runId, message);
                setMessage("");
              })
            }
          >
            提问
          </button>
          <button
            className="primary"
            disabled={busy || active || !message.trim()}
            title={active ? "任务进行中" : "复原已移动的文件并按新计划重做"}
            onClick={() =>
              act(async () => {
                await api.revise(runId, message);
                setMessage("");
              })
            }
          >
            按此修订并重做
          </button>
        </div>
      </section>

      <section>
        <h2>文件</h2>
        <ul className="files">
          {run.snapshot.map((item) => (
            <li key={item.candidate_id}>
              <code>{item.candidate_id}</code> {item.relative_path}
              {item.variant && <span className="tag">{item.variant}</span>}
            </li>
          ))}
        </ul>
      </section>

      <section className="danger">
        <h2>其他操作</h2>
        <div className="actions">
          <button disabled={busy || active} onClick={() => act(() => api.retry(runId))}>
            重试
          </button>
          <button
            className="danger"
            disabled={busy || active}
            title="复原已入库的文件，把原始文件夹整体移入 fail；自动下载的字幕会被删除"
            onClick={() => act(() => api.discard(runId))}
          >
            放弃
          </button>
          <button
            className="danger"
            disabled={busy || active}
            onClick={() =>
              act(async () => {
                await api.deleteRun(runId);
                window.location.hash = "#/";
              })
            }
          >
            删除记录
          </button>
        </div>
        {actionError && <p className="error">{actionError}</p>}
      </section>

      <section>
        <h2>日志</h2>
        <ul className="logs">
          {run.logs.map((item, index) => (
            <li key={index} className={item.level}>
              <span className="ts">
                {new Date(item.ts).toLocaleTimeString("zh-CN", {
                  hour12: false,
                })}
              </span>
              {item.message}
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
