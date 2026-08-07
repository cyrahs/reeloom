import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import {
  ApiError,
  byteLength,
  idempotencyKey,
} from "../api";
import { useAuth } from "../auth";
import { Hint } from "../components/Hint";
import {
  IconArrowLeft,
  IconCheck,
  IconCopy,
  IconFilm,
  IconFolder,
  IconMoveTo,
  IconSubtitle,
} from "../components/Icon";
import {
  errorMessage,
  PageError,
  ShortHash,
  Status,
} from "../components/Status";
import { RunDeletionAction } from "../components/RunDeletion";
import { compactRunId, compactValue, statusLabel } from "../labels";
import { HashLink } from "../router";
import {
  applyResultSchema,
  attentionControlResultSchema,
  folderDispositionResultSchema,
  interactionsSchema,
  interactionResultSchema,
  lineageSchema,
  previewSchema,
  reapplyResultSchema,
  recoveryResultSchema,
  runSchema,
  subtitleAcquisitionActionResultSchema,
  type Preview,
  type Run,
  type RunEvent,
} from "../schemas";
import {
  cursorKey,
  readAllEvents,
  streamRunEvents,
} from "../sse";
import { workTypeLabel } from "../workTypes";

type ActionKind = "question" | "revision" | "reapply";
type PreviewExplanationValue = Extract<
  Preview["items"][number],
  { disposition: "unmapped" }
>["explanation"];
type ActionAttempt = {
  kind: ActionKind;
  message: string;
  head: string;
  key: string;
};
type ImmediateInteraction = {
  interaction_id: string;
  kind: ActionKind;
  request_message: string;
  assistant_reply: string;
};
type ApplyAttempt = {
  planHash: string;
  folderDispositionPlanHash: string | null;
  key: string;
};
type RecoveryAttempt = {
  planHash: string;
  approvalId: string;
  key: string;
};
type FolderAttempt = {
  planHash: string;
  approvalId?: string;
  key: string;
};
type SubtitleAttempt = {
  planHash: string;
  key: string;
  mode?: "approve" | "retry" | "fail";
};
type AttentionAttempt = {
  kind: "retry" | "fail";
  eventSequence: number;
  key: string;
};
type UncertainAttempt =
  | { type: "action"; value: ActionAttempt }
  | { type: "apply"; value: ApplyAttempt }
  | { type: "recover"; value: RecoveryAttempt }
  | { type: "subtitle"; value: SubtitleAttempt }
  | { type: "folder"; value: FolderAttempt }
  | { type: "attention"; value: AttentionAttempt };

