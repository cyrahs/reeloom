import { useState } from "react";

import {
  ACTIVE_STATES,
  STATE_LABEL,
  api,
  type EpisodeSpan,
  type ReplaceAction,
  type ReplaceGroup,
  type RunDetail,
} from "../api";
import { Markdown } from "../Markdown";
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

type SnapshotItem = RunDetail["snapshot"][number];

interface DirNode {
  dirs: Map<string, DirNode>;
  files: SnapshotItem[];
}

function buildTree(items: SnapshotItem[]): DirNode {
  const root: DirNode = { dirs: new Map(), files: [] };
  for (const item of items) {
    const parts = item.relative_path.split("/");
    let node = root;
    for (const part of parts.slice(0, -1)) {
      let child = node.dirs.get(part);
      if (!child) {
        child = { dirs: new Map(), files: [] };
        node.dirs.set(part, child);
      }
      node = child;
    }
    node.files.push(item);
  }
  return root;
}

function dirStats(node: DirNode): { count: number; bytes: number } {
  let count = node.files.length;
  let bytes = 0;
  for (const file of node.files) bytes += file.size_bytes;
  for (const child of node.dirs.values()) {
    const sub = dirStats(child);
    count += sub.count;
    bytes += sub.bytes;
  }
  return { count, bytes };
}

const SIZE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

function formatSize(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1;
  return `${value.toFixed(digits)} ${SIZE_UNITS[unit]}`;
}

function naturalCompare(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true });
}

function FileRow({ item }: { item: SnapshotItem }) {
  const name = item.relative_path.split("/").at(-1) ?? item.relative_path;
  return (
    <li className="tree-file">
      <code className="cid">{item.candidate_id}</code>
      <span className="name">{name}</span>
      {item.variant && <span className="tag">{item.variant}</span>}
      <span className="size">{formatSize(item.size_bytes)}</span>
    </li>
  );
}

function TreeLevel({ node }: { node: DirNode }) {
  const dirs = [...node.dirs.entries()].sort(([a], [b]) =>
    naturalCompare(a, b),
  );
  const files = [...node.files].sort((a, b) =>
    naturalCompare(a.relative_path, b.relative_path),
  );
  return (
    <ul className="tree">
      {dirs.map(([name, child]) => {
        const stats = dirStats(child);
        return (
          <li key={name}>
            <details className="tree-dir">
              <summary>
                <span className="name">{name}/</span>
                <span className="size">
                  {stats.count} 个文件 · {formatSize(stats.bytes)}
                </span>
              </summary>
              <TreeLevel node={child} />
            </details>
          </li>
        );
      })}
      {files.map((item) => (
        <FileRow key={item.candidate_id} item={item} />
      ))}
    </ul>
  );
}

function Files({ run }: { run: RunDetail }) {
  const total = run.snapshot.reduce((sum, item) => sum + item.size_bytes, 0);
  return (
    <section>
      <h2>文件</h2>
      {run.snapshot.length === 0 ? (
        <p className="muted">无</p>
      ) : (
        <>
          <p className="files-total">
            共 {run.snapshot.length} 个文件 · {formatSize(total)}
          </p>
          <div className="files">
            <TreeLevel node={buildTree(run.snapshot)} />
          </div>
        </>
      )}
    </section>
  );
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
            <span className="tag">下载字幕</span>
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
  const { duplicates, missing, replaced, discarded, subtitle_note: note } =
    run.result;
  return (
    <section>
      <h2>结果</h2>
      <p>
        移动 {run.result.moved} · 归档 {run.result.archived} · 字幕{" "}
        {run.result.subtitles_moved} · 下载字幕 {run.result.subtitles_acquired}
      </p>
      {replaced.length > 0 && (
        <p className="warn-text">
          洗版替换（旧版已入回收区）：{replaced.join(", ")}
        </p>
      )}
      {discarded.length > 0 && (
        <p className="warn-text">重复（已入回收区）：{discarded.join(", ")}</p>
      )}
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

const VERDICT_LABEL: Record<ReplaceGroup["verdict"], string> = {
  import: "全新入库",
  replace: "洗版替换",
  discard: "丢弃新下载",
  manual: "需人工确认",
};

const QUALITY_LABEL: Record<ReplaceGroup["quality"], string> = {
  better: "画质更好",
  same: "画质相近",
  worse: "画质更差",
  unknown: "画质未知",
};

function spanLabel(span: EpisodeSpan | null): string {
  if (!span) return "正片";
  const head = `S${String(span.season).padStart(2, "0")}E${String(
    span.episode_start,
  ).padStart(2, "0")}`;
  return span.episode_end !== span.episode_start
    ? `${head}-E${String(span.episode_end).padStart(2, "0")}`
    : head;
}

function GroupRow({ group }: { group: ReplaceGroup }) {
  const title =
    group.season === null ? "电影" : `第 ${group.season} 季`;
  return (
    <details className="replace-group">
      <summary>
        <strong>{title}</strong>
        <span className={`tag ${group.verdict}`}>
          {VERDICT_LABEL[group.verdict]}
        </span>
        {group.ratio !== null && <span>体积比 {group.ratio.toFixed(2)}</span>}
        <span className="muted">{QUALITY_LABEL[group.quality]}</span>
        {group.new_episodes.length > 0 && (
          <span className="muted">新增 {group.new_episodes.length} 集</span>
        )}
      </summary>
      <ul className="replace-episodes">
        {group.overlap.map((item) => (
          <li key={item.candidate_id}>
            <span className="name">{spanLabel(item.span)}</span>
            <span>
              新 {formatSize(item.incoming_bytes)} ↔ 旧{" "}
              {formatSize(item.existing_bytes)}
            </span>
            <span className="muted">
              {item.existing
                .map((file) =>
                  file.extra_base
                    ? `${file.extra_base}/${file.relative_path}`
                    : file.relative_path,
                )
                .join("；")}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

function ReplacePanel({
  run,
  busy,
  onResolve,
}: {
  run: RunDetail;
  busy: boolean;
  onResolve: (action: ReplaceAction) => void;
}) {
  const decision = run.replace_decision;
  if (!decision || decision.groups.length === 0) return null;
  const awaiting =
    run.state === "needs_attention" &&
    run.error?.code === "replace_confirmation";
  return (
    <section>
      <h2>洗版</h2>
      {decision.groups.map((group, index) => (
        <GroupRow key={index} group={group} />
      ))}
      {decision.resolution && (
        <p className="muted">已选择：{decision.resolution}</p>
      )}
      {awaiting && (
        <div className="actions">
          <button
            className="primary"
            disabled={busy}
            title="旧版本移入回收区，保留期后删除；新版本入库"
            onClick={() => onResolve("replace")}
          >
            确认替换旧版
          </button>
          <button
            disabled={busy}
            title="保留现有版本，新下载的重叠文件移入回收区"
            onClick={() => onResolve("discard_incoming")}
          >
            丢弃新下载
          </button>
          <button
            disabled={busy}
            title="不做洗版：与库内冲突的文件会进入 fail 目录"
            onClick={() => onResolve("keep_both")}
          >
            两版共存
          </button>
        </div>
      )}
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

      <ReplacePanel
        run={run}
        busy={busy}
        onResolve={(action) => act(() => api.resolveReplace(runId, action))}
      />
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
            <div
              key={index}
              className={`chat ${item.role === "agent" ? "agent" : "user"}`}
            >
              <strong>{item.role === "agent" ? "Agent" : "你"}</strong>
              {item.role === "revision" && <span className="tag">修订</span>}
              <Markdown text={item.content} />
            </div>
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

      <Files run={run} />

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
