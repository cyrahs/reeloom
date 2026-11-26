import { useEffect, useState } from "react";

import {
  api,
  type Grab,
  type GrabState,
  type ReleasePreview,
  type ReleaseVerdict,
  type Subscription,
  type SubscriptionInput,
  type TmdbHit,
  type WatchConfig,
} from "../api";
import { Link } from "../router";
import { formatWhen } from "../time";
import { usePoll } from "../usePoll";

const GRAB_LABEL: Record<GrabState, string> = {
  downloading: "下载中",
  completed: "待归档",
  imported: "已归档",
  failed: "失败",
};

const VERDICT_LABEL: Record<ReleaseVerdict, string> = {
  grab: "会抓取",
  group: "组不符",
  include: "未含关键词",
  exclude: "含排除词",
  season: "季不符",
  grabbed: "已抓过",
  covered: "集已有",
  too_old: "订阅前发布",
};

function grabClass(state: GrabState): string {
  if (state === "failed") return "warn";
  if (state === "imported") return "ok";
  return "busy";
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}

function episodesLabel(season: number | null, episodes: number[] | null) {
  if (season === null) return "";
  const s = `S${String(season).padStart(2, "0")}`;
  if (episodes === null) return `${s} 整季`;
  if (episodes.length === 1) return `${s}E${String(episodes[0]).padStart(2, "0")}`;
  return `${s}E${String(episodes[0]).padStart(2, "0")}-E${String(
    episodes[episodes.length - 1],
  ).padStart(2, "0")}`;
}

interface FormState {
  name: string;
  config_id: string;
  tmdb_id: string;
  query: string;
  season: string;
  groups: string;
  include: string;
  exclude: string;
  notes: string;
  backfill: boolean;
}

function toInput(form: FormState): SubscriptionInput {
  return {
    name: form.name.trim() || undefined,
    config_id: form.config_id,
    tmdb_id: Number(form.tmdb_id),
    query: form.query.trim(),
    season: form.season.trim() === "" ? null : Number(form.season),
    groups: form.groups
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean),
    include: form.include,
    exclude: form.exclude,
    notes: form.notes,
    backfill: form.backfill,
  };
}

function fromSubscription(subscription: Subscription): FormState {
  return {
    name: subscription.name,
    config_id: subscription.config_id,
    tmdb_id: String(subscription.tmdb_id),
    query: subscription.query,
    season: subscription.season === null ? "" : String(subscription.season),
    groups: subscription.groups.join(", "),
    include: subscription.include,
    exclude: subscription.exclude,
    notes: subscription.notes,
    backfill: subscription.ignore_before === null,
  };
}

