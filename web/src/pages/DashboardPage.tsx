import { useQuery } from "@tanstack/react-query";

import { ApiError } from "../api";
import { useAuth } from "../auth";
import { PageError, ShortHash, Status } from "../components/Status";
import { HashLink } from "../router";
import {
  configSchema,
  discoveriesSchema,
  foldersSchema,
  healthSchema,
  runsSchema,
} from "../schemas";
import { workTypeLabel } from "../workTypes";

export function DashboardPage() {
  const { api } = useAuth();
  const visible = document.visibilityState === "visible";
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.request("/health", healthSchema),
    refetchInterval: visible ? 10_000 : false,
  });
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => api.request("/api/v1/runs?limit=50", runsSchema),
    refetchInterval: visible ? 10_000 : false,
  });
  const discoveries = useQuery({
    queryKey: ["discoveries"],
    queryFn: () =>
      api.request("/api/v1/discoveries?limit=50", discoveriesSchema),
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
          value={String(runs.data?.items.length ?? "—")}
          detail="当前页最多 50 条"
        />
        <Metric
          label="等待审批"
          value={String(
            runs.data?.items.filter(
              (item) => item.status === "awaiting_approval",
            ).length ?? "—",
          )}
          detail="需要人工确认"
          accent
        />
        <Metric
          label="最新发现"
          value={String(discoveries.data?.items.length ?? "—")}
          detail="页面可见时每 10 秒刷新"
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
          <span className="muted">稳定中、活动与阻断状态</span>
        </div>
        {folders.error instanceof ApiError ? (
          <PageError code={folders.error.code} />
        ) : null}
        <div className="discovery-list">
          {folders.data?.items.map((item) => (
            <article key={`${item.watch_id}:${item.source_folder}`}>
              <div>
                <strong>{item.source_folder}</strong>
                <span>
                  {item.watch_id}
                  {item.reason_code ? ` · ${item.reason_code}` : ""}
                </span>
              </div>
              {item.run_id ? (
                <HashLink to={`/runs/${encodeURIComponent(item.run_id)}`}>
                  <Status value={item.status} />
                </HashLink>
              ) : (
                <Status value={item.status} />
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
        {runs.data?.items.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>运行</th>
                  <th>类型</th>
                  <th>入站文件夹</th>
                  <th>阶段</th>
                  <th>计划</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.items.map((run) => (
                  <tr key={run.run_id}>
                    <td>
                      <HashLink to={`/runs/${encodeURIComponent(run.run_id)}`}>
                        {run.run_id}
                      </HashLink>
                    </td>
                    <td>{workTypeLabel(run.work_type)}</td>
                    <td>{run.source_folder ?? "Legacy"}</td>
                    <td>{run.phase ?? "—"}</td>
                    <td><ShortHash value={run.plan_hash} /></td>
                    <td><Status value={run.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-inline">
            <p>还没有运行。</p>
            <span>配置完成后，稳定的媒体发现会出现在这里。</span>
          </div>
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
                <strong>{item.watch_id}</strong>
                <span>
                  {workTypeLabel(item.work_type)}
                  {item.source_folder ? ` · ${item.source_folder}` : " · Legacy"}
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
          {!discoveries.data?.items.length ? (
            <div className="empty-inline"><p>暂无发现。</p></div>
          ) : null}
        </div>
      </section>
    </main>
  );
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
