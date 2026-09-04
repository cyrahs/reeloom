import { useEffect, useRef, useState } from "react";

import {
  DOWNLOAD_STATE_LABEL,
  api,
  errorLabel,
  type DownloadState,
  type MagnetDownload,
} from "../api";
import { DirPicker } from "../DirPicker";
import { formatWhen } from "../time";
import { usePoll } from "../usePoll";

function stateClass(state: DownloadState): string {
  if (state === "failed" || state === "stalled" || state === "lost")
    return "warn";
  if (state === "completed") return "ok";
  if (state === "removed") return "";
  return "busy";
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / 1024 ** 4).toFixed(1)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function magnetDisplayName(magnet: string): string | null {
  const query = magnet.indexOf("?");
  if (query < 0) return null;
  try {
    return new URLSearchParams(magnet.slice(query + 1)).get("dn")?.trim() || null;
  } catch {
    return null;
  }
}

/** Best available name: CloudDrive's task name, else the landed folder,
 * else the magnet's own ``dn`` hint, else the raw link truncated. */
function label(download: MagnetDownload): string {
  if (download.name) return download.name;
  const landed = download.final_path?.split("/").filter(Boolean).at(-1);
  if (landed) return landed;
  const hinted = magnetDisplayName(download.magnet);
  if (hinted) return hinted;
  const magnet = download.magnet;
  return magnet.length > 60 ? `${magnet.slice(0, 60)}…` : magnet;
}

function AddForm({
  dirs,
  onAdded,
}: {
  dirs: string[];
  onAdded: () => void;
}) {
  const [magnets, setMagnets] = useState("");
  const [directory, setDirectory] = useState("");
  const [picking, setPicking] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(
    null,
  );
  // Seed the directory from the most recently used one exactly once, and
  // never over what the user already typed.
  const seeded = useRef(false);
  useEffect(() => {
    if (seeded.current || dirs.length === 0) return;
    seeded.current = true;
    setDirectory((current) => current || dirs[0]);
  }, [dirs]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const lines = magnets
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (lines.length === 0 || !directory.trim()) return;
    setSubmitting(true);
    setNotice(null);
    let added = 0;
    const failures: string[] = [];
    // One request per magnet: a bad line fails alone instead of the batch.
    for (const magnet of lines) {
      try {
        await api.addDownload(magnet, directory.trim());
        added += 1;
      } catch (thrown) {
        failures.push(
          `${magnet.slice(0, 40)}… ${errorLabel({ code: (thrown as Error).message })}`,
        );
      }
    }
    setSubmitting(false);
    if (failures.length === 0) {
      setMagnets("");
      setNotice({ ok: true, text: `已添加 ${added} 个下载` });
    } else {
      setNotice({
        ok: false,
        text: `${added > 0 ? `已添加 ${added} 个；` : ""}失败：${failures.join("；")}`,
      });
    }
    if (added > 0) onAdded();
  }

  return (
    <form className="card downloads-add" onSubmit={submit}>
      <h2>添加磁力下载</h2>
      <label className="field field-wide">
        磁力链接（每行一个）
        <textarea
          rows={3}
          placeholder="magnet:?xt=urn:btih:…"
          value={magnets}
          onChange={(event) => setMagnets(event.target.value)}
        />
      </label>
      <label className="field field-wide">
        下载目录（CloudDrive 路径）
        <div className="field-pair">
          <input
            placeholder="/115/downloads"
            value={directory}
            onChange={(event) => setDirectory(event.target.value)}
            list="download-dir-history"
          />
          <button type="button" onClick={() => setPicking(true)}>
            浏览…
          </button>
        </div>
        <datalist id="download-dir-history">
          {dirs.map((dir) => (
            <option key={dir} value={dir} />
          ))}
        </datalist>
        <span className="muted">
          任务先下载到该目录的 in_progress 子目录（reeloom
          保留名，扫描器不会拾取），完成后自动移出并进入正常整理流程。
        </span>
      </label>
      <div className="form-actions">
        <button
          type="submit"
          className="primary"
          disabled={submitting || !magnets.trim() || !directory.trim()}
        >
          {submitting ? "提交中…" : "添加"}
        </button>
        {notice && (
          <span className={notice.ok ? "muted" : "error"}>{notice.text}</span>
        )}
      </div>
      {picking && (
        <DirPicker
          title="选择下载目录"
          initial={directory || "/"}
          fetchDirs={api.listCloudDirs}
          onSelect={(path) => {
            setDirectory(path);
            setPicking(false);
          }}
          onClose={() => setPicking(false)}
        />
      )}
    </form>
  );
}