function TmdbPicker({ onPick }: { onPick: (hit: TmdbHit) => void }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<TmdbHit[]>([]);
  const [error, setError] = useState("");

  async function search() {
    setError("");
    try {
      const body = await api.searchTmdb(query);
      setHits(body.results);
      if (body.results.length === 0) setError("没有结果");
    } catch (thrown) {
      setError((thrown as Error).message);
    }
  }

  return (
    <div className="field field-wide">
      <span>TMDB 剧集</span>
      <span className="inline-row">
        <input
          placeholder="搜索 TMDB"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void search();
            }
          }}
        />
        <button type="button" onClick={search} disabled={!query.trim()}>
          搜索
        </button>
      </span>
      {error && <span className="error">{error}</span>}
      {hits.length > 0 && (
        <ul className="pick-list">
          {hits.slice(0, 8).map((hit) => (
            <li key={hit.tmdb_id}>
              <button
                type="button"
                className="link"
                onClick={() => {
                  onPick(hit);
                  setHits([]);
                }}
              >
                {hit.title}
                {hit.year ? ` (${hit.year})` : ""} · tmdb-{hit.tmdb_id}
                {hit.original_title && hit.original_title !== hit.title
                  ? ` · ${hit.original_title}`
                  : ""}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PreviewTable({ releases }: { releases: ReleasePreview[] }) {
  if (releases.length === 0) return <p className="muted">索引器没有搜到结果。</p>;
  return (
    <ul className="runs preview">
      {releases.map((item) => (
        <li key={item.guid}>
          <div className="intake-row">
            <span className="run-main">
              <span className="folder">{item.title}</span>
              <span className="title">
                {episodesLabel(item.season, item.episodes)}
                {item.size_bytes ? ` · ${formatBytes(item.size_bytes)}` : ""}
                {item.flags.length ? ` · ${item.flags.join(" ")}` : ""}
              </span>
            </span>
            <span className="run-side">
              <span className={item.verdict === "grab" ? "badge ok" : "badge"}>
                {VERDICT_LABEL[item.verdict]}
              </span>
              {item.published_at && (
                <span className="time">{formatWhen(item.published_at)}</span>
              )}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function SubscriptionForm({
  configs,
  initial,
  editing,
  onSubmit,
  onCancel,
}: {
  configs: WatchConfig[];
  initial: FormState;
  editing: boolean;
  onSubmit: (input: SubscriptionInput) => Promise<void>;
  onCancel: () => void;
}) {
  const [form, setForm] = useState(initial);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<ReleasePreview[] | null>(null);
  const set = (patch: Partial<FormState>) => setForm({ ...form, ...patch });
  const series = configs.filter((config) => config.media_type !== "movie");
  const ready = form.config_id && Number(form.tmdb_id) > 0 && form.query.trim();

  async function runPreview() {
    setError("");
    setBusy(true);
    try {
      setPreview((await api.previewSubscription(toInput(form))).releases);
    } catch (thrown) {
      setError((thrown as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="card subscription-form"
      onSubmit={async (event) => {
        event.preventDefault();
        setError("");
        setBusy(true);
        try {
          await onSubmit(toInput(form));
        } catch (thrown) {
          setError((thrown as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      <TmdbPicker
        onPick={(hit) =>
          set({
            tmdb_id: String(hit.tmdb_id),
            name: hit.title,
            query: form.query || hit.title,
          })
        }
      />
      <div className="field-grid">
        <label className="field">
          TMDB ID
          <input
            inputMode="numeric"
            value={form.tmdb_id}
            onChange={(event) => set({ tmdb_id: event.target.value })}
          />
        </label>
        <label className="field">
          名称
          <input
            value={form.name}
            onChange={(event) => set({ name: event.target.value })}
          />
        </label>
        <label className="field">
          搜索词
          <input
            value={form.query}
            onChange={(event) => set({ query: event.target.value })}
          />
        </label>
        <label className="field">
          发布组（逗号分隔，空 = 任意）
          <input
            placeholder="HHWEB, ADWeb"
            value={form.groups}
            onChange={(event) => set({ groups: event.target.value })}
          />
        </label>
        <label className="field">
          必须包含（正则）
          <input
            placeholder="1080p"
            value={form.include}
            onChange={(event) => set({ include: event.target.value })}
          />
        </label>
        <label className="field">
          排除（正则）
          <input
            placeholder="2160p|Immersive"
            value={form.exclude}
            onChange={(event) => set({ exclude: event.target.value })}
          />
        </label>
        <label className="field">
          季（空 = 不限）
          <input
            inputMode="numeric"
            value={form.season}
            onChange={(event) => set({ season: event.target.value })}
          />
        </label>
        <label className="field">
          归档到监控目录
          <select
            value={form.config_id}
            onChange={(event) => set({ config_id: event.target.value })}
          >
            <option value="">选择…</option>
            {series.map((config) => (
              <option key={config.id} value={config.id}>
                {config.name}（{config.inbound_root}）
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="field field-wide">
        给归档 Agent 的说明（可选）
        <textarea
          rows={2}
          placeholder="例如：第N期上/下 对应 TMDB 的相邻两集"
          value={form.notes}
          onChange={(event) => set({ notes: event.target.value })}
        />
      </label>
      {!editing && (
        <label className="checkbox-field">
          <input
            type="checkbox"
            checked={form.backfill}
            onChange={(event) => set({ backfill: event.target.checked })}
          />
          补抓索引器上已有的集（不勾只抓今后发布的）
        </label>
      )}
      <div className="form-actions">
        <button type="submit" className="primary" disabled={!ready || busy}>
          {editing ? "保存" : "添加订阅"}
        </button>
        <button type="button" disabled={!ready || busy} onClick={runPreview}>
          预览匹配
        </button>
        <button type="button" onClick={onCancel}>
          取消
        </button>
        {error && <span className="error">{error}</span>}
      </div>
      {preview && <PreviewTable releases={preview} />}
    </form>
  );
}

function GrabRow({ grab, onChanged }: { grab: Grab; onChanged: () => void }) {
  const [error, setError] = useState("");
  return (
    <li>
      <div className="intake-row">
        <span className="run-main">
          <span className="folder">{grab.title}</span>
          <span className="title">
            {episodesLabel(grab.season, grab.episodes)}
            {grab.size_bytes ? ` · ${formatBytes(grab.size_bytes)}` : ""}
          </span>
          {grab.state === "downloading" && (
            <span className="progress" role="progressbar">
              <span
                className="progress-fill"
                style={{ width: `${Math.round(grab.progress * 100)}%` }}
              />
            </span>
          )}
        </span>
        <span className="run-side">
          <span className={`badge ${grabClass(grab.state)}`}>
            {GRAB_LABEL[grab.state]}
          </span>
          {grab.state === "downloading" && (
            <span className="summary">{(grab.progress * 100).toFixed(1)}%</span>
          )}
          {grab.error && <span className="summary">{grab.error}</span>}
          {grab.run_id && <Link to={`/runs/${grab.run_id}`}>任务</Link>}
          {grab.updated_at && (
            <span className="time">{formatWhen(grab.updated_at)}</span>
          )}
          <span className="row-actions">
            <button
              type="button"
              onClick={async () => {
                const question =
                  grab.state === "downloading"
                    ? "取消下载并删除已下载的部分，之后可再次抓取。确定？"
                    : "忘记这条记录，之后可再次抓取同一种子（文件不受影响）。确定？";
                if (!window.confirm(question)) return;
                try {
                  await api.forgetGrab(grab.id);
                  onChanged();
                } catch (thrown) {
                  setError((thrown as Error).message);
                }
              }}
            >
              {grab.state === "downloading" ? "取消" : "忘记"}
            </button>
          </span>
          {error && <span className="error">{error}</span>}
        </span>
      </div>
    </li>
  );
}

function SubscriptionCard({
  subscription,
  configs,
  onChanged,
}: {
  subscription: Subscription;
  configs: WatchConfig[];
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(
    null,
  );
  const [preview, setPreview] = useState<ReleasePreview[] | null>(null);
  const [busy, setBusy] = useState(false);
  const config = configs.find((item) => item.id === subscription.config_id);

  async function act(action: () => Promise<string>) {
    setBusy(true);
    setNotice(null);
    try {
      setNotice({ ok: true, text: await action() });
      onChanged();
    } catch (thrown) {
      setNotice({ ok: false, text: (thrown as Error).message });
    } finally {
      setBusy(false);
    }
  }

  if (editing) {
    return (
      <SubscriptionForm
        configs={configs}
        initial={fromSubscription(subscription)}
        editing
        onCancel={() => setEditing(false)}
        onSubmit={async (input) => {
          await api.updateSubscription(subscription.id, {
            ...input,
            clear_season: input.season === null,
            season: input.season ?? undefined,
          });
          setEditing(false);
          onChanged();
        }}
      />
    );
  }

  return (
    <section className="card subscription">
      <div className="config-head">
        <strong>{subscription.name}</strong>
        <a
          className="tag"
          href={`https://www.themoviedb.org/tv/${subscription.tmdb_id}`}
          target="_blank"
          rel="noreferrer"
        >
          tmdb-{subscription.tmdb_id}
        </a>
        <span className={subscription.enabled ? "badge ok" : "badge"}>
          {subscription.enabled ? "已启用" : "已停用"}
        </span>
        {subscription.groups.map((group) => (
          <span key={group} className="tag">
            {group}
          </span>
        ))}
      </div>
      <p className="muted">
        搜索「{subscription.query}」
        {subscription.season !== null ? ` · 第 ${subscription.season} 季` : ""}
        {subscription.include ? ` · 包含 /${subscription.include}/` : ""}
        {subscription.exclude ? ` · 排除 /${subscription.exclude}/` : ""}
        {config ? ` · → ${config.name}` : " · 监控目录已删除"}
        {subscription.last_checked_at
          ? ` · 上次检查 ${formatWhen(subscription.last_checked_at)}`
          : " · 尚未检查"}
      </p>
      {subscription.last_error && (
        <p className="error">上次检查出错：{subscription.last_error}</p>
      )}
      <div className="form-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            act(async () => {
              const body = await api.checkSubscription(subscription.id);
              return body.grabbed.length
                ? `抓取了 ${body.grabbed.length} 个种子`
                : "没有新种子";
            })
          }
        >
          立即检查
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            act(async () => {
              setPreview((await api.previewExisting(subscription.id)).releases);
              return "";
            })
          }
        >
          预览匹配
        </button>
        <button type="button" onClick={() => setEditing(true)}>
          编辑
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            act(async () => {
              await api.updateSubscription(subscription.id, {
                enabled: !subscription.enabled,
              });
              return subscription.enabled ? "已停用" : "已启用";
            })
          }
        >
          {subscription.enabled ? "停用" : "启用"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            if (window.confirm("删除这个订阅及其抓取记录？已下载的文件不受影响。"))
              void act(async () => {
                await api.deleteSubscription(subscription.id);
                return "已删除";
              });
          }}
        >
          删除
        </button>
        {notice?.text && (
          <span className={notice.ok ? "muted" : "error"}>{notice.text}</span>
        )}
      </div>
      {preview && <PreviewTable releases={preview} />}
      {subscription.grabs.length > 0 && (
        <ul className="runs">
          {subscription.grabs.map((grab) => (
            <GrabRow key={grab.id} grab={grab} onChanged={onChanged} />
          ))}
        </ul>
      )}
    </section>
  );
}

const EMPTY_FORM: FormState = {
  name: "",
  config_id: "",
  tmdb_id: "",
  query: "",
  season: "",
  groups: "",
  include: "",
  exclude: "",
  notes: "",
  backfill: true,
};

export function SubscriptionsPage() {
  const { data, error, loading, refresh } = usePoll(
    () => api.listSubscriptions(),
    5000,
  );
  const [configs, setConfigs] = useState<WatchConfig[]>([]);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    api.listConfigs().then((body) => setConfigs(body.configs));
  }, []);

  if (loading && !data) return <p className="loading">载入中…</p>;
  if (error && !data) return <p className="error">{error}</p>;
  const subscriptions = data?.subscriptions ?? [];

  return (
    <>
      <h1>订阅</h1>
      {error && <p className="banner">连接中断：{error} · 正在自动重试</p>}
      <p className="muted">
        按发布组和关键词订阅 PT 资源：经 Prowlarr 定时搜索，匹配的种子交给 PT
        下载器，下载完成后按订阅的 TMDB 条目由 Agent 归档进媒体库。
      </p>
      {adding ? (
        <SubscriptionForm
          configs={configs}
          initial={EMPTY_FORM}
          editing={false}
          onCancel={() => setAdding(false)}
          onSubmit={async (input) => {
            await api.createSubscription(input);
            setAdding(false);
            refresh();
          }}
        />
      ) : (
        <button type="button" className="primary" onClick={() => setAdding(true)}>
          新建订阅
        </button>
      )}
      {subscriptions.length === 0 ? (
        <p className="empty">还没有订阅。</p>
      ) : (
        subscriptions.map((subscription) => (
          <SubscriptionCard
            key={subscription.id}
            subscription={subscription}
            configs={configs}
            onChanged={refresh}
          />
        ))
      )}
    </>
  );
}
