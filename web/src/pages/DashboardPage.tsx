import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { ApiError } from "../api";
import { useAuth } from "../auth";
import { Hint } from "../components/Hint";
import {
  RunBulkDeletionAction,
  RunDeletionAction,
} from "../components/RunDeletion";
import { PageError, ShortHash, Status } from "../components/Status";
import { compactRunId, compactValue, statusLabel } from "../labels";
import { HashLink } from "../router";
import {
  configSchema,
  discoveriesSchema,
  foldersSchema,
  healthSchema,
  runsSchema,
} from "../schemas";
import { workTypeLabel } from "../workTypes";

const PAGE_LIMIT = 50;

export function DashboardPage() {
  const { api } = useAuth();
  const visible = useDocumentVisible();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.request("/health", healthSchema),
    refetchInterval: visible ? 10_000 : false,
  });
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () =>
      api.request(`/api/v1/runs?limit=${PAGE_LIMIT}`, runsSchema),
    refetchInterval: visible ? 10_000 : false,
  });
  const discoveries = useQuery({
    queryKey: ["discoveries"],
    queryFn: () =>
      api.request(
        `/api/v1/discoveries?limit=${PAGE_LIMIT}`,
        discoveriesSchema,
      ),
    refetchInterval: visible ? 10_000 : false,
  });
  const folders = useQuery({
    queryKey: ["folders"],
    queryFn: () =>
      api.request("/api/v1/folders?limit=100", foldersSchema),
    refetchInterval: visible ? 10_000 : false,
  });
  const config = useQuery({
    queryKey: ["config"],
    queryFn: () => api.request("/api/v1/admin/config", configSchema),
    retry: false,
  });
  const noConfig =
    config.error instanceof ApiError && config.error.status === 404;
  const runItems = runs.data?.items ?? [];
  const truncated = runItems.length >= PAGE_LIMIT;
  const deletableIds = runItems
    .filter((run) => run.available_actions.includes("delete_run"))
    .map((run) => run.run_id);
  const [selected, setSelected] = useState<string[]>([]);
  /* A run may stop being deletable (or disappear) on any refresh; the
     selection must never outlive what the server still offers. */
  const deletableKey = deletableIds.join("\u0000");
  useEffect(() => {
    const allowed = new Set(deletableKey ? deletableKey.split("\u0000") : []);
    setSelected((prev) => {
      const next = prev.filter((id) => allowed.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [deletableKey]);
  const selectedSet = new Set(selected);
  const allSelected =
    deletableIds.length > 0 && selected.length === deletableIds.length;

  return (
    <main className="page">
      <section className="hero-row">
        <div className="hero-title">
          <h1>今天的整理进度，一眼看清。</h1>
          <Hint label="数据来源说明">
            页面只展示服务端持久化状态；浏览器不会推断执行成功。
          </Hint>
        </div>
        <div className="health-card" aria-label="服务健康状态">
          <span className={health.data ? "pulse online" : "pulse"} />
          <div>
            <strong>{health.data ? "服务正常" : "正在连接"}</strong>
            <small>
              {health.data
                ? `PostgreSQL ${health.data.postgres_major} · Schema ${health.data.schema_version}`
                : "等待健康检查"}
            </small>
            {health.data?.telegram_configured ? (
              <small>
                Telegram 队列 {health.data.notification_pending}
                {health.data.notification_dead
                  ? ` · 失败 ${health.data.notification_dead}`
                  : ""}
              </small>
            ) : null}
          </div>
        </div>
      </section>

      {noConfig ? (
        <section className="setup-banner">
          <div>
            <p className="eyebrow">首次设置</p>
            <h2>还没有活动配置</h2>
            <p>先定义监听目录、归档目标和模型凭据，后台才会开始发现。</p>
          </div>
          <HashLink className="primary button-link" to="/config">
            开始配置
          </HashLink>
        </section>
      ) : null}

      <section className="metric-grid" aria-label="关键指标">
        <Metric
          label="最近运行"
          value={
            runs.isSuccess
              ? `${runItems.length}${truncated ? "+" : ""}`
              : "—"
          }
          hint={
            truncated
              ? `只统计最新 ${PAGE_LIMIT} 条，服务端实际更多。`
              : "服务端现有的全部运行。页面可见时每 10 秒刷新。"
          }
        />
        <Metric
          label="等待审批"
          value={
            runs.isSuccess
              ? String(
                  runItems.filter(
                    (item) => item.status === "awaiting_approval",
                  ).length,
                )
              : "—"
          }
          hint="计划已生成，需要管理员确认后才会移动文件。"
          accent
        />
        <Metric
          label="最新发现"
          value={
            discoveries.isSuccess
              ? String(discoveries.data.items.length)
              : "—"
          }
          hint={`最新 ${PAGE_LIMIT} 条发现记录。`}
        />
      </section>

      {runs.error instanceof ApiError ? (
        <PageError code={runs.error.code} />
      ) : null}
      <section className="panel">
        <div className="panel-heading">
          <div className="heading-title">
            <h2>入站文件夹</h2>
            {folders.isSuccess ? (
              <span className="count-pill">{folders.data.items.length}</span>
            ) : null}
            <Hint label="入站文件夹说明">
              Reeloom 监听目录下正在观察或处理的子文件夹。稳定中表示还在等待写入结束，
              已阻断表示需要人工介入。
            </Hint>
          </div>
        </div>
        {folders.error instanceof ApiError ? (
          <PageError code={folders.error.code} />
        ) : null}
        <div className="discovery-list">
          {folders.data?.items.map((item) => (
            <article key={`${item.watch_id}:${item.source_folder}`}>
              <div>
                <strong title={item.source_folder}>
                  {item.source_folder}
                </strong>
                <span title={item.watch_id}>
                  {compactValue(item.watch_id)}
                  {item.reason_code
                    ? ` · ${statusLabel("reason", item.reason_code)}`
                    : ""}
                  {item.retry_count
                    ? ` · 重试 ${item.retry_count}/3`
                    : ""}
                </span>
              </div>
              {item.run_id ? (
                <HashLink to={`/runs/${encodeURIComponent(item.run_id)}`}>
                  <Status value={item.status} kind="folder" />
                </HashLink>
              ) : (
                <Status value={item.status} kind="folder" />
              )}
            </article>
          ))}
          {folders.isSuccess && !folders.data.items.length ? (
            <div className="empty-inline"><p>暂无入站文件夹。</p></div>
          ) : null}
        </div>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div className="heading-title">
            <h2>运行</h2>
            {runs.isSuccess ? (
              <span className="count-pill">
                {runItems.length}
                {truncated ? "+" : ""}
              </span>
            ) : null}
            <Hint label="运行列表说明">
              按创建时间倒序。删除记录只隐藏控制台里的这条运行，不会改动任何媒体文件。
              删除按钮点第一次变成确认，再点一次才执行。
            </Hint>
          </div>
          {selected.length ? (
            <div className="selection-actions">
              <span className="muted">已选 {selected.length} 条</span>
              <button className="ghost compact" onClick={() => setSelected([])}>
                取消选择
              </button>
              <RunBulkDeletionAction
                runIds={selected}
                onDeleted={(deleted) =>
                  setSelected((prev) =>
                    prev.filter((id) => !deleted.includes(id)),
                  )
                }
              />
            </div>
          ) : null}
        </div>
        {runItems.length ? (
          <div className="table-wrap">
            <table className="runs-table">
              <thead>
                <tr>
                  <th className="select-cell">
                    <input
                      type="checkbox"
                      aria-label="全选可删除的运行"
                      disabled={!deletableIds.length}
                      checked={allSelected}
                      ref={(node) => {
                        if (node) {
                          node.indeterminate =
                            selected.length > 0 && !allSelected;
                        }
                      }}
                      onChange={(event) =>
                        setSelected(event.target.checked ? deletableIds : [])
                      }
                    />
                  </th>
                  <th>运行</th>
                  <th>类型</th>
                  <th>入站文件夹</th>
                  <th>阶段</th>
                  <th>计划</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {runItems.map((run) => {
                  const deletable =
                    run.available_actions.includes("delete_run");
                  return (
                  <tr key={run.run_id}>
                    <td data-label="选择" className="select-cell">
                      {deletable ? (
                        <input
                          type="checkbox"
                          aria-label={`选择运行 ${run.run_id}`}
                          checked={selectedSet.has(run.run_id)}
                          onChange={(event) =>
                            setSelected((prev) =>
                              event.target.checked
                                ? [...prev, run.run_id]
                                : prev.filter((id) => id !== run.run_id),
                            )
                          }
                        />
                      ) : null}
                    </td>
                    <td data-label="运行" className="run-cell">
                      <HashLink
                        to={`/runs/${encodeURIComponent(run.run_id)}`}
                        title={run.run_id}
                      >
                        {compactRunId(run.run_id)}
                      </HashLink>
                    </td>
                    <td data-label="类型" className="nowrap">
                      {workTypeLabel(run.work_type)}
                    </td>
                    <td data-label="入站文件夹" className="folder-cell">
                      <span title={run.source_folder ?? undefined}>
                        {run.source_folder ?? "无（旧版运行）"}
                      </span>
                    </td>
                    <td data-label="阶段" className="nowrap">
                      {run.phase ? statusLabel("phase", run.phase) : "—"}
                    </td>
                    <td data-label="计划">
                      <ShortHash value={run.plan_hash} />
                    </td>
                    <td data-label="状态" className="nowrap">
                      <Status value={run.status} />
                    </td>
                    <td data-label="操作" className="row-actions">
                      {deletable ? (
                        <RunDeletionAction
                          runId={run.run_id}
                          className="danger-outline compact"
                        />
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : runs.isSuccess ? (
          <div className="empty-inline">
            <p>还没有运行。</p>
            <span>配置完成后，稳定的媒体发现会出现在这里。</span>
          </div>
        ) : (
          <div className="empty-inline"><p>正在读取运行…</p></div>
        )}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div className="heading-title">
            <h2>发现</h2>
            {discoveries.isSuccess ? (
              <span className="count-pill">
                {discoveries.data.items.length}
              </span>
            ) : null}
            <Hint label="发现说明">
              后台扫描到的稳定文件夹。等待调度表示尚未创建运行。
            </Hint>
          </div>
        </div>
        <div className="discovery-list">
          {discoveries.data?.items.map((item) => (
            <article key={item.discovery_id}>
              <div>
                <strong title={item.source_folder ?? undefined}>
                  {item.source_folder ?? "无（旧版发现）"}
                </strong>
                <span title={item.watch_id}>
                  {workTypeLabel(item.work_type)}
                  {" · "}
                  {compactValue(item.watch_id)}
                  {" · "}
                  {formatTime(item.discovered_at)}
                </span>
              </div>
              {item.run_id ? (
                <HashLink to={`/runs/${encodeURIComponent(item.run_id)}`}>
                  查看运行
                </HashLink>
              ) : (
                <span className="muted">等待调度</span>
              )}
            </article>
          ))}
          {discoveries.isSuccess && !discoveries.data.items.length ? (
            <div className="empty-inline"><p>暂无发现。</p></div>
          ) : null}
        </div>
      </section>
    </main>
  );
}

/** Polling must follow real tab visibility, not the value at first render. */
function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(
    () => document.visibilityState === "visible",
  );
  useEffect(() => {
    const update = () =>
      setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", update);
    update();
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

function Metric({
  label,
  value,
  hint,
  accent = false,
}: {
  label: string;
  value: string;
  hint: string;
  accent?: boolean;
}) {
  return (
    <article className={accent ? "metric accent" : "metric"}>
      <span>
        {label}
        <Hint label={`${label}说明`}>{hint}</Hint>
      </span>
      <strong>{value}</strong>
    </article>
  );
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}
