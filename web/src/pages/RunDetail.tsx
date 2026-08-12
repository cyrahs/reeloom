import { useState } from "react";

import {
  ACTIVE_STATES,
  STATE_LABEL,
  api,
  type EpisodeSpan,
  type Move,
  type ReplaceAction,
  type ReplaceGroup,
  type RunDetail,
} from "../api";
import { Markdown } from "../Markdown";
import { usePoll } from "../usePoll";

function archivedSummary(run: RunDetail): string[] {
  if (!run.plan || run.snapshot.length === 0) return [];
  const unmapped = new Set(run.plan.unmapped);
  // A subfolder whose files are all unmapped is archived whole, so it
  // collapses to "folder/" at the shallowest level that covers it; a fully
  // unmapped run collapses to the intake folder itself.
  const shown: string[] = [];
  if (collapseUnmapped(buildTree(run.snapshot), "", unmapped, shown)) {
    return [`${run.folder_name}/`];
  }
  return shown;
}

// Appends this subtree's unmapped listing to `out` and reports whether every
// file below `node` is unmapped, in which case the caller discards the
// listing and shows the folder itself instead.
function collapseUnmapped(
  node: DirNode,
  prefix: string,
  unmapped: Set<string>,
  out: string[],
): boolean {
  let all = true;
  for (const [name, child] of node.dirs) {
    const lines: string[] = [];
    if (collapseUnmapped(child, `${prefix}${name}/`, unmapped, lines)) {
      out.push(`${prefix}${name}/`);
    } else {
      out.push(...lines);
      all = false;
    }
  }
  for (const file of node.files) {
    if (unmapped.has(file.candidate_id)) out.push(file.relative_path);
    else all = false;
  }
  return all;
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

const TRASH_KIND_LABEL: Record<string, string> = {
  trash_replaced: "洗版替换",
  trash_duplicate: "重复",
};

interface TrashGroup {
  kind: string;
  dir: string;
  moves: Move[];
}

// A trash move keeps its relative path under the trash directory, so the
// per-file destination is noise; show each trash directory once with the
// files that went in.
function groupTrashMoves(moves: Move[]): TrashGroup[] {
  const groups = new Map<string, TrashGroup>();
  for (const move of moves) {
    if (!(move.kind in TRASH_KIND_LABEL)) continue;
    const dir = move.dest_path.slice(
      0,
      move.dest_path.length - move.source_path.length,
    );
    const key = `${move.kind}|${dir}`;
    let group = groups.get(key);
    if (!group) {
      group = { kind: move.kind, dir, moves: [] };
      groups.set(key, group);
    }
    group.moves.push(move);
  }
  return [...groups.values()];
}

function Moves({ run }: { run: RunDetail }) {
  if (!run.plan) return null;
  const archived = archivedSummary(run);
  const planMoves = run.plan.moves.filter(
    (move) => !(move.kind in TRASH_KIND_LABEL),
  );
  const trash = groupTrashMoves(run.plan.moves);
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
        {planMoves.map((move, index) => (
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
      {trash.map((group) => (
        <div className="trash-moves" key={`${group.kind}|${group.dir}`}>
          <p className="trash-dest">
            {TRASH_KIND_LABEL[group.kind]}（{group.moves.length} 个）移入{" "}
            <code>{group.dir}</code>
          </p>
          <ul className="trash-files">
            {group.moves.map((move, index) => (
              <li key={index}>
                <code>{move.source_path}</code>
              </li>
            ))}
          </ul>
        </div>
      ))}
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
  // The plan section already names every replaced and duplicate file, so the
  // summary line carries only their counts.
  const summary = [
    `移动 ${run.result.moved}`,
    `归档 ${run.result.archived}`,
    `字幕 ${run.result.subtitles_moved}`,
    `下载字幕 ${run.result.subtitles_acquired}`,
  ];
  if (replaced.length > 0) summary.push(`洗版 ${replaced.length}`);
  const duplicateCount = discarded.length + duplicates.length;
  if (duplicateCount > 0) summary.push(`重复 ${duplicateCount}`);
  return (
    <section>
      <h2>结果</h2>
      <p>{summary.join(" · ")}</p>
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
      <p className="muted">
        被淘汰的文件会移回监控目录下的 .reeloom-trash
        回收区，保留期满后自动删除。
      </p>
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
