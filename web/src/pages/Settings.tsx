import { useEffect, useState } from "react";

import { api, type Settings, type WatchConfig } from "../api";
import { DirPicker } from "../DirPicker";

const EMPTY_CONFIG = {
  name: "",
  inbound_root: "",
  library_root: "",
  media_type: "anime" as const,
  stability_seconds: 120,
  acquire_subtitles: false,
  subtitle_variant: "chs" as "chs" | "cht",
  notify: true,
  enabled: true,
};

function ConfigRow({
  config,
  onChanged,
}: {
  config: WatchConfig;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function patch(body: Partial<WatchConfig>) {
    setBusy(true);
    try {
      await api.updateConfig(config.id, body);
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="config">
      <div className="config-head">
        <strong>{config.name}</strong>
        <span className="tag">{config.media_type}</span>
        <label>
          <input
            type="checkbox"
            checked={config.enabled}
            disabled={busy}
            onChange={(event) => patch({ enabled: event.target.checked })}
          />
          启用
        </label>
        <label title="仅对 anime 生效">
          <input
            type="checkbox"
            checked={config.acquire_subtitles}
            disabled={busy}
            onChange={(event) =>
              patch({ acquire_subtitles: event.target.checked })
            }
          />
          自动找字幕
        </label>
        <label title="首选字幕语种（仅自动找字幕生效）">
          <select
            value={config.subtitle_variant}
            disabled={busy}
            onChange={(event) =>
              patch({
                subtitle_variant: event.target.value as "chs" | "cht",
              })
            }
          >
            <option value="chs">简体</option>
            <option value="cht">繁体</option>
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={config.notify}
            disabled={busy}
            onChange={(event) => patch({ notify: event.target.checked })}
          />
          通知
        </label>
        <button
          className="link"
          disabled={busy}
          onClick={async () => {
            if (!confirm(`删除监控 ${config.name}？`)) return;
            await api.deleteConfig(config.id);
            onChanged();
          }}
        >
          删除
        </button>
      </div>
      <div className="config-paths">
        <code>{config.inbound_root}</code> → <code>{config.library_root}</code>
        <span className="muted"> · 静置 {config.stability_seconds}s</span>
      </div>
    </li>
  );
}

function PathField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const [picking, setPicking] = useState(false);

  return (
    <>
      <label className="field">
        {label}
        <span className="path-field">
          <input
            placeholder="/绝对路径"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            required
          />
          <button type="button" onClick={() => setPicking(true)}>
            浏览…
          </button>
        </span>
      </label>
      {picking && (
        <DirPicker
          title={`选择${label}`}
          initial={value}
          onSelect={(path) => {
            onChange(path);
            setPicking(false);
          }}
          onClose={() => setPicking(false)}
        />
      )}
    </>
  );
}

function NewConfig({ onCreated }: { onCreated: () => void }) {
  const [form, setForm] = useState(EMPTY_CONFIG);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api.createConfig(form);
      setForm(EMPTY_CONFIG);
      onCreated();
    } catch (thrown) {
      setError((thrown as Error).message);
    }
  }

  return (
    <form className="new-config card" onSubmit={submit}>
      <h3>添加监控</h3>
      <label className="field">
        名称
        <input
          value={form.name}
          onChange={(event) => setForm({ ...form, name: event.target.value })}
          required
        />
      </label>
      <PathField
        label="监控目录"
        value={form.inbound_root}
        onChange={(inbound_root) => setForm({ ...form, inbound_root })}
      />
      <PathField
        label="媒体库目录"
        value={form.library_root}
        onChange={(library_root) => setForm({ ...form, library_root })}
      />
      <label className="field">
        媒体类型
        <select
          value={form.media_type}
          onChange={(event) =>
            setForm({
              ...form,
              media_type: event.target.value as typeof form.media_type,
            })
          }
        >
          <option value="anime">动画</option>
          <option value="tv">剧集</option>
          <option value="movie">电影</option>
        </select>
      </label>
      <label className="field">
        字幕偏好
        <select
          value={form.subtitle_variant}
          onChange={(event) =>
            setForm({
              ...form,
              subtitle_variant: event.target.value as "chs" | "cht",
            })
          }
        >
          <option value="chs">简体</option>
          <option value="cht">繁体</option>
        </select>
      </label>
      <label className="field">
        静置秒数
        <input
          type="number"
          min={0}
          value={form.stability_seconds}
          onChange={(event) =>
            setForm({
              ...form,
              stability_seconds: Number(event.target.value),
            })
          }
        />
      </label>
      <div className="form-actions">
        <button type="submit" className="primary">
          添加监控
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function Credentials({
  settings,
  onSaved,
}: {
  settings: Settings;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);

  const field = (key: string, label: string, hint?: string) => (
    <label key={key} className="field">
      {label}
      {hint && <span className="muted"> {hint}</span>}
      <input
        type={key.includes("key") || key.includes("token") ? "password" : "text"}
        value={form[key] ?? ""}
        placeholder={hint === "已设置" ? "保持不变" : ""}
        onChange={(event) => setForm({ ...form, [key]: event.target.value })}
      />
    </label>
  );

  return (
    <form
      className="credentials card"
      onSubmit={async (event) => {
        event.preventDefault();
        const body = Object.fromEntries(
          Object.entries(form).filter(([, value]) => value !== ""),
        );
        await api.putSettings(body);
        setForm({});
        setSaved(true);
        onSaved();
      }}
    >
      {field(
        "tmdb_api_key",
        "TMDB API Key",
        settings.tmdb_api_key_set ? "已设置" : "未设置",
      )}
      {field("llm_base_url", "模型 Base URL")}
      {field(
        "llm_api_key",
        "模型 API Key",
        settings.llm_api_key_set ? "已设置" : "未设置",
      )}
      {field("llm_model", "模型名称")}
      {field(
        "telegram_bot_token",
        "Telegram Bot Token",
        settings.telegram_bot_token_set ? "已设置" : "未设置",
      )}
      {field("telegram_chat_id", "Telegram Chat ID")}
      <div className="form-actions">
        <button type="submit" className="primary">
          保存
        </button>
        {saved && <span className="muted">已保存</span>}
      </div>
    </form>
  );
}

export function SettingsPage() {
  const [configs, setConfigs] = useState<WatchConfig[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);

  const reload = () => {
    api.listConfigs().then((body) => setConfigs(body.configs));
    api.getSettings().then(setSettings);
  };

  useEffect(reload, []);

  return (
    <>
      <h1>设置</h1>
      <section>
        <h2>监控目录</h2>
        <ul className="configs">
          {configs.map((config) => (
            <ConfigRow key={config.id} config={config} onChanged={reload} />
          ))}
        </ul>
        <NewConfig onCreated={reload} />
      </section>
      <section>
        <h2>凭据</h2>
        {settings && <Credentials settings={settings} onSaved={reload} />}
        <p className="muted">
          已保存的密钥不会回显，留空表示保持不变。
        </p>
      </section>
    </>
  );
}