export function RunPage({ runId }: { runId: string }) {
  const { api, logout } = useAuth();
  const queryClient = useQueryClient();
  const encodedRunId = encodeURIComponent(runId);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamState, setStreamState] = useState("正在同步");
  const [approveOpen, setApproveOpen] = useState(false);
  const [folderConfirmOpen, setFolderConfirmOpen] = useState(false);
  const [actionNotice, setActionNotice] = useState("");
  const [immediateInteraction, setImmediateInteraction] =
    useState<ImmediateInteraction | null>(null);
  const [uncertainAttempt, setUncertainAttempt] =
    useState<UncertainAttempt | null>(null);
  const [resyncing, setResyncing] = useState(false);
  const approveButtonRef = useRef<HTMLButtonElement>(null);
  const closeApprove = () => {
    setApproveOpen(false);
    window.requestAnimationFrame(() => approveButtonRef.current?.focus());
  };

  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.request(`/api/v1/runs/${encodedRunId}`, runSchema),
  });
  const lineage = useQuery({
    queryKey: ["lineage", runId],
    queryFn: () =>
      api.request(
        `/api/v1/runs/${encodedRunId}/plans?limit=100`,
        lineageSchema,
      ),
    enabled: Boolean(run.data?.plan_hash),
  });
  const effectiveVersion =
    selectedVersion ?? lineage.data?.items[0]?.version ?? null;

  const preview = useInfiniteQuery({
    queryKey: ["preview", runId, effectiveVersion],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.request(
        `/api/v1/runs/${encodedRunId}/plans/${effectiveVersion}/preview?after=${pageParam}&limit=100`,
        previewSchema,
      ),
    getNextPageParam: (last) => last.next_after ?? undefined,
    enabled: effectiveVersion !== null,
  });
  const interactions = useInfiniteQuery({
    queryKey: ["interactions", runId],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      api.request(
        `/api/v1/runs/${encodedRunId}/interactions?limit=100${
          pageParam ? `&before=${encodeURIComponent(pageParam)}` : ""
        }`,
        interactionsSchema,
      ),
    getNextPageParam: (last) =>
      last.items.length === 100
        ? last.items.at(-1)?.interaction_id
        : undefined,
  });

  const allPreviewItems =
    preview.data?.pages.flatMap((page) => page.items) ?? [];
  const currentPreview = preview.data?.pages[0];
  const persistedInteractions =
    interactions.data?.pages.flatMap((page) => page.items) ?? [];
  const displayedInteractions =
    immediateInteraction &&
    !persistedInteractions.some(
      (item) => item.interaction_id === immediateInteraction.interaction_id,
    )
      ? [
          {
            ...immediateInteraction,
            status: "completed" as const,
            content_available: true as const,
          },
          ...persistedInteractions,
        ]
      : persistedInteractions;

  useEffect(() => {
    const controller = new AbortController();
    readAllEvents(
      (path, schema) => api.request(path, schema),
      runId,
    )
      .then((initial) => {
        if (controller.signal.aborted) return;
        setEvents(initial);
        const latest = initial.at(-1)?.event_id ?? 0;
        window.localStorage.setItem(cursorKey(runId), String(latest));
        setStreamState("实时连接");
        return streamRunEvents({
          runId,
          token: api.token,
          signal: controller.signal,
          onEvents: (incoming) => {
            setEvents((current) => mergeEvents(current, incoming));
            void Promise.all([
              queryClient.invalidateQueries({ queryKey: ["run", runId] }),
              queryClient.invalidateQueries({ queryKey: ["lineage", runId] }),
              queryClient.invalidateQueries({ queryKey: ["preview", runId] }),
              queryClient.invalidateQueries({
                queryKey: ["interactions", runId],
              }),
            ]);
          },
          onCursorAhead: async () => {
            setStreamState("正在完整重同步");
            const resynced = await readAllEvents(
              (path, schema) => api.request(path, schema),
              runId,
            );
            setEvents(resynced);
            await Promise.all([
              queryClient.invalidateQueries({ queryKey: ["run", runId] }),
              queryClient.invalidateQueries({ queryKey: ["lineage", runId] }),
              queryClient.invalidateQueries({ queryKey: ["preview", runId] }),
              queryClient.invalidateQueries({
                queryKey: ["interactions", runId],
              }),
            ]);
            setStreamState("实时连接");
            return resynced.at(-1)?.event_id ?? 0;
          },
          onUnauthorized: logout,
        });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setStreamState(
            error instanceof ApiError ? `同步失败：${error.code}` : "同步失败",
          );
        }
      });
    return () => controller.abort();
  }, [api, logout, queryClient, runId]);

  const invalidateRun = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["run", runId] }),
      queryClient.invalidateQueries({ queryKey: ["lineage", runId] }),
      queryClient.invalidateQueries({ queryKey: ["preview", runId] }),
      queryClient.invalidateQueries({ queryKey: ["interactions", runId] }),
    ]);
    setSelectedVersion(null);
  };

  const reconcileUncertain = async (attempt: UncertainAttempt) => {
    setUncertainAttempt(attempt);
    setResyncing(true);
    try {
      await invalidateRun();
    } finally {
      setResyncing(false);
    }
  };

  const action = useMutation({
    mutationFn: async ({
      kind,
      message,
      head,
      key,
    }: ActionAttempt) => {
      const headers = {
        "If-Match": head,
        "Idempotency-Key": key,
      };
      if (kind === "reapply") {
        return api.request(
          `/api/v1/runs/${encodedRunId}/reapply`,
          reapplyResultSchema,
          { method: "POST", headers, body: { message } },
        );
      }
      return api.request(
        `/api/v1/runs/${encodedRunId}/interactions`,
        interactionResultSchema,
        { method: "POST", headers, body: { kind, message } },
      );
    },
    onSuccess: async (result, attempt) => {
      setUncertainAttempt(null);
      setImmediateInteraction({
        interaction_id: result.interaction_id,
        kind: attempt.kind,
        request_message: attempt.message,
        assistant_reply: result.assistant_reply,
      });
      if ("no_op" in result && result.no_op) {
        setActionNotice("布局没有变化；服务端保留了原有计划，未生成空的修订版本。");
      } else if (result.plan_hash) {
        setActionNotice("已生成新的不可变计划，正在切换到最新版本。");
      } else {
        setActionNotice("问答已完成；当前 plan 未改变。");
      }
      await invalidateRun();
    },
    onError: async (error, attempt) => {
      if (error instanceof ApiError && error.code === "network_uncertain") {
        setActionNotice(
          "结果不确定，已重新读取服务端持久化状态；如需重试，只会复用原请求键。",
        );
        await reconcileUncertain({ type: "action", value: attempt });
      } else {
        setUncertainAttempt(null);
        await invalidateRun();
      }
    },
  });

  const attentionControl = useMutation({
    mutationFn: async ({ kind, eventSequence, key }: AttentionAttempt) =>
      api.request(
        `/api/v1/runs/${encodedRunId}/${kind}`,
        attentionControlResultSchema,
        {
          method: "POST",
          headers: {
            "If-Match": `event:${eventSequence}`,
            "Idempotency-Key": key,
          },
          body: {},
        },
      ),
    onSuccess: async (result) => {
      setUncertainAttempt(null);
      setActionNotice(
        result.status === "retry_scheduled"
          ? "已安排重新扫描；系统会为仍然匹配的源文件夹创建新运行。"
          : "已生成不可变的 fail 处置计划；请核对后再次确认移动。",
      );
      await invalidateRun();
    },
    onError: async (error, attempt) => {
      if (error instanceof ApiError && error.code === "network_uncertain") {
        setActionNotice(
          "操作结果不确定；已重新读取服务端状态，只允许复用原请求键。",
        );
        await reconcileUncertain({ type: "attention", value: attempt });
      } else {
        setUncertainAttempt(null);
        await invalidateRun();
      }
    },
  });

  const apply = useMutation({
    mutationFn: async ({
      planHash,
      folderDispositionPlanHash,
      key,
    }: ApplyAttempt) => {
      return api.request(
        `/api/v1/runs/${encodedRunId}/approve-and-apply`,
        applyResultSchema,
        {
          method: "POST",
          headers: {
            "If-Match": planHash,
            "Idempotency-Key": key,
          },
          body: {
            automatic: false,
            folder_disposition_plan_hash: folderDispositionPlanHash,
          },
        },
      );
    },
    onSuccess: async () => {
      setUncertainAttempt(null);
      closeApprove();
      await invalidateRun();
    },
    onError: async (error, attempt) => {
      closeApprove();
      if (error instanceof ApiError && error.code === "network_uncertain") {
        setActionNotice(
          "执行结果不确定；页面只显示服务端已结算的结果，可用原请求键安全重试。",
        );
        await reconcileUncertain({ type: "apply", value: attempt });
      } else {
        setUncertainAttempt(null);
        await invalidateRun();
      }
    },
  });

  const folderDisposition = useMutation({
    mutationFn: async ({
      planHash,
      approvalId,
      key,
    }: FolderAttempt) => {
      const recovering = approvalId !== undefined;
      return api.request(
        recovering
          ? `/api/v1/operations/runs/${encodedRunId}/folder-disposition/recover`
          : `/api/v1/runs/${encodedRunId}/folder-disposition`,
        folderDispositionResultSchema,
        {
          method: "POST",
          headers: { "Idempotency-Key": key },
          body: recovering
            ? { plan_hash: planHash, approval_id: approvalId }
            : { plan_hash: planHash, automatic: false },
        },
      );
    },
    onSuccess: async () => {
      setUncertainAttempt(null);
      setFolderConfirmOpen(false);
      await invalidateRun();
    },
    onError: async (error, attempt) => {
      setFolderConfirmOpen(false);
      if (error instanceof ApiError && error.code === "network_uncertain") {
        setActionNotice(
          "文件夹事务结果不确定；已读取服务端持久化状态，只允许复用原请求键。",
        );
        await reconcileUncertain({ type: "folder", value: attempt });
      } else {
        setUncertainAttempt(null);
        await invalidateRun();
      }
    },
  });

  const recover = useMutation({
    mutationFn: async ({
      planHash,
      approvalId,
      key,
    }: RecoveryAttempt) => {
      return api.request(
        `/api/v1/operations/runs/${encodedRunId}/recover`,
        recoveryResultSchema,
        {
          method: "POST",
          headers: {
            "If-Match": planHash,
            "Idempotency-Key": key,
          },
          body: { approval_id: approvalId },
        },
      );
    },
    onSuccess: async () => {
      setUncertainAttempt(null);
      await invalidateRun();
    },
    onError: async (error, attempt) => {
      if (error instanceof ApiError && error.code === "network_uncertain") {
        setActionNotice(
          "恢复结果不确定；已读取服务端持久化状态，只允许复用原请求键。",
        );
        await reconcileUncertain({ type: "recover", value: attempt });
      } else {
        setUncertainAttempt(null);
        await invalidateRun();
      }
    },
  });

  const subtitleAcquisition = useMutation({
    mutationFn: async ({ planHash, key, mode }: SubtitleAttempt) =>
      api.request(
        `/api/v1/runs/${encodedRunId}/subtitle-acquisition/${mode ?? "approve"}`,
        subtitleAcquisitionActionResultSchema,
        {
          method: "POST",
          headers: {
            "If-Match": planHash,
            "Idempotency-Key": key,
          },
          body: {},
        },
      ),
    onSuccess: async (result) => {
      setUncertainAttempt(null);
      if (result.status === "failed") {
        setActionNotice("已结束此运行；原字幕获取失败诊断仍保留供审计。");
      }
      await invalidateRun();
    },
    onError: async (error, attempt) => {
      if (error instanceof ApiError && error.code === "network_uncertain") {
        setActionNotice(
          "字幕获取结果不确定；已读取服务端持久化状态，只允许复用原请求键。",
        );
        await reconcileUncertain({ type: "subtitle", value: attempt });
      } else {
        setUncertainAttempt(null);
        await invalidateRun();
      }
    },
  });

  if (run.isLoading) {
    return <main className="page"><p>正在读取运行…</p></main>;
  }
  if (run.error instanceof ApiError) {
    return <main className="page"><PageError code={run.error.code} /></main>;
  }
  if (!run.data) return null;
  const available = new Set(run.data.available_actions);
  const selectedPlan = lineage.data?.items.find(
    (item) => item.version === effectiveVersion,
  );
  const archiveReportIsCurrent = shouldShowArchiveReport(
    selectedPlan?.plan_hash ?? null,
    run.data.plan_hash,
  );
  const canApprove =
    available.has("approve_apply") &&
    canApproveCurrentPlan(
      run.data.plan_hash,
      currentPreview?.plan_hash ?? null,
    );
  const blocked =
    resyncing ||
    uncertainAttempt !== null ||
    action.isPending ||
    apply.isPending ||
    recover.isPending ||
    subtitleAcquisition.isPending ||
    folderDisposition.isPending ||
    attentionControl.isPending;

  const retryUncertain = () => {
    if (!uncertainAttempt || resyncing) return;
    if (uncertainAttempt.type === "action") {
      action.mutate(uncertainAttempt.value);
    } else if (uncertainAttempt.type === "apply") {
      apply.mutate(uncertainAttempt.value);
    } else if (uncertainAttempt.type === "recover") {
      recover.mutate(uncertainAttempt.value);
    } else if (uncertainAttempt.type === "subtitle") {
      subtitleAcquisition.mutate(uncertainAttempt.value);
    } else if (uncertainAttempt.type === "attention") {
      attentionControl.mutate(uncertainAttempt.value);
    } else {
      folderDisposition.mutate(uncertainAttempt.value);
    }
  };

  return (
    <main className="page run-page">
      <HashLink className="back-link" to="/">
        <IconArrowLeft size={15} />
        返回总览
      </HashLink>
      <section className="run-heading">
        <div>
          <div className="heading-status">
            <Status value={run.data.status} />
            <span>{workTypeLabel(run.data.work_type)}</span>
            {/* The phase often restates the status; show it only when it adds something. */}
            {run.data.phase &&
            statusLabel("phase", run.data.phase) !==
              statusLabel("run", run.data.status) ? (
              <span>{statusLabel("phase", run.data.phase)}</span>
            ) : null}
          </div>
          <h1 title={run.data.source_folder ?? undefined}>
            {run.data.source_folder ?? "运行详情"}
          </h1>
          <div className="run-meta-row">
            <RunIdentity runId={run.data.run_id} />
            <ShortHash value={run.data.plan_hash} label="当前计划" />
          </div>
        </div>
        <div className="stream-indicator">
          <span className="pulse online" />
          {streamState}
        </div>
      </section>

      <section className="stat-strip" aria-label="Agent 用量">
        <RunMetric label="模型轮次" value={run.data.model_turns} />
        <RunMetric label="模型 tokens" value={run.data.model_tokens} />
        <RunMetric label="工具调用" value={run.data.tool_calls} />
        <RunMetric label="失败计数" value={run.data.failures} />
      </section>

      {run.data.settlement ? (
        <section className="settlement" aria-live="polite">
          <div>
            <h2>
              执行状态：
              {statusLabel("settlement", run.data.settlement.status)}
            </h2>
          </div>
          <dl>
            <div><dt>已应用</dt><dd>{run.data.settlement.applied_count}</dd></div>
            <div><dt>已回滚</dt><dd>{run.data.settlement.rolled_back_count}</dd></div>
            <div>
              <dt>事务</dt>
              <dd title={run.data.settlement.transaction_id}>
                {compactValue(run.data.settlement.transaction_id)}
              </dd>
            </div>
          </dl>
          {run.data.settlement.failure_code ? (
            <p className="notice danger" role="alert">
              {errorMessage(run.data.settlement.failure_code)}{" "}
              <code>{run.data.settlement.failure_code}</code>
            </p>
          ) : null}
        </section>
      ) : null}

      {run.data.folder_disposition ? (
        <section className="settlement" aria-live="polite">
          <div>
            <h2>
              文件夹收尾：
              {statusLabel(
                "disposition",
                run.data.folder_disposition.status,
              )}
            </h2>
          </div>
          <dl>
            <div>
              <dt>动作</dt>
              <dd>{folderActionLabel(run.data.folder_disposition.action)}</dd>
            </div>
            <div>
              <dt>目标</dt>
              <dd>{run.data.folder_disposition.target_relative ?? "删除已验证空目录"}</dd>
            </div>
            <div>
              <dt>残留文件</dt>
              <dd>{run.data.folder_disposition.file_count}</dd>
            </div>
          </dl>
          {run.data.folder_disposition.failure_code ? (
            <div className="notice danger" role="alert">
              {errorMessage(run.data.folder_disposition.failure_code)}
              <code>{run.data.folder_disposition.failure_code}</code>
            </div>
          ) : null}
        </section>
      ) : null}

      {run.data.subtitle_acquisition ? (
        <section className="settlement" aria-live="polite">
          <div>
            <h2>字幕获取：{run.data.subtitle_acquisition.status}</h2>
          </div>
          <dl>
            <div>
              <dt>策略</dt>
              <dd>{run.data.subtitle_acquisition.policy}</dd>
            </div>
            <div>
              <dt>计划</dt>
              <dd><ShortHash value={run.data.subtitle_acquisition.plan_hash} /></dd>
            </div>
          </dl>
          {run.data.subtitle_acquisition.failure_code ? (
            <div className="notice danger" role="alert">
              {errorMessage(run.data.subtitle_acquisition.failure_code)}
              <code>{run.data.subtitle_acquisition.failure_code}</code>
              {run.data.subtitle_acquisition.failure_diagnostic ? (
                <p>
                  {subtitleFailureDiagnostic(
                    run.data.subtitle_acquisition.failure_diagnostic,
                  )}
                </p>
              ) : null}
            </div>
          ) : null}
          {run.data.subtitle_acquisition.successor_status === "blocked" ? (
            <div className="notice danger" role="alert">
              字幕已发布，但 fresh scan 或后继运行校验未通过，需要人工检查。
            </div>
          ) : null}
        </section>
      ) : null}

      <div className="run-layout">
        <div className="run-main">
          {displayedInteractions.length ? (
            <section className="panel interaction-history">
              <div className="panel-heading">
                <div className="heading-title">
                  <h2>交互历史</h2>
                  <span className="count-pill">
                    {displayedInteractions.length}
                  </span>
                </div>
              </div>
              <div className="interaction-list">
                {displayedInteractions.map((item) => (
                  <article key={item.interaction_id}>
                    <header>
                      <strong>{interactionLabel(item.kind)}</strong>
                      <Status value={item.status} kind="interaction" />
                    </header>
                    {item.content_available ? (
                      <>
                        <div className="message user-message">
                          <span>你</span>
                          <p>{item.request_message}</p>
                        </div>
                        {item.assistant_reply ? (
                          <div className="message assistant-message">
                            <span>Agent</span>
                            <p>{item.assistant_reply}</p>
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <p className="muted">
                        这条早期历史没有保存消息内容。
                      </p>
                    )}
                  </article>
                ))}
              </div>
              {interactions.hasNextPage ? (
                <button
                  className="secondary"
                  onClick={() => interactions.fetchNextPage()}
                >
                  加载更早记录
                </button>
              ) : null}
            </section>
          ) : null}

          {run.data.archive_report && archiveReportIsCurrent ? (
            <ArchiveReportCard report={run.data.archive_report} />
          ) : run.data.archive_report && selectedPlan ? (
            <section className="panel">
              <p className="muted">
                历史计划版本不显示当前计划的媒体库参考。
              </p>
            </section>
          ) : null}
          <section className="panel">
            <div className="panel-heading">
              <h2>计划审查</h2>
              {lineage.data?.items.length ? (
                <label className="version-picker">
                  <span>版本</span>
                  <select
                    value={effectiveVersion ?? ""}
                    onChange={(event) =>
                      setSelectedVersion(Number(event.target.value))
                    }
                  >
                    {lineage.data.items.map((plan) => (
                      <option key={plan.version} value={plan.version}>
                        v{plan.version} · {plan.plan_kind}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
            </div>
            {selectedPlan ? (
              <div className="plan-meta">
                <ShortHash value={selectedPlan.plan_hash} />
                <span>{selectedPlan.plan_kind === "initial" ? "初始计划" : "修订计划"}</span>
                {selectedPlan.parent_plan_hash ? (
                  <span>父计划 <ShortHash value={selectedPlan.parent_plan_hash} /></span>
                ) : null}
              </div>
            ) : null}
            {currentPreview ? (
              <>
                <PlanReviewCard review={currentPreview.review} />
                <div className="count-strip">
                  <Count label="移动" value={currentPreview.counts.move} />
                  <Count label="未映射" value={currentPreview.counts.unmapped} />
                  <Count label="保持不变" value={currentPreview.counts.unchanged} />
                </div>
                <PreviewGroups items={allPreviewItems} />
                {preview.hasNextPage ? (
                  <button
                    className="secondary"
                    onClick={() => preview.fetchNextPage()}
                    disabled={preview.isFetchingNextPage}
                  >
                    {preview.isFetchingNextPage ? "正在加载…" : "加载更多路径"}
                  </button>
                ) : null}
              </>
            ) : (
              <div className="empty-inline"><p>此运行尚无可审查计划。</p></div>
            )}
          </section>

        </div>

        <aside className="run-aside">
          <section className="panel action-panel">
            <h2>下一步</h2>
            {uncertainAttempt ? (
              <div className="recovery-box">
                <strong>请求结果尚未确认</strong>
                <p>
                  已完成服务端对账。再次提交会复用原请求键，不会产生新的副作用请求。
                </p>
                <button
                  className="secondary wide"
                  disabled={
                    resyncing ||
                    action.isPending ||
                    apply.isPending ||
                    recover.isPending ||
                    subtitleAcquisition.isPending ||
                    folderDisposition.isPending ||
                    attentionControl.isPending
                  }
                  onClick={retryUncertain}
                >
                  {resyncing ? "正在读取服务端状态…" : "使用原请求安全重试"}
                </button>
              </div>
            ) : null}
            {run.data.recovery_approval_id && available.has("recover") ? (
              <div className="recovery-box">
                <strong>需要恢复</strong>
                <p>
                  普通执行已隐藏。只能使用服务端返回的指定审批 ID 继续恢复。
                </p>
                <button
                  className="danger-button wide"
                  disabled={recover.isPending || blocked}
                  onClick={() =>
                    recover.mutate({
                      planHash: run.data.plan_hash!,
                      approvalId: run.data.recovery_approval_id!,
                      key: idempotencyKey(),
                    })
                  }
                >
                  {recover.isPending ? "正在恢复…" : "执行定向恢复"}
                </button>
              </div>
            ) : (
              <div
                className="action-list"
                role="group"
                aria-label="可用操作"
              >
                {available.has("retry_run") ? (
                  <button
                    className="secondary wide"
                    disabled={attentionControl.isPending || blocked}
                    onClick={() =>
                      attentionControl.mutate({
                        kind: "retry",
                        eventSequence: run.data.event_sequence,
                        key: idempotencyKey(),
                      })
                    }
                  >
                    {attentionControl.isPending
                      ? "正在安排重试…"
                      : "重新尝试"}
                  </button>
                ) : null}
                {available.has("retry_subtitle_acquisition") &&
                run.data.subtitle_acquisition ? (
                  <button
                    className="primary wide"
                    disabled={blocked || subtitleAcquisition.isPending}
                    onClick={() =>
                      subtitleAcquisition.mutate({
                        planHash: run.data.subtitle_acquisition!.plan_hash,
                        key: idempotencyKey(),
                        mode: "retry",
                      })
                    }
                  >
                    {subtitleAcquisition.isPending
                      ? "正在恢复并发布字幕…"
                      : "重试字幕获取"}
                  </button>
                ) : null}
                {available.has("fail_subtitle_acquisition") &&
                run.data.subtitle_acquisition ? (
                  <button
                    className="danger-button wide"
                    disabled={blocked || subtitleAcquisition.isPending}
                    onClick={() =>
                      subtitleAcquisition.mutate({
                        planHash: run.data.subtitle_acquisition!.plan_hash,
                        key: idempotencyKey(),
                        mode: "fail",
                      })
                    }
                  >
                    结束此运行
                  </button>
                ) : null}
                {available.has("fail_run") ? (
                  <button
                    className="danger-button wide"
                    disabled={attentionControl.isPending || blocked}
                    onClick={() =>
                      attentionControl.mutate({
                        kind: "fail",
                        eventSequence: run.data.event_sequence,
                        key: idempotencyKey(),
                      })
                    }
                  >
                    标记失败
                  </button>
                ) : null}
                {(["question", "revision", "reapply"] as const).map((kind) =>
                  available.has(kind) ? (
                    <InteractionForm
                      key={kind}
                      kind={kind}
                      pending={action.isPending || blocked}
                      onSubmit={(message) => {
                        const head = run.data.plan_hash
                          ? run.data.plan_hash
                          : `event:${run.data.event_sequence}`;
                        if (blocked) return;
                        action.mutate({
                          kind,
                          message,
                          head,
                          key: idempotencyKey(),
                        });
                      }}
                    />
                  ) : null,
                )}
                {canApprove && currentPreview ? (
                  <button
                    ref={approveButtonRef}
                    className="primary wide"
                    disabled={blocked}
                    onClick={() => setApproveOpen(true)}
                  >
                    审批并执行此计划
                  </button>
                ) : null}
                {available.has("approve_subtitle_acquisition") &&
                run.data.subtitle_acquisition ? (
                  <button
                    className="primary wide"
                    disabled={blocked || subtitleAcquisition.isPending}
                    onClick={() =>
                      subtitleAcquisition.mutate({
                        planHash: run.data.subtitle_acquisition!.plan_hash,
                        key: idempotencyKey(),
                      })
                    }
                  >
                    {subtitleAcquisition.isPending
                      ? "正在获取并发布字幕…"
                      : "审批并获取字幕"}
                  </button>
                ) : null}
                {run.data.folder_disposition &&
                (available.has("settle_folder") ||
                  available.has("dispose_failed_folder")) ? (
                  <button
                    className={
                      run.data.folder_disposition.action === "fail"
                        ? "danger-button wide"
                        : "secondary wide"
                    }
                    disabled={folderDisposition.isPending || blocked}
                    onClick={() => setFolderConfirmOpen(true)}
                  >
                    {run.data.folder_disposition.action === "fail"
                      ? "移入 fail"
                      : "完成文件夹收尾"}
                  </button>
                ) : null}
                {run.data.folder_disposition?.recovery_approval_id &&
                available.has("recover_folder_disposition") ? (
                  <button
                    className="danger-button wide"
                    disabled={folderDisposition.isPending || blocked}
                    onClick={() =>
                      folderDisposition.mutate({
                        planHash: run.data.folder_disposition!.plan_hash,
                        approvalId:
                          run.data.folder_disposition!.recovery_approval_id!,
                        key: idempotencyKey(),
                      })
                    }
                  >
                    恢复文件夹事务
                  </button>
                ) : null}
                {available.has("delete_run") ? (
                  <div className="danger-zone">
                    <p className="danger-zone-note">
                      仅隐藏控制台记录，不改动媒体文件；点两次确认，且无法撤销。
                    </p>
                    <RunDeletionAction
                      runId={runId}
                      disabled={blocked}
                      redirectOnSuccess
                      className="danger-outline wide"
                    />
                  </div>
                ) : null}
              </div>
            )}
            {actionNotice ? (
              <div className="notice" role="status">{actionNotice}</div>
            ) : null}
            {action.error instanceof ApiError ? (
              <PageError code={action.error.code} />
            ) : null}
            {apply.error instanceof ApiError ? (
              <PageError code={apply.error.code} />
            ) : null}
            {recover.error instanceof ApiError ? (
              <PageError code={recover.error.code} />
            ) : null}
            {folderDisposition.error instanceof ApiError ? (
              <PageError code={folderDisposition.error.code} />
            ) : null}
            {subtitleAcquisition.error instanceof ApiError ? (
              <PageError code={subtitleAcquisition.error.code} />
            ) : null}
            {attentionControl.error instanceof ApiError ? (
              <PageError code={attentionControl.error.code} />
            ) : null}
          </section>

          <section className="panel timeline-panel">
            <div className="panel-heading">
              <h2>事件时间线</h2>
            </div>
            <ol className="timeline">
              {events.slice(-40).reverse().map((event) => (
                <li key={event.event_id}>
                  <span className="timeline-dot" />
                  <div>
                    <strong title={event.event_type}>
                      {statusLabel("event", event.event_type)}
                    </strong>
                    <small>#{event.event_id}</small>
                    <SafeEventData value={event.data} />
                  </div>
                </li>
              ))}
            </ol>
          </section>
        </aside>
      </div>

      {approveOpen && currentPreview && canApprove ? (
        <ApproveDialog
          preview={currentPreview}
          folderDisposition={run.data.folder_disposition}
          pending={apply.isPending || blocked}
          onCancel={closeApprove}
          onConfirm={() =>
            apply.mutate({
              planHash: currentPreview.plan_hash,
              folderDispositionPlanHash:
                run.data.folder_disposition?.plan_hash ?? null,
              key: idempotencyKey(),
            })
          }
        />
      ) : null}
      {folderConfirmOpen && run.data.folder_disposition ? (
        <FolderDispositionDialog
          disposition={run.data.folder_disposition}
          pending={folderDisposition.isPending || blocked}
          onCancel={() => setFolderConfirmOpen(false)}
          onConfirm={() =>
            folderDisposition.mutate({
              planHash: run.data.folder_disposition!.plan_hash,
              key: idempotencyKey(),
            })
          }
        />
      ) : null}
    </main>
  );
}

function subtitleFailureDiagnostic(diagnostic: {
  stage: string;
  reason: string;
  actual_mode?: number | null;
  actual_uid?: number | null;
  expected_uid?: number | null;
  entry_count?: number | null;
  member_index?: number | null;
}) {
  const stage = {
    destination_preflight: "最终发布目录预检",
    staging_prepare: "临时目录创建",
    staging_validate: "临时目录校验",
    member_write: "字幕文件写入",
    publish: "原子发布",
  }[diagnostic.stage] ?? diagnostic.stage;
  const reason = {
    name_exists: "目标名称已存在",
    create_failed: "目录创建发生冲突",
    entry_type_mismatch: "同名条目类型不正确",
    unsafe_permissions: "目录权限允许非 owner 写入",
    owner_mismatch: "目录 owner 与服务进程不一致",
    not_empty: "待接管的临时目录不是空目录",
    unexpected_entries: "临时目录包含计划外条目",
    casefold_collision: "存在大小写或 Unicode 等价重名",
  }[diagnostic.reason] ?? diagnostic.reason;
  const details = [
    diagnostic.actual_mode == null
      ? null
      : `实际权限 0${diagnostic.actual_mode.toString(8)}`,
    diagnostic.actual_uid == null
      ? null
      : `实际 UID ${diagnostic.actual_uid}`,
    diagnostic.expected_uid == null
      ? null
      : `期望 UID ${diagnostic.expected_uid}`,
    diagnostic.entry_count == null
      ? null
      : `条目数 ${diagnostic.entry_count}`,
    diagnostic.member_index == null
      ? null
      : `member #${diagnostic.member_index + 1}`,
  ].filter((value): value is string => value !== null);
  return `${stage}：${reason}${details.length ? `（${details.join("，")}）` : ""}`;
}

export function canApproveCurrentPlan(
  currentPlanHash: string | null,
  previewPlanHash: string | null,
): boolean {
  return (
    currentPlanHash !== null &&
    previewPlanHash !== null &&
    currentPlanHash === previewPlanHash
  );
}

export function shouldShowArchiveReport(
  selectedPlanHash: string | null,
  currentPlanHash: string | null,
) {
  return (
    selectedPlanHash !== null &&
    currentPlanHash !== null &&
    selectedPlanHash === currentPlanHash
  );
}

export { compactRunId };

export function RunIdentity({ runId }: { runId: string }) {
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">(
    "idle",
  );
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(runId);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  };
  return (
    <div className="run-id-row">
      <span className="run-id-label">运行 ID</span>
      <code
        title={runId}
        aria-label={`完整运行 ID：${runId}`}
      >
        {compactRunId(runId)}
      </code>
      <button
        type="button"
        className="copy-id-button"
        aria-live="polite"
        onClick={copy}
      >
        {copyStatus === "copied" ? <IconCheck size={13} /> : <IconCopy size={13} />}
        {copyStatus === "copied"
          ? "已复制"
          : copyStatus === "failed"
            ? "重试复制"
            : "复制"}
      </button>
    </div>
  );
}

function mergeEvents(current: RunEvent[], incoming: RunEvent[]) {
  const byId = new Map(current.map((event) => [event.event_id, event]));
  for (const event of incoming) byId.set(event.event_id, event);
  return [...byId.values()].sort((left, right) => left.event_id - right.event_id);
}

function RunMetric({ label, value }: { label: string; value: number }) {
  return (
    <article>
      <span>{label}</span>
      <strong>{value.toLocaleString("zh-CN")}</strong>
    </article>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

function PreviewGroups({
  items,
}: {
  items: Preview["items"];
}) {
  const groups = useMemo(
    () =>
      (["move", "unmapped", "unchanged"] as const).map((disposition) => ({
        disposition,
        items: items.filter((item) => item.disposition === disposition),
      })),
    [items],
  );
  return (
    <div className="preview-groups">
      {groups.map((group) =>
        group.items.length ? (
          <section key={group.disposition}>
            <h3>{dispositionLabel(group.disposition)}</h3>
            <div className="path-list">
              {group.items.map((item) => (
                <article
                  key={item.index}
                  className={
                    item.disposition === "unmapped" &&
                    item.explanation.related_video_id
                      ? "related-preview"
                      : undefined
                  }
                >
                  <span className={`kind-icon ${item.kind}`} aria-hidden="true">
                    {item.kind === "video" ? <IconFilm /> : <IconSubtitle />}
                  </span>
                  <div>
                    <code>{item.source}</code>
                    {item.destination ? (
                      <>
                        <span className="path-arrow" aria-hidden="true">
                          <IconMoveTo size={15} />
                        </span>
                        <code className="destination">{item.destination}</code>
                      </>
                    ) : null}
                    {item.disposition === "unmapped" ? (
                      <PreviewExplanation
                        explanation={item.explanation}
                        items={items}
                      />
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          </section>
        ) : null,
      )}
    </div>
  );
}

export function PlanReviewCard({ review }: { review: Preview["review"] }) {
  return (
    <section className="plan-rationale" aria-label="计划说明">
      <div className="plan-rationale-heading">
        <div className="heading-title">
          <h3>计划说明</h3>
          <Hint label="计划说明的作用" align="end">
            说明仅供审查，执行仍以计划哈希和路径预览为准。系统核验{" "}
            {review.coverage.system_verified} 项、Agent 说明{" "}
            {review.coverage.agent_explained} 项、通用兜底{" "}
            {review.coverage.fallback} 项。
          </Hint>
        </div>
        <span className={`review-status ${review.status}`}>
          {reviewStatusLabel(review.status)}
        </span>
      </div>
      {review.agent_summary ? (
        <div className="review-summary">
          <span className="review-attribution">Agent 判断（仅供审查）</span>
          <p>{review.agent_summary}</p>
        </div>
      ) : (
        <p className="muted">Agent 原始说明不可用。</p>
      )}
    </section>
  );
}

function PreviewExplanation({
  explanation,
  items,
}: {
  explanation: PreviewExplanationValue;
  items: Preview["items"];
}) {
  const related = explanation.related_video_id
    ? items.find(
        (item) => item.candidate_id === explanation.related_video_id,
      )
    : null;
  return (
    <div className="preview-explanation">
      <span className={`verification ${explanation.verification}`}>
        {verificationLabel(explanation.verification)}
      </span>
      <strong>{reasonLabel(explanation.reason_code)}</strong>
      {explanation.agent_detail ? (
        <p>
          <span>Agent：</span>
          {explanation.agent_detail}
        </p>
      ) : null}
      <p>
        <span>系统：</span>
        {systemExplanation(explanation)}
      </p>
      {related ? (
        <p>
          <span>关联正片：</span>
          <code>{related.source}</code>
        </p>
      ) : null}
    </div>
  );
}

export function ArchiveReportCard({
  report,
}: {
  report: NonNullable<Run["archive_report"]>;
}) {
  return (
    <section className="panel archive-report">
      <div className="panel-heading">
        <div className="heading-title">
          <h2>历史媒体库参考</h2>
          <Hint label="历史媒体库参考说明" align="end">
            这些结果仅供 Agent 判断已有内容；实际写入位置始终以计划预览为准，
            不会向旧目录合并文件。
          </Hint>
        </div>
        <Status value={report.status} kind="archive" />
      </div>
      {report.possible_existing_archive ? (
        <div className="notice" role="status">
          找到可能已归档的项目。旧目录格式可能造成重复归档，请核对下方内容。
        </div>
      ) : (
        <div className="empty-inline"><p>没有找到匹配的历史目录。</p></div>
      )}
      {report.status === "incomplete" ? (
        <p className="archive-warning">
          Agent 未完整浏览所有匹配分页或子目录，本报告不能视为完整库存。
        </p>
      ) : null}
      {report.entries.length ? (
        <div className="archive-tree" aria-label="已浏览的历史媒体库内容">
          {report.entries.map((entry) => (
            <div
              key={entry.entry_id}
              className={`archive-entry ${entry.kind}`}
              style={{ paddingInlineStart: `${(entry.depth - 1) * 18}px` }}
            >
              <span aria-hidden="true">
                {entry.kind === "directory" ? (
                  <IconFolder size={15} />
                ) : (
                  <IconFilm size={15} />
                )}
              </span>
              <span>{entry.name}</span>
              {entry.kind === "directory" && !entry.listed ? (
                <small>未展开</small>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function InteractionForm({
  kind,
  pending,
  onSubmit,
}: {
  kind: ActionKind;
  pending: boolean;
  onSubmit: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const bytes = byteLength(message);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (bytes > 0 && bytes <= 16 * 1024) {
      onSubmit(message);
      setMessage("");
    }
  };
  if (!open) {
    return (
      <button className="secondary wide" onClick={() => setOpen(true)}>
        {interactionAction(kind)}
      </button>
    );
  }
  return (
    <form className="interaction-form" onSubmit={submit}>
      <label htmlFor={`message-${kind}`}>{interactionAction(kind)}</label>
      <textarea
        id={`message-${kind}`}
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        rows={5}
        autoFocus
        required
      />
      <div className={bytes > 16 * 1024 ? "byte-count invalid" : "byte-count"}>
        {bytes.toLocaleString("zh-CN")} / 16,384 bytes
      </div>
      <div className="button-row">
        <button type="button" className="ghost" onClick={() => setOpen(false)}>
          取消
        </button>
        <button className="primary" disabled={pending || bytes > 16 * 1024}>
          提交
        </button>
      </div>
    </form>
  );
}

function ApproveDialog({
  preview,
  folderDisposition,
  pending,
  onCancel,
  onConfirm,
}: {
  preview: Preview;
  folderDisposition: Run["folder_disposition"];
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef(onCancel);
  const pendingRef = useRef(pending);
  cancelRef.current = onCancel;
  pendingRef.current = pending;
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !pendingRef.current) {
        cancelRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [
        ...(dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])',
        ) ?? []),
      ];
      const first = focusable[0];
      const last = focusable.at(-1);
      if (
        event.shiftKey &&
        first &&
        last &&
        document.activeElement === first
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        first &&
        last &&
        document.activeElement === last
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
  return (
    <div className="modal-backdrop">
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approve-title"
      >
        <div className="heading-title">
          <h2 id="approve-title">确认文件移动</h2>
          <Hint label="执行前的服务端校验" align="end">
            服务端会再次校验计划哈希、审批记录、源文件身份、目标冲突与目标不存在，
            任一不符都会中止。
          </Hint>
        </div>
        <p>即将审批 v{preview.version}（{preview.plan_kind}）。</p>
        <div className="exact-hash">
          <span>计划哈希</span>
          <code>{preview.plan_hash}</code>
        </div>
        <div className="count-strip">
          <Count label="移动" value={preview.counts.move} />
          <Count label="未映射" value={preview.counts.unmapped} />
          <Count label="保持不变" value={preview.counts.unchanged} />
        </div>
        <p className="review-coverage">
          计划说明：{reviewStatusLabel(preview.review.status)} · 系统核验{" "}
          {preview.review.coverage.system_verified} / 未映射{" "}
          {preview.review.coverage.total_unmapped}
        </p>
        {folderDisposition ? (
          <div className="exact-hash">
            <span>
              文件夹收尾 · {folderActionLabel(folderDisposition.action)}
            </span>
            <code>{folderDisposition.plan_hash}</code>
            <small>
              {folderDisposition.target_relative ??
                "执行后仅删除已验证为空的入站目录"}
              {" · "}
              残留文件 {folderDisposition.file_count}
            </small>
          </div>
        ) : null}
        <label className="risk-check">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            autoFocus
          />
          <span>
            我已审查计划哈希与路径预览，并理解文件移动可能需要恢复操作。
          </span>
        </label>
        <div className="button-row end">
          <button className="ghost" disabled={pending} onClick={onCancel}>
            取消
          </button>
          <button
            className="danger-button"
            disabled={!confirmed || pending}
            onClick={onConfirm}
          >
            {pending ? "等待服务端结算…" : "批准并执行"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FolderDispositionDialog({
  disposition,
  pending,
  onCancel,
  onConfirm,
}: {
  disposition: NonNullable<Run["folder_disposition"]>;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  return (
    <div className="modal-backdrop">
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="folder-disposition-title"
      >
        <div className="heading-title">
          <h2 id="folder-disposition-title">确认文件夹收尾</h2>
          <Hint label="执行前的服务端校验" align="end">
            服务端会重新校验目录身份、完整文件清单与目标不存在；
            浏览器不会推断移动是否成功。
          </Hint>
        </div>
        <div className="exact-hash">
          <span>{folderActionLabel(disposition.action)}</span>
          <code>{disposition.plan_hash}</code>
          <small>
            {disposition.target_relative ?? "仅移除已验证为空的目录"}
            {" · "}
            文件 {disposition.file_count}
          </small>
        </div>
        <label className="risk-check">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            autoFocus
          />
          <span>我已核对计划哈希、目标和文件数量。</span>
        </label>
        <div className="button-row end">
          <button className="ghost" disabled={pending} onClick={onCancel}>
            取消
          </button>
          <button
            className="danger-button"
            disabled={!confirmed || pending}
            onClick={onConfirm}
          >
            {pending ? "等待服务端结算…" : "确认执行"}
          </button>
        </div>
      </div>
    </div>
  );
}

function SafeEventData({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value).filter(
    ([, item]) =>
      item === null ||
      typeof item === "string" ||
      typeof item === "number" ||
      typeof item === "boolean",
  );
  if (!entries.length) return null;
  const full = entries
    .map(([key, item]) => `${key}: ${String(item)}`)
    .join(" · ");
  return (
    <p title={full}>
      {entries
        .map(([key, item]) => `${key}: ${compactValue(String(item))}`)
        .join(" · ")}
    </p>
  );
}

function interactionLabel(kind: string) {
  return { question: "问答", revision: "计划修订", reapply: "布局重应用" }[
    kind
  ] ?? kind;
}

function interactionAction(kind: ActionKind) {
  return {
    question: "向 Agent 提问",
    revision: "要求修订计划",
    reapply: "重新整理已完成布局",
  }[kind];
}

function dispositionLabel(value: "move" | "unmapped" | "unchanged") {
  return { move: "将移动", unmapped: "未映射", unchanged: "保持不变" }[value];
}

function folderActionLabel(value: "archive" | "fail" | "remove_empty") {
  return {
    archive: "移入 archive",
    fail: "移入 fail",
    remove_empty: "删除空目录",
  }[value];
}

function reviewStatusLabel(value: Preview["review"]["status"]) {
  return {
    agent_and_system: "Agent + 系统",
    system_only: "仅系统证据",
    unavailable: "历史说明不可用",
  }[value];
}

function verificationLabel(
  value: PreviewExplanationValue["verification"],
) {
  return {
    verified: "冲突记录已核验",
    advisory: "Agent 判断",
    fallback: "系统 fallback",
  }[value];
}

function reasonLabel(value: PreviewExplanationValue["reason_code"]) {
  return {
    existing_episode: "媒体库中已存在对应集数",
    possible_existing_movie: "可能已经归档",
    extra_video: "额外视频",
    ambiguous_mapping: "映射不确定",
    unsupported_content: "不支持的内容",
    duplicate_candidate: "重复候选",
    not_selected: "未进入最终映射",
    other: "其他原因",
  }[value];
}

function systemExplanation(explanation: PreviewExplanationValue) {
  if (
    explanation.verification === "verified" &&
    explanation.reason_code === "existing_episode" &&
    explanation.season !== null &&
    explanation.episode !== null
  ) {
    return `系统记录了该候选对 S${String(explanation.season).padStart(2, "0")}E${String(explanation.episode).padStart(2, "0")} 的 inventory_conflict，因此保持未映射。`;
  }
  if (explanation.verification === "advisory") {
    return "该原因来自 Agent 的审查结论，未获得确定性系统核验。";
  }
  return "该候选未进入最终映射，原始具体原因不可用。";
}
