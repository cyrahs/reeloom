import { api, type SubtitleRecheck } from "../api";
import { Link } from "../router";
import { formatDateTime, formatWhen } from "../time";
import { usePoll } from "../usePoll";

const STATUS_LABEL: Record<SubtitleRecheck["status"], string> = {
  searching: "搜索中",
  waiting: "等待下次",
  given_up: "已停止",
};

function statusClass(status: SubtitleRecheck["status"]): string {
  if (status === "searching") return "busy";
  if (status === "given_up") return "warn";
  return "ok";
}

const iso = (seconds: number) => new Date(seconds * 1000).toISOString();

/** "MM-DD HH:mm" or, for a moment already past, "即将" — the worker only
 * acts every few minutes, so a due time can sit briefly in the past. */
function formatDue(seconds: number, nowSeconds: number): string {
  return seconds <= nowSeconds ? "即将" : formatDateTime(iso(seconds));
}

function Row({ item, now }: { item: SubtitleRecheck; now: number }) {
  return (
    <li>
      <Link to={`/runs/${item.run_id}`}>
        <span className="run-main">
          <span className="folder">
            {item.title} ({item.year})
          </span>
          <span className="title">
            {item.config_name} · {item.folder_name}
          </span>
          {item.note && <span className="title">字幕：{item.note}</span>}
        </span>
        <span className="run-side">
          <span className={`badge ${statusClass(item.status)}`}>
            {STATUS_LABEL[item.status]}
          </span>
          <span className="summary">
            {item.count > 0 ? `已复查 ${item.count} 次` : "尚未复查"}
            {item.last_at !== null &&
              ` · 上次 ${formatWhen(iso(item.last_at))}`}
          </span>
          <span className="time">
            {item.status === "waiting" && item.next_at !== null
              ? `下次 ${formatDue(item.next_at, now)} · `
              : ""}
            {item.status === "given_up" ? "截止于 " : "截止 "}
            {formatDateTime(iso(item.deadline))}
          </span>
        </span>
      </Link>
    </li>
  );
}

export function SubtitlesPage() {
  const { data, error, loading } = usePoll(api.listSubtitleRechecks, 10_000);

  if (loading && !data) return <p className="loading">加载中…</p>;
  if (error && !data) return <p className="error">{error}</p>;
  if (!data) return null;

  const active = data.items.filter((item) => item.status !== "given_up");
  const stopped = data.items.filter((item) => item.status === "given_up");

  return (
    <>
      <h1>字幕复查</h1>
      <p className="muted">
        开启字幕获取的番剧整理完成时若仍缺字幕，会在这里每天再搜一次
        ACG.RIP；找到即通知，超过复查天数仍未找到则发出警告并停止。
      </p>
      {data.days === 0 ? (
        <p className="empty">字幕复查已在设置中关闭。</p>
      ) : (
        <section>
          <h2>每日搜索中 · {active.length}</h2>
          {active.length === 0 ? (
            <p className="empty">没有在等字幕的项目。</p>
          ) : (
            <ul className="runs">
              {active.map((item) => (
                <Row key={item.run_id} item={item} now={data.now} />
              ))}
            </ul>
          )}
        </section>
      )}
      {stopped.length > 0 && (
        <section>
          <h2>已停止 · {stopped.length}</h2>
          <p className="muted">
            复查天数内始终没有找到。延长设置中的复查天数会重新开始搜索。
          </p>
          <ul className="runs">
            {stopped.map((item) => (
              <Row key={item.run_id} item={item} now={data.now} />
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
