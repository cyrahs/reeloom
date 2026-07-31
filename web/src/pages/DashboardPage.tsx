import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { ApiError } from "../api";
import { useAuth } from "../auth";
import { RunDeletionAction } from "../components/RunDeletion";
import { PageError, ShortHash, Status } from "../components/Status";
import { compactRunId, statusLabel } from "../labels";
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

  return (
    <main className="page">
      <section className="hero-row">
        <div>
          <p className="eyebrow">OPERATIONS OVERVIEW</p>
          <h1>今天的整理进度，一眼看清。</h1>
          <p className="lede">
            页面只展示服务端持久化状态；浏览器不会推断执行成功。
          </p>
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
          detail={
            truncated
              ? `仅统计最新 ${PAGE_LIMIT} 条，实际更多`
              : "服务端现有全部运行"
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
          detail={
            truncated
              ? `最新 ${PAGE_LIMIT} 条中需人工确认`
              : "需要人工确认"
          }
          accent
        />
        <Metric
          label="最新发现"
          value={
            discoveries.isSuccess
              ? String(discoveries.data.items.length)
              : "—"
          }
          detail={`最新 ${PAGE_LIMIT} 条 · 页面可见时每 10 秒刷新`}
        />
      </section>

      {runs.error instanceof ApiError ? (
        <PageError code={runs.error.code} />
      ) : null}
      <section className="panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">INBOUND FOLDERS</p>
            <h2>入站文件夹</h2>
          </div>
          <span className="muted">稳定中、处理中与阻断状态</span>
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
                <span>
                  {item.watch_id}
                  {item.reason_code ? ` · ${item.reason_code}` : ""}
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
          <div>
            <p className="eyebrow">RUNS</p>
            <h2>运行</h2>
          </div>
          <span className="muted">按创建时间倒序</span>
        </div>
        {runItems.length ? (
          <div className="table-wrap">
            <table className="runs-table">
              <thead>
                <tr>
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
                {runItems.map((run) => (
                  <tr key={run.run_id}>
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
                      {run.available_actions.includes("delete_run") ? (
                        <RunDeletionAction
                          runId={run.run_id}
                          className="danger-outline compact"
                        />
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
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
          <div>
            <p className="eyebrow">DISCOVERIES</p>
            <h2>发现</h2>
          </div>
        </div>
        <div className="discovery-list">
          {discoveries.data?.items.map((item) => (
            <article key={item.discovery_id}>
              <div>
                <strong title={item.source_folder ?? undefined}>
                  {item.source_folder ?? "无（旧版发现）"}
                </strong>
                <span>
                  {workTypeLabel(item.work_type)}
                  {" · "}
                  {item.watch_id}
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
  detail,
  accent = false,
}: {
  label: string;
  value: string;
  detail: string;
  accent?: boolean;
}) {
  return (
    <article className={accent ? "metric accent" : "metric"}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
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
