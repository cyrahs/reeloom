import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { ApiError, idempotencyKey, type ApiClient } from "../api";
import { useAuth } from "../auth";
import { PageError } from "../components/Status";
import {
  configSchema,
  directoryListingSchema,
  type Config,
} from "../schemas";
import {
  workTypeLabel,
  workTypes,
  type WorkType,
} from "../workTypes";

type RootMode = "retain" | "replace";
type WatchForm = {
  watch_id: string;
  work_type: WorkType;
  poll_interval_seconds: number;
  settle_interval_seconds: number;
  rootMode: RootMode;
  rootPath: string;
};
type RouteForm = {
  work_type: WorkType;
  rootMode: RootMode;
  rootPath: string;
};
type FormState = {
  watches: WatchForm[];
  routes: RouteForm[];
  base_url: string;
  model: string;
  reasoning_effort: string;
  verbosity: string;
  credentialMode: RootMode;
  apiKey: string;
  apply_policy: "plan_only" | "manual" | "automatic";
};
type ConfigAttempt = {
  state: FormState;
  current?: Config;
  revision: number;
  key: string;
};
type DirectoryTarget = {
  label: string;
  select: (path: string) => void;
};

const emptyState = (): FormState => ({
  watches: [
    {
      watch_id: "primary",
      work_type: "anime",
      poll_interval_seconds: 30,
      settle_interval_seconds: 120,
      rootMode: "replace",
      rootPath: "",
    },
  ],
  routes: [
    {
      work_type: "anime",
      rootMode: "replace",
      rootPath: "",
    },
  ],
  base_url: "https://api.openai.com/v1",
  model: "gpt-5",
  reasoning_effort: "medium",
  verbosity: "medium",
  credentialMode: "replace",
  apiKey: "",
  apply_policy: "plan_only",
});

function fromConfig(config: Config): FormState {
  return {
    watches: config.watches.map((item) => ({
      watch_id: item.watch_id,
      work_type: item.work_type,
      poll_interval_seconds: item.poll_interval_seconds,
      settle_interval_seconds: item.settle_interval_seconds,
      rootMode: "retain",
      rootPath: "",
    })),
    routes: config.archive_routes.map((item) => ({
      work_type: item.work_type,
      rootMode: "retain",
      rootPath: "",
    })),
    base_url: config.provider.base_url,
    model: config.provider.model,
    reasoning_effort: config.provider.reasoning_effort ?? "medium",
    verbosity: config.provider.verbosity ?? "medium",
    credentialMode: "retain",
    apiKey: "",
    apply_policy: config.apply_policy,
  };
}

