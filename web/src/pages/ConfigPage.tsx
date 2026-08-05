import { useEffect, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { ApiError, idempotencyKey, type ApiClient } from "../api";
import { useAuth } from "../auth";
import { Hint } from "../components/Hint";
import { IconFolder, IconPlus, IconTrash } from "../components/Icon";
import { errorMessage, PageError } from "../components/Status";
import { compactValue } from "../labels";
import {
  configSchema,
  directoryListingSchema,
  moveCapabilitySchema,
  type Config,
} from "../schemas";
import {
  workTypeLabel,
  workTypes,
  type WorkType,
} from "../workTypes";

type RootMode = "retain" | "replace";
type TelegramDestinationMode = RootMode | "unset";
type TelegramNotificationType =
  | "plan_ready"
  | "archive_completed"
  | "attention_required";
type WatchForm = {
  watch_id: string;
  work_type: WorkType;
  poll_interval_seconds: number;
  settle_interval_seconds: number;
  rootMode: RootMode;
  rootPath: string;
  libraryRootMode: RootMode;
  libraryRootPath: string;
};
type FormState = {
  watches: WatchForm[];
  base_url: string;
  model: string;
  reasoning_effort: string;
  verbosity: string;
  credentialMode: RootMode;
  apiKey: string;
  telegramEnabled: boolean;
  telegramTypes: TelegramNotificationType[];
  telegramDestinationMode: TelegramDestinationMode;
  telegramBotToken: string;
  telegramChatId: string;
  apply_policy: "plan_only" | "manual" | "automatic";
  acgripEnabled: boolean;
  subtitle_acquisition_policy: "plan_only" | "manual" | "automatic";
  agent_budget: {
    max_model_turns: number;
    max_tool_calls: number;
    max_failures: number;
    max_total_tokens: number;
    max_elapsed_seconds: number;
  };
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

const newWatch = (): WatchForm => ({
  watch_id: `watch-${globalThis.crypto.randomUUID()}`,
  work_type: "anime",
  poll_interval_seconds: 30,
  settle_interval_seconds: 120,
  rootMode: "replace",
  rootPath: "",
  libraryRootMode: "replace",
  libraryRootPath: "",
});

const emptyState = (): FormState => ({
  watches: [newWatch()],
  base_url: "https://api.openai.com/v1",
  model: "gpt-5",
  reasoning_effort: "medium",
  verbosity: "medium",
  credentialMode: "replace",
  apiKey: "",
  telegramEnabled: false,
  telegramTypes: [
    "plan_ready",
    "archive_completed",
    "attention_required",
  ],
  telegramDestinationMode: "unset",
  telegramBotToken: "",
  telegramChatId: "",
  apply_policy: "plan_only",
  acgripEnabled: false,
  subtitle_acquisition_policy: "automatic",
  agent_budget: {
    max_model_turns: 64,
    max_tool_calls: 64,
    max_failures: 16,
    max_total_tokens: 100_000,
    max_elapsed_seconds: 600,
  },
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
      libraryRootMode: "retain",
      libraryRootPath: "",
    })),
    base_url: config.provider.base_url,
    model: config.provider.model,
    reasoning_effort: config.provider.reasoning_effort ?? "medium",
    verbosity: config.provider.verbosity ?? "medium",
    credentialMode: "retain",
    apiKey: "",
    telegramEnabled: config.telegram.enabled,
    telegramTypes: config.telegram.notification_types,
    telegramDestinationMode: config.telegram.destination_configured
      ? "retain"
      : "unset",
    telegramBotToken: "",
    telegramChatId: "",
    apply_policy: config.apply_policy,
    acgripEnabled: config.acgrip.enabled,
    subtitle_acquisition_policy: config.subtitle_acquisition_policy,
    agent_budget: config.agent_budget,
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
  const [providerNotice, setProviderNotice] = useState("");
  const [telegramNotice, setTelegramNotice] = useState("");
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
      setNotice(`配置版本 ${result.revision} 已保存。`);
    },
    onError: async (error, attempt) => {
      if (error instanceof ApiError && error.status === 409) {
        setNotice("配置已被其他管理员修改。请重新加载后再编辑。");
        setUncertainAttempt(null);
        await queryClient.invalidateQueries({ queryKey: ["config"] });
      } else if (error instanceof ApiError && error.code === "network_uncertain") {
        setNotice(
          "保存结果不确定，已从服务端重新读取；如需重试只会复用原请求键。",
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
    onMutate: () => setProviderNotice(""),
    onSuccess: (result) =>
      setProviderNotice(
        result.available ? "Provider 连接正常。" : "Provider 当前不可用。",
      ),
  });

  const moveProbe = useMutation({
    mutationFn: (watchId: string) =>
      api.request(
        `/api/v1/admin/watches/${encodeURIComponent(watchId)}/move-capability-probe`,
        moveCapabilitySchema,
        { method: "POST", body: {} },
      ),
  });

  const telegramTest = useMutation({
    mutationFn: () =>
      api.request(
        "/api/v1/admin/config/telegram-test",
        z
          .object({
            notification_id: z.string(),
            state: z.literal("queued"),
          })
          .strict(),
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey() },
          body: {},
        },
      ),
    onMutate: () => setTelegramNotice(""),
    onSuccess: () => setTelegramNotice("测试通知已加入发送队列。"),
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
        <div className="hero-title">
          <h1>{noConfig ? "建立第一份安全配置" : "配置"}</h1>
          <Hint label="配置说明">
            已配置的路径与密钥默认保留，只有点“替换”才需要重新输入。
            保存使用乐观并发校验：若他人已改动，必须重新加载，不会自动覆盖。
          </Hint>
        </div>
      </section>
      {query.error instanceof ApiError && !noConfig ? (
        <PageError code={query.error.code} />
      ) : null}
      {notice ? <div className="notice" role="status">{notice}</div> : null}
      {uncertainAttempt ? (
        <div className="notice" role="status">
          <p>保存已锁定，直到上一次请求得到确定结果。</p>
          <button
            type="button"
            className="secondary"
            disabled={resyncing || save.isPending}
            onClick={() => save.mutate(uncertainAttempt)}
          >
            {resyncing ? "正在读取服务端配置…" : "使用原请求安全重试"}
          </button>
        </div>
      ) : null}
      {save.error instanceof ApiError && save.error.status !== 409 ? (
        <PageError code={save.error.code} />
      ) : null}

      <form onSubmit={submit}>
        <ConfigSection
          title="监听与媒体库"
          hint={
            <>
              只处理入站目录的直接子文件夹；archive、fail 和隐藏目录会被忽略。
              archive 和 fail 由 Reeloom 在入站目录内管理，不等同于媒体库目录。
            </>
          }
        >
          {form.watches.map((watch, index) => (
            <div className="form-card" key={watch.watch_id}>
              <div className="form-card-heading">
                <strong>
                  监听 {index + 1} · {workTypeLabel(watch.work_type)}
                </strong>
                <code title={watch.watch_id}>
                  {compactValue(watch.watch_id)}
                </code>
              </div>
              <div className="form-grid">
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
                canRetain={existingWatch(watch) !== undefined}
                retainedValue={existingWatch(watch)?.root}
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
              <SecretChoice
                label="媒体库目录"
                mode={watch.libraryRootMode}
                canRetain={existingWatch(watch) !== undefined}
                retainedValue={existingWatch(watch)?.library_root}
                value={watch.libraryRootPath}
                onMode={(mode) => updateWatch(index, "libraryRootMode", mode)}
                onValue={(value) =>
                  updateWatch(index, "libraryRootPath", value)
                }
                onBrowse={() =>
                  setDirectoryTarget({
                    label: "媒体库目录",
                    select: (path) =>
                      updateWatch(index, "libraryRootPath", path),
                  })
                }
                placeholder="/media/library/anime"
              />
              <button
                type="button"
                className="secondary"
                disabled={
                  existingWatch(watch) === undefined ||
                  moveProbe.isPending
                }
                onClick={() => moveProbe.mutate(watch.watch_id)}
              >
                {moveProbe.isPending &&
                moveProbe.variables === watch.watch_id
                  ? "正在检测移动能力…"
                  : "检测移动兼容性"}
              </button>
              {moveProbe.data?.watch_id === watch.watch_id ? (
                <div
                  className={moveCapabilityClass(
                    moveProbe.data.folder_disposition.status,
                    moveProbe.data.media_apply.status,
                  )}
                  role="status"
                >
                  文件夹收尾：
                  {moveCapabilityLabel(
                    moveProbe.data.folder_disposition.status,
                  )}
                  {moveProbe.data.folder_disposition.failure_code ? (
                    <>
                      {" "}
                      {errorMessage(
                        moveProbe.data.folder_disposition.failure_code,
                      )}{" "}
                      <code>
                        {moveProbe.data.folder_disposition.failure_code}
                      </code>
                    </>
                  ) : null}
                  ；媒体执行：
                  {moveCapabilityLabel(moveProbe.data.media_apply.status)}
                  {moveProbe.data.media_apply.failure_code ? (
                    <>
                      {" "}
                      {errorMessage(
                        moveProbe.data.media_apply.failure_code,
                      )}{" "}
                      <code>
                        {moveProbe.data.media_apply.failure_code}
                      </code>
                    </>
                  ) : null}
                </div>
              ) : null}
              {moveProbe.error instanceof ApiError &&
              moveProbe.variables === watch.watch_id ? (
                <PageError code={moveProbe.error.code} />
              ) : null}
              <div className="form-card-footer">
                <button
                  type="button"
                  className="text-button danger-text"
                  onClick={() =>
                    setForm((current) => ({
                      ...current,
                      watches: current.watches.filter(
                        (_, item) => item !== index,
                      ),
                    }))
                  }
                >
                  <IconTrash size={14} />
                  移除此监听
                </button>
              </div>
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
                  newWatch(),
                ],
              }))
            }
          >
            <IconPlus size={15} />
            添加监听
          </button>
        </ConfigSection>

        <ConfigSection
          title="Telegram 通知"
          hint={
            <>
              仅向固定 Chat ID 单向推送，不接受命令或审批。Bot Token 与 Chat ID
              不会回传浏览器；网络失败由 PostgreSQL 队列恢复。
            </>
          }
        >
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={form.telegramEnabled}
              onChange={(event) =>
                setForm({ ...form, telegramEnabled: event.target.checked })
              }
            />
            <span>启用 Telegram 推送</span>
          </label>
          <fieldset className="secret-choice">
            <legend>推送类型</legend>
            {[
              ["plan_ready", "计划待批准"],
              ["archive_completed", "整理完成"],
              ["attention_required", "需要处理"],
            ].map(([value, label]) => (
              <label className="toggle-row" key={value}>
                <input
                  type="checkbox"
                  checked={form.telegramTypes.includes(
                    value as TelegramNotificationType,
                  )}
                  disabled={
                    form.telegramTypes.length === 1 &&
                    form.telegramTypes.includes(
                      value as TelegramNotificationType,
                    )
                  }
                  onChange={(event) =>
                    setForm({
                      ...form,
                      telegramTypes: event.target.checked
                        ? [...form.telegramTypes, value as TelegramNotificationType]
                        : form.telegramTypes.filter((item) => item !== value),
                    })
                  }
                />
                <span>{label}</span>
              </label>
            ))}
          </fieldset>
          {form.telegramDestinationMode === "retain" ? (
            <div className="secret-row">
              <p className="retained-value">
                目标与 Bot Token 已配置 · 内容不会回传浏览器
              </p>
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  setForm({
                    ...form,
                    telegramDestinationMode: "replace",
                  })
                }
              >
                替换
              </button>
            </div>
          ) : (
            <div className="form-grid">
              <Field label="Bot Token">
                <input
                  type="password"
                  autoComplete="off"
                  value={form.telegramBotToken}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      telegramDestinationMode: "replace",
                      telegramBotToken: event.target.value,
                    })
                  }
                  required={form.telegramEnabled}
                  placeholder="123456789:…"
                />
              </Field>
              <Field label="Chat ID">
                <input
                  value={form.telegramChatId}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      telegramDestinationMode: "replace",
                      telegramChatId: event.target.value,
                    })
                  }
                  required={form.telegramEnabled}
                  placeholder="-1001234567890"
                />
              </Field>
            </div>
          )}
          <button
            className="secondary"
            type="button"
            disabled={
              telegramTest.isPending ||
              !query.data?.telegram.destination_configured
            }
            onClick={() => telegramTest.mutate()}
          >
            {telegramTest.isPending ? "正在入队…" : "发送测试通知"}
          </button>
          {telegramNotice ? (
            <div className="notice" role="status">{telegramNotice}</div>
          ) : null}
          {telegramTest.error instanceof ApiError ? (
            <PageError code={telegramTest.error.code} />
          ) : null}
        </ConfigSection>

        <ConfigSection title="模型 Provider">
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
          {providerNotice ? (
            <div className="notice" role="status">{providerNotice}</div>
          ) : null}
          {probe.error instanceof ApiError ? (
            <PageError code={probe.error.code} />
          ) : null}
        </ConfigSection>

        <ConfigSection
          title="动漫字幕自动获取"
          hint={
            <>
              启用后仅对动漫运行访问固定的 ACG.RIP 字幕版块。站点搜索、帖子和
              原生附件是唯一允许的网络范围；不使用登录、验证码绕过、外链或代理。
              字幕选择由 Agent 完成，下载与发布始终经过独立不可变计划和审批。
            </>
          }
        >
          <div className="subtitle-provider-row">
            <div className="field">
              <span>自动字幕获取</span>
              <button
                type="button"
                className={`subtitle-feature-button${
                  form.acgripEnabled ? " enabled" : ""
                }`}
                aria-pressed={form.acgripEnabled}
                onClick={() =>
                  setForm({
                    ...form,
                    acgripEnabled: !form.acgripEnabled,
                  })
                }
              >
                {form.acgripEnabled ? "已启用" : "启用"}
              </button>
            </div>
            <Field label="字幕来源">
              <select
                value="acgrip"
                disabled={!form.acgripEnabled}
                aria-label="字幕来源"
                onChange={() => undefined}
              >
                <option value="acgrip">ACG.RIP 动漫字幕论坛</option>
              </select>
            </Field>
          </div>
          <fieldset
            className="subtitle-policy-choice"
            disabled={!form.acgripEnabled}
          >
            <legend>字幕获取审批策略</legend>
            <div className="segmented-choice">
              {[
                ["plan_only", "仅生成计划"],
                ["manual", "人工审批"],
                ["automatic", "自动审批"],
              ].map(([value, label]) => (
                <label className="segmented-option" key={value}>
                  <input
                    type="radio"
                    name="subtitle-acquisition-policy"
                    value={value}
                    checked={form.subtitle_acquisition_policy === value}
                    onChange={() =>
                      setForm({
                        ...form,
                        subtitle_acquisition_policy:
                          value as FormState["subtitle_acquisition_policy"],
                      })
                    }
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </fieldset>
        </ConfigSection>

        <ConfigSection
          title="Agent 预算"
          hint={
            <>
              每个新运行固定使用保存时的累计预算；每次问答或修订会获得新的单次时间窗口。
              修改配置不会改变已创建的任务，任一限制触发都会安全停止。
            </>
          }
        >
          <div className="form-grid">
            <BudgetField
              label="单次操作时间上限（秒）"
              value={form.agent_budget.max_elapsed_seconds}
              min={1}
              max={3600}
              onChange={(max_elapsed_seconds) =>
                updateBudget({ max_elapsed_seconds })
              }
            />
            <BudgetField
              label="Token 总上限"
              value={form.agent_budget.max_total_tokens}
              min={1}
              max={10_000_000}
              onChange={(max_total_tokens) =>
                updateBudget({ max_total_tokens })
              }
            />
            <BudgetField
              label="模型轮次上限"
              value={form.agent_budget.max_model_turns}
              min={1}
              max={1024}
              onChange={(max_model_turns) =>
                updateBudget({ max_model_turns })
              }
            />
            <BudgetField
              label="工具调用上限"
              value={form.agent_budget.max_tool_calls}
              min={1}
              max={4096}
              onChange={(max_tool_calls) =>
                updateBudget({ max_tool_calls })
              }
            />
            <BudgetField
              label="失败次数上限"
              value={form.agent_budget.max_failures}
              min={1}
              max={100}
              onChange={(max_failures) =>
                updateBudget({ max_failures })
              }
            />
          </div>
        </ConfigSection>

        <ConfigSection title="执行策略">
          <div className="policy-grid">
            {[
              ["plan_only", "仅计划", "永不移动文件。"],
              ["manual", "人工审批", "每次执行都要管理员确认计划哈希。"],
              ["automatic", "确定性自动审批", "仅后台策略可自动审批；界面仍需人工提交。"],
            ].map(([value, title, detail]) => (
              <label className="policy-card" key={value}>
                <span className="policy-card-title">
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
                </span>
                <span>{detail}</span>
              </label>
            ))}
          </div>
        </ConfigSection>

        <div className="sticky-actions">
          <span>
            {query.data ? `当前版本 ${query.data.revision}` : "尚未保存过配置"}
          </span>
          <div className="sticky-actions-buttons">
            <button
              type="button"
              className="ghost on-dark"
              disabled={save.isPending || resyncing}
              onClick={() => {
                setNotice("");
                setForm(query.data ? fromConfig(query.data) : emptyState());
              }}
            >
              放弃修改
            </button>
            <button
              className="primary"
              disabled={
                save.isPending || resyncing || uncertainAttempt !== null
              }
            >
              {save.isPending ? "正在保存…" : "保存配置"}
            </button>
          </div>
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

  function existingWatch(watch: WatchForm) {
    return query.data?.watches.find(
      (item) =>
        item.watch_id === watch.watch_id &&
        item.work_type === watch.work_type,
    );
  }

  function updateBudget(
    value: Partial<FormState["agent_budget"]>,
  ) {
    setForm((current) => ({
      ...current,
      agent_budget: { ...current.agent_budget, ...value },
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
    acgrip: { enabled: state.acgripEnabled },
    subtitle_acquisition_policy: state.subtitle_acquisition_policy,
    agent_budget: state.agent_budget,
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
              configured.work_type === item.work_type,
          ),
      ),
      library_root: root(
        item.libraryRootMode,
        item.libraryRootPath,
        current === undefined ||
          current.watches.some(
            (configured) =>
              configured.watch_id === item.watch_id &&
              configured.work_type === item.work_type,
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
    telegram: {
      enabled: state.telegramEnabled,
      notification_types: state.telegramTypes,
      destination:
        state.telegramDestinationMode === "retain" &&
        current?.telegram.destination_configured === true
          ? { mode: "retain" as const }
          : state.telegramDestinationMode === "unset" &&
              !state.telegramEnabled &&
              !state.telegramBotToken &&
              !state.telegramChatId
            ? { mode: "unset" as const }
            : {
                mode: "replace" as const,
                bot_token: state.telegramBotToken,
                chat_id: state.telegramChatId,
              },
    },
  };
}

function BudgetField({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        min={min}
        max={max}
        step={1}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        required
      />
    </Field>
  );
}

function ConfigSection({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="config-section">
      <div className="section-heading">
        <h2>{title}</h2>
        {hint ? <Hint label={`${title}说明`}>{hint}</Hint> : null}
      </div>
      {children}
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
  retainedValue,
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
  retainedValue?: string;
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
      {effectiveMode === "retain" ? (
        <div className="secret-row">
          <p className="retained-value">
            {retainedValue || value || "已配置 · 内容不会回传浏览器"}
          </p>
          <button
            type="button"
            className="text-button"
            onClick={() => onMode("replace")}
          >
            替换
          </button>
        </div>
      ) : (
        <>
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
          {canRetain ? (
            <button
              type="button"
              className="text-button"
              onClick={() => onMode("retain")}
            >
              保留现有值
            </button>
          ) : null}
        </>
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
          <h2 id="directory-picker-title">选择{label}</h2>
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
              <span aria-hidden="true">
                <IconFolder size={15} />
              </span>
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

function moveCapabilityLabel(
  status:
    | "supported"
    | "degraded"
    | "unsupported"
    | "cross_filesystem"
    | "uncertain",
) {
  return {
    supported: "支持原子不覆盖移动",
    degraded: "FUSE 降级移动（移动前检查，不保证原子不覆盖）",
    unsupported: "挂载不支持",
    cross_filesystem: "跨文件系统，无法原子移动",
    uncertain: "结果不确定，请保持目录不变",
  }[status];
}

function moveCapabilityUsable(status: string) {
  return status === "supported" || status === "degraded";
}

function moveCapabilityClass(folder: string, media: string) {
  if (!moveCapabilityUsable(folder) || !moveCapabilityUsable(media)) {
    return "notice danger";
  }
  return folder === "degraded" || media === "degraded"
    ? "notice warning"
    : "notice";
}