function Row({
  download,
  onChanged,
}: {
  download: MagnetDownload;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await action();
      onChanged();
    } catch (thrown) {
      setError(errorLabel({ code: (thrown as Error).message }));
    } finally {
      setBusy(false);
    }
  }

  const live =
    download.state !== "completed" &&
    download.state !== "lost" &&
    download.state !== "removed";
  const retryable =
    download.state === "failed" || download.state === "stalled";
  const progress =
    download.progress !== null &&
    (download.state === "downloading" ||
      download.state === "submitted" ||
      download.state === "stalled")
      ? Math.min(100, Math.max(0, download.progress))
      : null;

  return (
    <li>
      <div className="intake-row">
        <span className="run-main">
          <span className="folder">{label(download)}</span>
          <span className="title">
            {download.download_dir}
            {download.size_bytes ? ` · ${formatBytes(download.size_bytes)}` : ""}
          </span>
          {progress !== null && (
            <span className="progress" role="progressbar">
              <span
                className="progress-fill"
                style={{ width: `${progress}%` }}
              />
            </span>
          )}
        </span>
        <span className="run-side">
          <span className={`badge ${stateClass(download.state)}`}>
            {DOWNLOAD_STATE_LABEL[download.state]}
          </span>
          {progress !== null && (
            <span className="summary">{progress.toFixed(1)}%</span>
          )}
          {download.error && (
            <span className="summary">
              {errorLabel({ code: download.error })}
            </span>
          )}
          {download.updated_at && (
            <span className="time">{formatWhen(download.updated_at)}</span>
          )}
          <span className="row-actions">
            {retryable && (
              <button
                type="button"
                disabled={busy}
                onClick={() => act(() => api.retryDownload(download.id))}
              >
                重试
              </button>
            )}
            {live && (
              <button
                type="button"
                disabled={busy || download.state === "moving"}
                onClick={() => {
                  if (
                    window.confirm(
                      "同时删除 CloudDrive 上的任务与已下载数据，确定？",
                    )
                  )
                    act(() => api.deleteDownload(download.id));
                }}
              >
                删除
              </button>
            )}
            {!live && (
              <button
                type="button"
                disabled={busy}
                onClick={() => act(() => api.deleteDownloadRow(download.id))}
              >
                移除记录
              </button>
            )}
          </span>
          {error && <span className="error">{error}</span>}
        </span>
      </div>
    </li>
  );
}

export function DownloadsPage() {
  const { data, error, loading, refresh } = usePoll(
    () => api.listDownloads(),
    4000,
  );

  if (loading && !data) return <p className="loading">载入中…</p>;
  if (error && !data) return <p className="error">{error}</p>;

  const downloads = data?.downloads ?? [];

  return (
    <>
      <h1>下载</h1>
      {error && <p className="banner">连接中断：{error} · 正在自动重试</p>}
      <AddForm dirs={data?.dirs ?? []} onAdded={refresh} />
      {downloads.length === 0 ? (
        <p className="empty">还没有下载。</p>
      ) : (
        <ul className="runs">
          {downloads.map((download) => (
            <Row key={download.id} download={download} onChanged={refresh} />
          ))}
        </ul>
      )}
    </>
  );
}