export function ConfigPage() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["config"],
    queryFn: () => api.request("/api/v1/admin/config", configSchema),
    retry: false,
  });
  const noConfig =
    query.error instanceof ApiError && query.error.status === 404;
  const [form, setForm] = useState<FormState>(emptyState);
  const [loadedRevision, setLoadedRevision] = useState<number | null>(null);
  const [notice, setNotice] = useState("");
  const [uncertainAttempt, setUncertainAttempt] =
    useState<ConfigAttempt | null>(null);
  const [resyncing, setResyncing] = useState(false);
  const [directoryTarget, setDirectoryTarget] =
    useState<DirectoryTarget | null>(null);

  useEffect(() => {
    if (query.data && loadedRevision !== query.data.revision) {
      setForm(fromConfig(query.data));
      setLoadedRevision(query.data.revision);
    } else if (noConfig && loadedRevision === null) {
      setForm(emptyState());
      setLoadedRevision(0);
    }
  }, [loadedRevision, noConfig, query.data]);

  const save = useMutation({
    mutationFn: async ({
      state,
      current,
      revision,
      key,
    }: ConfigAttempt) => {
      return api.request("/api/v1/admin/config", configSchema, {
        method: "PUT",
        headers: {
          "If-Match": String(revision),
          "Idempotency-Key": key,
        },
        body: toPayload(state, current),
      });
    },
    onSuccess: (result) => {
      setUncertainAttempt(null);
      queryClient.setQueryData(["config"], result);
      setLoadedRevision(result.revision);
      setForm(fromConfig(result));
      setNotice(`配置 revision ${result.revision} 已保存。`);
    },
    onError: async (error, attempt) => {
      if (error instanceof ApiError && error.status === 409) {
        setNotice("配置已被其他管理员修改。请重新加载后再编辑。");
        setUncertainAttempt(null);
        await queryClient.invalidateQueries({ queryKey: ["config"] });
      } else if (error instanceof ApiError && error.code === "network_uncertain") {
        setNotice(
          "保存结果不确定，已从服务端重新读取；如需重放只会复用原请求键。",
        );
        setUncertainAttempt(attempt);
        setResyncing(true);
        try {
          await queryClient.invalidateQueries({ queryKey: ["config"] });
        } finally {
          setResyncing(false);
        }
      } else {
        setUncertainAttempt(null);
      }
    },
  });

  const probe = useMutation({
    mutationFn: () =>
      api.request(
        "/api/v1/admin/config/provider-probe",
        z
          .object({
            available: z.boolean(),
            status_code: z.number().int().nullable(),
          })
          .strict(),
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey() },
          body: {},
        },
      ),
    onSuccess: (result) =>
      setNotice(result.available ? "Provider 连接正常。" : "Provider 当前不可用。"),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setNotice("");
    if (uncertainAttempt) return;
    save.mutate({
      state: form,
      current: query.data,
      revision: query.data?.revision ?? 0,
      key: idempotencyKey(),
    });
  };

  if (query.isLoading && loadedRevision === null) {
    return <main className="page"><p>正在读取配置…</p></main>;
  }

  return (
    <main className="page config-page">
      <section className="hero-row compact">
        <div>
          <p className="eyebrow">ADMIN CONFIGURATION</p>
          <h1>{noConfig ? "建立第一份安全配置" : "配置控制面"}</h1>
          <p className="lede">
            已配置路径与密钥默认保留。只有明确选择“替换”才需要重新输入。
          </p>
        </div>
        {query.data ? (
          <div className="revision-badge">
            <span>当前 revision</span>
            <strong>{query.data.revision}</strong>
          </div>
        ) : null}
      </section>
      {query.error instanceof ApiError && !noConfig ? (
        <PageError code={query.error.code} />
      ) : null}
      {notice ? <div className="notice" role="status">{notice}</div> : null}
      {uncertainAttempt ? (
        <div className="notice" role="status">
          <p>新保存已锁定，直到原请求得到确定结果。</p>
          <button
            type="button"
            className="secondary"
            disabled={resyncing || save.isPending}
            onClick={() => save.mutate(uncertainAttempt)}
          >
            {resyncing ? "正在读取 durable config…" : "使用原请求安全重放"}
          </button>
        </div>
      ) : null}
      {save.error instanceof ApiError && save.error.status !== 409 ? (
        <PageError code={save.error.code} />
      ) : null}

      <form onSubmit={submit}>
        <ConfigSection number="01" title="监听目录">
          <p className="section-help">
            只处理此目录的直接子文件夹；archive、fail 和隐藏目录会被忽略。
            两个保留目录由 Reeloom 自动管理，scanner 不跟随符号链接。
          </p>
          {form.watches.map((watch, index) => (
            <div className="form-card" key={index}>
              <div className="form-grid">
                <Field label="Watch ID">
                  <input
                    value={watch.watch_id}
                    onChange={(event) =>
                      updateWatch(index, "watch_id", event.target.value)
                    }
                    required
                  />
                </Field>
                <Field label="内容类型">
                  <select
                    value={watch.work_type}
                    onChange={(event) =>
                      updateWatch(
                        index,
                        "work_type",
                        event.target.value as WorkType,
                      )
                    }
                  >
                    {workTypes.map((value) => (
                      <option value={value} key={value}>
                        {workTypeLabel(value)}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="轮询间隔（秒）">
                  <input
                    type="number"
                    min={1}
                    max={86400}
                    value={watch.poll_interval_seconds}
                    onChange={(event) =>
                      updateWatch(
                        index,
                        "poll_interval_seconds",
                        Number(event.target.value),
                      )
                    }
                  />
                </Field>
                <Field label="稳定等待（秒）">
                  <input
                    type="number"
                    min={1}
                    max={604800}
                    value={watch.settle_interval_seconds}
                    onChange={(event) =>
                      updateWatch(
                        index,
                        "settle_interval_seconds",
                        Number(event.target.value),
                      )
                    }
                  />
                </Field>
              </div>
              <SecretChoice
                label="入站目录"
                mode={watch.rootMode}
                canRetain={query.data?.watches.some(
                  (item) =>
                    item.watch_id === watch.watch_id &&
                    item.work_type === watch.work_type,
                ) ?? false}
                value={watch.rootPath}
                onMode={(mode) => updateWatch(index, "rootMode", mode)}
                onValue={(value) => updateWatch(index, "rootPath", value)}
                onBrowse={() =>
                  setDirectoryTarget({
                    label: "入站目录",
                    select: (path) => updateWatch(index, "rootPath", path),
                  })
                }
                placeholder="/media/incoming/anime"
              />
              <button
                type="button"
                className="text-button danger-text"
                onClick={() =>
                  setForm((current) => ({
                    ...current,
                    watches: current.watches.filter((_, item) => item !== index),
                  }))
                }
              >
                移除此监听
              </button>
            </div>
          ))}
          <button
            type="button"
            className="secondary"
            onClick={() =>
              setForm((current) => ({
                ...current,
                watches: [
                  ...current.watches,
                  {
                    watch_id: `watch-${current.watches.length + 1}`,
                    work_type: "anime",
                    poll_interval_seconds: 30,
                    settle_interval_seconds: 120,
                    rootMode: "replace",
                    rootPath: "",
                  },
                ],
              }))
            }
          >
            添加监听
          </button>
        </ConfigSection>

        <ConfigSection number="02" title="媒体库路由">
          <p className="section-help">
            每种内容类型只能配置一个最终媒体库；它不同于入站目录内保存残留文件的 archive。
          </p>
          {form.routes.map((route, index) => (
            <div className="form-card" key={index}>
              <Field label="内容类型">
                <select
                  value={route.work_type}
                  onChange={(event) =>
                    updateRoute(
                      index,
                      "work_type",
                      event.target.value as WorkType,
                    )
                  }
                >
                  {workTypes.map((value) => (
                    <option value={value} key={value}>
                      {workTypeLabel(value)}
                    </option>
                  ))}
                </select>
              </Field>
              <SecretChoice
                label="媒体库目录"
                mode={route.rootMode}
                canRetain={query.data?.archive_routes.some(
                  (item) => item.work_type === route.work_type,
                ) ?? false}
                value={route.rootPath}
                onMode={(mode) => updateRoute(index, "rootMode", mode)}
                onValue={(value) => updateRoute(index, "rootPath", value)}
                onBrowse={() =>
                  setDirectoryTarget({
                    label: "媒体库目录",
                    select: (path) => updateRoute(index, "rootPath", path),
                  })
                }
                placeholder="/media/library/anime"
              />
              <button
                type="button"
                className="text-button danger-text"
                onClick={() =>
                  setForm((current) => ({
                    ...current,
                    routes: current.routes.filter((_, item) => item !== index),
                  }))
                }
              >
                移除此路由
              </button>
            </div>
          ))}
          <button
            type="button"
            className="secondary"
            disabled={workTypes.every((value) =>
              form.routes.some((item) => item.work_type === value),
            )}
            onClick={() =>
              setForm((current) => ({
                ...current,
                routes: [
                  ...current.routes,
                  {
                    work_type:
                      workTypes.find(
                        (value) =>
                          !current.routes.some(
                            (item) => item.work_type === value,
                          ),
                      ) ?? "anime",
                    rootMode: "replace",
                    rootPath: "",
                  },
                ],
              }))
            }
          >
            添加路由
          </button>
        </ConfigSection>

        <ConfigSection number="03" title="模型 Provider">
          <div className="form-grid">
            <Field label="Base URL">
              <input
                type="url"
                value={form.base_url}
                onChange={(event) =>
                  setForm({ ...form, base_url: event.target.value })
                }
                required
              />
            </Field>
            <Field label="模型">
              <input
                value={form.model}
                onChange={(event) =>
                  setForm({ ...form, model: event.target.value })
                }
                required
              />
            </Field>
            <Field label="推理强度">
              <select
                value={form.reasoning_effort}
                onChange={(event) =>
                  setForm({ ...form, reasoning_effort: event.target.value })
                }
              >
                {["none", "minimal", "low", "medium", "high", "xhigh", "max"].map(
                  (value) => <option key={value}>{value}</option>,
                )}
              </select>
            </Field>
            <Field label="输出详细度">
              <select
                value={form.verbosity}
                onChange={(event) =>
                  setForm({ ...form, verbosity: event.target.value })
                }
              >
                {["low", "medium", "high"].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
          </div>
          <SecretChoice
            label="API key"
            secret
            mode={form.credentialMode}
            canRetain={query.data?.provider.api_key_configured ?? false}
            value={form.apiKey}
            onMode={(credentialMode) =>
              setForm({ ...form, credentialMode, apiKey: "" })
            }
            onValue={(apiKey) => setForm({ ...form, apiKey })}
            placeholder="输入新的 API key"
          />
          <button
            className="secondary"
            type="button"
            disabled={probe.isPending || noConfig}
            onClick={() => probe.mutate()}
          >
            {probe.isPending ? "正在探测…" : "探测当前 Provider"}
          </button>
        </ConfigSection>

        <ConfigSection number="04" title="执行策略">
          <div className="policy-grid">
            {[
              ["plan_only", "仅计划", "永不执行文件移动。"],
              ["manual", "人工审批", "每次执行都必须由管理员确认 exact hash。"],
              ["automatic", "确定性自动审批", "仅后台 policy 可发起自动审批；UI 仍只人工提交。"],
            ].map(([value, title, detail]) => (
              <label className="policy-card" key={value}>
                <input
                  type="radio"
                  name="apply-policy"
                  value={value}
                  checked={form.apply_policy === value}
                  onChange={() =>
                    setForm({
                      ...form,
                      apply_policy: value as FormState["apply_policy"],
                    })
                  }
                />
                <strong>{title}</strong>
                <span>{detail}</span>
              </label>
            ))}
          </div>
        </ConfigSection>

        <div className="sticky-actions">
          <span>
            保存使用 CAS；冲突时必须重新加载，不会自动覆盖。
          </span>
          <button
            className="primary"
            disabled={save.isPending || resyncing || uncertainAttempt !== null}
          >
            {save.isPending ? "正在保存…" : "保存配置"}
          </button>
        </div>
      </form>
      {directoryTarget ? (
        <DirectoryPicker
          api={api}
          label={directoryTarget.label}
          onClose={() => setDirectoryTarget(null)}
          onSelect={(path) => {
            directoryTarget.select(path);
            setDirectoryTarget(null);
          }}
        />
      ) : null}
    </main>
  );

  function updateWatch<K extends keyof WatchForm>(
    index: number,
    key: K,
    value: WatchForm[K],
  ) {
    setForm((current) => ({
      ...current,
      watches: current.watches.map((item, itemIndex) =>
        itemIndex === index ? { ...item, [key]: value } : item,
      ),
    }));
  }

  function updateRoute<K extends keyof RouteForm>(
    index: number,
    key: K,
    value: RouteForm[K],
  ) {
    setForm((current) => ({
      ...current,
      routes: current.routes.map((item, itemIndex) =>
        itemIndex === index ? { ...item, [key]: value } : item,
      ),
    }));
  }
}

export function toPayload(state: FormState, current?: Config) {
  const root = (mode: RootMode, path: string, canRetain: boolean) =>
    mode === "retain" && canRetain
      ? { mode: "retain" as const }
      : { mode: "replace" as const, path };
  return {
    apply_policy: state.apply_policy,
    watches: state.watches.map((item) => ({
      watch_id: item.watch_id,
      work_type: item.work_type,
      poll_interval_seconds: item.poll_interval_seconds,
      settle_interval_seconds: item.settle_interval_seconds,
      root: root(
        item.rootMode,
        item.rootPath,
        current === undefined ||
          current.watches.some(
            (configured) =>
              configured.watch_id === item.watch_id &&
              configured.work_type === item.work_type &&
              configured.root_configured,
          ),
      ),
    })),
    archive_routes: state.routes.map((item) => ({
      work_type: item.work_type,
      root: root(
        item.rootMode,
        item.rootPath,
        current === undefined ||
          current.archive_routes.some(
            (configured) =>
              configured.work_type === item.work_type &&
              configured.root_configured,
          ),
      ),
    })),
    provider: {
      base_url: state.base_url,
      model: state.model,
      reasoning_effort: state.reasoning_effort,
      verbosity: state.verbosity,
      credential:
        state.credentialMode === "retain" &&
        (current === undefined || current.provider.api_key_configured)
          ? { mode: "retain" as const }
          : { mode: "replace" as const, api_key: state.apiKey },
    },
  };
}

function ConfigSection({
  number,
  title,
  children,
}: {
  number: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="config-section">
      <div className="section-number">{number}</div>
      <div className="section-content">
        <h2>{title}</h2>
        {children}
      </div>
    </section>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function SecretChoice({
  label,
  mode,
  canRetain,
  value,
  onMode,
  onValue,
  onBrowse,
  placeholder,
  secret = false,
}: {
  label: string;
  mode: RootMode;
  canRetain: boolean;
  value: string;
  onMode: (mode: RootMode) => void;
  onValue: (value: string) => void;
  onBrowse?: () => void;
  placeholder: string;
  secret?: boolean;
}) {
  const effectiveMode = canRetain ? mode : "replace";
  return (
    <fieldset className="secret-choice">
      <legend>{label}</legend>
      {canRetain ? (
        <div className="segmented">
          <button
            type="button"
            className={effectiveMode === "retain" ? "selected" : ""}
            onClick={() => onMode("retain")}
          >
            保留现有值
          </button>
          <button
            type="button"
            className={effectiveMode === "replace" ? "selected" : ""}
            onClick={() => onMode("replace")}
          >
            替换
          </button>
        </div>
      ) : (
        <p className="field-note">新项目必须提供明确值。</p>
      )}
      {effectiveMode === "replace" ? (
        <div className="path-input-row">
          <input
            type={secret ? "password" : "text"}
            autoComplete="off"
            value={value}
            onChange={(event) => onValue(event.target.value)}
            placeholder={placeholder}
            required
          />
          {onBrowse ? (
            <button
              type="button"
              className="secondary"
              onClick={onBrowse}
              aria-label={`浏览${label}`}
            >
              浏览
            </button>
          ) : null}
        </div>
      ) : (
        <p className="retained-value">已配置 · 内容不会回传浏览器</p>
      )}
    </fieldset>
  );
}

function DirectoryPicker({
  api,
  label,
  onClose,
  onSelect,
}: {
  api: ApiClient;
  label: string;
  onClose: () => void;
  onSelect: (path: string) => void;
}) {
  const [path, setPath] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const query = useQuery({
    queryKey: ["directories", path],
    queryFn: () => {
      const search = new URLSearchParams({ path });
      return api.request(
        `/api/v1/admin/directories?${search.toString()}`,
        directoryListingSchema,
      );
    },
    retry: false,
  });

  useEffect(() => {
    const previousFocus = document.activeElement;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, [onClose]);

  return (
    <div className="modal-backdrop">
      <div
        ref={dialogRef}
        className="modal directory-picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby="directory-picker-title"
      >
        <div className="directory-picker-heading">
          <div>
            <p className="eyebrow">POD DIRECTORIES</p>
            <h2 id="directory-picker-title">选择{label}</h2>
          </div>
          <button type="button" className="ghost" onClick={onClose} autoFocus>
            关闭
          </button>
        </div>
        <p className="directory-current">
          {query.data?.absolute_path ?? "正在读取目录…"}
        </p>
        <div className="button-row">
          <button
            type="button"
            className="secondary"
            disabled={query.isFetching || query.data?.parent === null}
            onClick={() => setPath(query.data?.parent ?? "")}
          >
            上一级
          </button>
          <button
            type="button"
            className="primary"
            disabled={!query.data || query.isFetching}
            onClick={() => query.data && onSelect(query.data.absolute_path)}
          >
            选择当前目录
          </button>
        </div>
        {query.error instanceof ApiError ? (
          <PageError code={query.error.code} />
        ) : null}
        <div className="directory-list" aria-label="子目录">
          {query.data?.directories.map((directory) => (
            <button
              type="button"
              key={directory.path}
              onClick={() => setPath(directory.path)}
            >
              <span aria-hidden="true">▸</span>
              <span>{directory.name}</span>
            </button>
          ))}
          {query.data?.directories.length === 0 ? (
            <p className="muted">当前目录没有可浏览的子目录。</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}
