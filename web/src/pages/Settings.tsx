import { useEffect, useState } from "react";

import { api, type Settings, type WatchConfig } from "../api";
import { DirPicker } from "../DirPicker";

const EMPTY_CONFIG = {
  name: "",
  inbound_root: "",
  library_root: "",
  media_type: "anime" as WatchConfig["media_type"],
  stability_seconds: 120,
  acquire_subtitles: false,
  subtitle_variant: "chs" as "chs" | "cht",
  notify: true,
  enabled: true,
  replace_enabled: false,
  replace_extra_dirs: [] as string[],
  replace_auto_ratio: 1.2,
};

interface ConfigForm {
  name: string;
  inbound_root: string;
  library_root: string;
  media_type: WatchConfig["media_type"];
  stability_seconds: number;
  replace_enabled: boolean;
  replace_extra_dirs: string[];
  replace_auto_ratio: number;
}

function ConfigRow({
  config,
  onChanged,
}: {
  config: WatchConfig;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);

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
        <label title="发现库内或额外目录已有旧版本时自动替换/去重">
          <input
            type="checkbox"
            checked={config.replace_enabled}
            disabled={busy}
            onChange={(event) =>
              patch({ replace_enabled: event.target.checked })
            }
          />
          洗版
        </label>
        <span className="config-actions">
          <button
            className="link"
            disabled={busy}
            onClick={() => setEditing((value) => !value)}
          >
            {editing ? "收起" : "编辑"}
          </button>
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
        </span>
      </div>
      <div className="config-paths">
        <code>{config.inbound_root}</code> → <code>{config.library_root}</code>
        <span className="muted"> · 静置 {config.stability_seconds}s</span>
      </div>
      {editing && (
        <EditConfig
          config={config}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
          onCancel={() => setEditing(false)}
        />
      )}
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

function ExtraDirsField({
  dirs,
  onChange,
}: {
  dirs: string[];
  onChange: (dirs: string[]) => void;
}) {
  return (
    <div className="field extra-dirs">
      额外检测目录
      {dirs.map((dir, index) => (
        <span className="path-row" key={index}>
          <PathField
            label={`目录 ${index + 1}`}
            value={dir}
            onChange={(value) =>
              onChange(dirs.map((item, at) => (at === index ? value : item)))
            }
          />
          <button
            type="button"
            className="link"
            onClick={() => onChange(dirs.filter((_, at) => at !== index))}
          >
            移除
          </button>
        </span>
      ))}
      <button
        type="button"
        onClick={() => onChange([...dirs, ""])}
        disabled={dirs.length >= 8}
      >
        添加目录
      </button>
      <span className="muted">
        除媒体库外，还会在这些目录里检测旧版本（如 anirss 下载目录）。
      </span>
    </div>
  );
}

function ConfigFields({
  form,
  onChange,
}: {
  form: ConfigForm;
  onChange: (patch: Partial<ConfigForm>) => void;
}) {
  return (
    <>
      <label className="field">
        名称
        <input
          value={form.name}
          onChange={(event) => onChange({ name: event.target.value })}
          required
        />
      </label>
      <PathField
        label="监控目录"
        value={form.inbound_root}
        onChange={(inbound_root) => onChange({ inbound_root })}
      />
      <PathField
        label="媒体库目录"
        value={form.library_root}
        onChange={(library_root) => onChange({ library_root })}
      />
      <label className="field">
        媒体类型
        <select
          value={form.media_type}
          onChange={(event) =>
            onChange({
              media_type: event.target.value as WatchConfig["media_type"],
            })
          }
        >
          <option value="anime">动画</option>
          <option value="tv">剧集</option>
          <option value="movie">电影</option>
        </select>
      </label>
      <label className="field">
        静置秒数
        <input
          type="number"
          min={0}
          value={form.stability_seconds}
          onChange={(event) =>
            onChange({ stability_seconds: Number(event.target.value) })
          }
        />
      </label>
      <label className="field checkbox-field">
        <input
          type="checkbox"
          checked={form.replace_enabled}
          onChange={(event) =>
            onChange({ replace_enabled: event.target.checked })
          }
        />
        洗版：发现已有旧版本时自动替换/去重
      </label>
      {form.replace_enabled && (
        <>
          <label className="field">
            自动替换体积比
            <input
              type="number"
              min={1}
              max={10}
              step={0.05}
              value={form.replace_auto_ratio}
              onChange={(event) =>
                onChange({ replace_auto_ratio: Number(event.target.value) })
              }
            />
            <span className="muted">
              新版总体积达到旧版的该倍数时自动洗版，低于则转人工确认。
            </span>
          </label>
          <ExtraDirsField
            dirs={form.replace_extra_dirs}
            onChange={(replace_extra_dirs) => onChange({ replace_extra_dirs })}
          />
        </>
      )}
    </>
  );
}

function EditConfig({
  config,
  onSaved,
  onCancel,
}: {
  config: WatchConfig;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<ConfigForm>({
    name: config.name,
    inbound_root: config.inbound_root,
    library_root: config.library_root,
    media_type: config.media_type,
    stability_seconds: config.stability_seconds,
    replace_enabled: config.replace_enabled,
    replace_extra_dirs: config.replace_extra_dirs,
    replace_auto_ratio: config.replace_auto_ratio,
  });
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api.updateConfig(config.id, form);
      onSaved();
    } catch (thrown) {
      setError((thrown as Error).message);
    }
  }

  return (
    <form className="edit-config" onSubmit={submit}>
      <ConfigFields
        form={form}
        onChange={(patch) => setForm({ ...form, ...patch })}
      />
      <div className="form-actions">
        <button type="submit" className="primary">
          保存
        </button>
        <button type="button" onClick={onCancel}>
          取消
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </form>
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
      <ConfigFields
        form={form}
        onChange={(patch) => setForm({ ...form, ...patch })}
      />
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
  const [form, setForm] = useState<Record<string, string>>({
    llm_base_url: settings.llm_base_url,
    llm_model: settings.llm_model,
    llm_reasoning_effort: settings.llm_reasoning_effort,
    telegram_chat_id: settings.telegram_chat_id,
    trash_retention_days: String(settings.trash_retention_days),
  });
  const [saved, setSaved] = useState(false);

  const bind = (key: string) => ({
    value: form[key] ?? "",
    onChange: (event: React.ChangeEvent<HTMLInputElement>) => {
      setSaved(false);
      setForm({ ...form, [key]: event.target.value });
    },
  });

  const secretField = (key: string, label: string, isSet: boolean) => (
    <label className="field">
      <span className="field-label">
        {label}
        <span className={isSet ? "badge ok" : "badge"}>
          {isSet ? "已配置" : "未配置"}
        </span>
      </span>
      <input
        type="password"
        placeholder={isSet ? "••••••••••••" : ""}
        autoComplete="new-password"
        {...bind(key)}
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
        // Unlike the text fields, "" is a real choice here (provider
        // default), so it must reach the server instead of meaning
        // "keep unchanged".
        body.llm_reasoning_effort = form.llm_reasoning_effort ?? "";
        await api.putSettings(body);
        setForm({
          llm_base_url: form.llm_base_url,
          llm_model: form.llm_model,
          llm_reasoning_effort: form.llm_reasoning_effort,
          telegram_chat_id: form.telegram_chat_id,
          trash_retention_days: form.trash_retention_days,
        });
        setSaved(true);
        onSaved();
      }}
    >
      <div className="cred-group">
        <h3>TMDB</h3>
        {secretField("tmdb_api_key", "API Key", settings.tmdb_api_key_set)}
      </div>
      <div className="cred-group">
        <h3>模型</h3>
        <label className="field">
          Base URL
          <input placeholder="https://…" {...bind("llm_base_url")} />
        </label>
        {secretField("llm_api_key", "API Key", settings.llm_api_key_set)}
        <label className="field">
          模型名称
          <input {...bind("llm_model")} />
        </label>
        <label className="field">
          推理强度
          <select
            value={form.llm_reasoning_effort ?? ""}
            onChange={(event) => {
              setSaved(false);
              setForm({ ...form, llm_reasoning_effort: event.target.value });
            }}
          >
            <option value="">默认</option>
            <option value="minimal">minimal</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
          </select>
        </label>
      </div>
      <div className="cred-group">
        <h3>回收区</h3>
        <label className="field">
          保留天数
          <input
            type="number"
            min={0}
            max={365}
            {...bind("trash_retention_days")}
          />
          <span className="muted">
            被洗版替换/判定重复的文件先移入各根目录下的隐藏回收区
            .reeloom-trash，到期后自动删除；0 表示任务完成即删。
          </span>
        </label>
      </div>
      <div className="cred-group">
        <h3>Telegram</h3>
        {secretField(
          "telegram_bot_token",
          "Bot Token",
          settings.telegram_bot_token_set,
        )}
        <label className="field">
          Chat ID
          <input {...bind("telegram_chat_id")} />
        </label>
      </div>
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
