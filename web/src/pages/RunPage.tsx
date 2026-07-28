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
import { PageError, ShortHash, Status } from "../components/Status";
import { HashLink } from "../router";
import {
  applyResultSchema,
  folderDispositionResultSchema,
  interactionsSchema,
  interactionResultSchema,
  lineageSchema,
  previewSchema,
  reapplyResultSchema,
  recoveryResultSchema,
  runDeletionSchema,
  runSchema,
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
type ActionAttempt = {
  kind: ActionKind;
  message: string;
  planHash: string;
  key: string;
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
type UncertainAttempt =
  | { type: "action"; value: ActionAttempt }
  | { type: "apply"; value: ApplyAttempt }
  | { type: "recover"; value: RecoveryAttempt }
  | { type: "folder"; value: FolderAttempt };

export function RunPage({ runId }: { runId: string }) {
  const { api, logout } = useAuth();
  const queryClient = useQueryClient();
  const encodedRunId = encodeURIComponent(runId);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [streamState, setStreamState] = useState("正在同步");
  const [approveOpen, setApproveOpen] = useState(false);
  const [folderConfirmOpen, setFolderConfirmOpen] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deleteAttemptKey, setDeleteAttemptKey] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState("");
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
      planHash,
      key,
    }: ActionAttempt) => {
      const headers = {
        "If-Match": planHash,
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
    onSuccess: async (result) => {
      setUncertainAttempt(null);
      if ("no_op" in result && result.no_op) {
        setActionNotice("布局没有变化；服务端保留原 head，未创建空 amendment。");
      } else if (result.plan_hash) {
        setActionNotice("已生成新的 immutable plan，正在切换到最新版本。");
      } else {
        setActionNotice("问答已完成；当前 plan 未改变。");
      }
      await invalidateRun();
    },
    onError: async (error, attempt) => {
      if (error instanceof ApiError && error.code === "network_uncertain") {
        setActionNotice(
          "结果不确定，已重新读取 durable state；如需重放，只会复用原请求键。",
        );
        await reconcileUncertain({ type: "action", value: attempt });
      } else {
        setUncertainAttempt(null);
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
          "执行结果不确定；页面只显示服务端 durable settlement，可用原请求键安全重放。",
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
          "文件夹事务结果不确定；已读取 durable state，只允许复用原请求键。",
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
          "恢复结果不确定；已读取 durable state，只允许复用原请求键。",
        );
        await reconcileUncertain({ type: "recover", value: attempt });
      } else {
        setUncertainAttempt(null);
        await invalidateRun();
      }
    },
  });

  const finishDeletion = async () => {
    window.localStorage.removeItem(cursorKey(runId));
    queryClient.removeQueries({ queryKey: ["run", runId] });
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["runs"] }),
      queryClient.invalidateQueries({ queryKey: ["discoveries"] }),
      queryClient.invalidateQueries({ queryKey: ["folders"] }),
    ]);
    window.location.hash = "/";
  };

  const deleteRun = useMutation({
    mutationFn: async (key: string) =>
      api.request(
        `/api/v1/runs/${encodedRunId}`,
        runDeletionSchema,
        {
          method: "DELETE",
          headers: { "Idempotency-Key": key },
        },
      ),
    onSuccess: finishDeletion,
    onError: async (error) => {
      setDeleteConfirmOpen(false);
      if (!(error instanceof ApiError) || error.code !== "network_uncertain") {
        setDeleteAttemptKey(null);
        return;
      }
      try {
        await api.request(`/api/v1/runs/${encodedRunId}`, runSchema);
        setActionNotice(
          "删除结果不确定；记录仍可读取，可使用原请求键安全重试。",
        );
      } catch (readError) {
        if (readError instanceof ApiError && readError.status === 404) {
          await finishDeletion();
          return;
        }
        setActionNotice(
          "删除结果不确定且暂时无法对账；请稍后使用原请求键重试。",
        );
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
  const canApprove =
    available.has("approve_apply") &&
    canApproveCurrentPlan(
      run.data.plan_hash,
      currentPreview?.plan_hash ?? null,
    );
  const blocked = resyncing || uncertainAttempt !== null;

  const retryUncertain = () => {
    if (!uncertainAttempt || resyncing) return;
    if (uncertainAttempt.type === "action") {
      action.mutate(uncertainAttempt.value);
    } else if (uncertainAttempt.type === "apply") {
      apply.mutate(uncertainAttempt.value);
    } else if (uncertainAttempt.type === "recover") {
      recover.mutate(uncertainAttempt.value);
    } else {
      folderDisposition.mutate(uncertainAttempt.value);
    }
  };

  return (
    <main className="page run-page">
      <HashLink className="back-link" to="/">← 返回总览</HashLink>
      <section className="run-heading">
        <div>
          <div className="heading-status">
            <Status value={run.data.status} />
            <span>{workTypeLabel(run.data.work_type)}</span>
            <span>{run.data.phase ?? "无活动阶段"}</span>
          </div>
          <h1>{run.data.run_id}</h1>
          <ShortHash value={run.data.plan_hash} />
          {run.data.source_folder ? (
            <p className="muted">入站文件夹：{run.data.source_folder}</p>
          ) : null}
        </div>
        <div className="stream-indicator">
          <span className="pulse online" />
          {streamState}
        </div>
      </section>

      <section className="metric-grid four">
        <RunMetric label="模型轮次" value={run.data.model_turns} />
        <RunMetric label="模型 tokens" value={run.data.model_tokens} />
        <RunMetric label="工具调用" value={run.data.tool_calls} />
        <RunMetric label="失败计数" value={run.data.failures} />
      </section>

      {run.data.settlement ? (
        <section className="settlement" aria-live="polite">
          <div>
            <p className="eyebrow">DURABLE SETTLEMENT</p>
            <h2>执行状态：{run.data.settlement.status}</h2>
          </div>
          <dl>
            <div><dt>Transaction</dt><dd>{run.data.settlement.transaction_id}</dd></div>
            <div><dt>已应用</dt><dd>{run.data.settlement.applied_count}</dd></div>
            <div><dt>已回滚</dt><dd>{run.data.settlement.rolled_back_count}</dd></div>
          </dl>
        </section>
      ) : null}

      {run.data.folder_disposition ? (
        <section className="settlement" aria-live="polite">
          <div>
            <p className="eyebrow">FOLDER DISPOSITION</p>
            <h2>
              文件夹收尾：{run.data.folder_disposition.status}
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
        </section>
      ) : null}

      <div className="run-layout">
        <div className="run-main">
          <section className="panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">PLAN REVIEW</p>
                <h2>计划审查</h2>
              </div>
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

          <section className="panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">AGENT HISTORY</p>
                <h2>交互历史</h2>
              </div>
            </div>
            <div className="interaction-list">
              {interactions.data?.pages.flatMap((page) =>
                page.items.map((item) => (
                  <article key={item.interaction_id}>
                    <header>
                      <strong>{interactionLabel(item.kind)}</strong>
                      <Status value={item.status} />
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
                        这条 M8 历史没有保存显式消息内容。
                      </p>
                    )}
                  </article>
                )),
              )}
              {!interactions.data?.pages[0].items.length ? (
                <div className="empty-inline"><p>还没有显式交互。</p></div>
              ) : null}
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
        </div>

        <aside className="run-aside">
          <section className="panel action-panel">
            <p className="eyebrow">AVAILABLE ACTIONS</p>
            <h2>下一步</h2>
            {uncertainAttempt ? (
              <div className="recovery-box">
                <strong>请求结果尚未确认</strong>
                <p>
                  已完成 REST 对账。再次提交会复用原 idempotency key，
                  不会创建新的副作用请求。
                </p>
                <button
                  className="secondary wide"
                  disabled={
                    resyncing ||
                    action.isPending ||
                    apply.isPending ||
                    recover.isPending ||
                    folderDisposition.isPending
                  }
                  onClick={retryUncertain}
                >
                  {resyncing ? "正在读取 durable state…" : "使用原请求安全重放"}
                </button>
              </div>
            ) : null}
            {run.data.recovery_approval_id ? (
              <div className="recovery-box">
                <strong>需要恢复</strong>
                <p>
                  普通执行已隐藏。只能使用服务端返回的 exact approval ID
                  继续恢复。
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
                  {recover.isPending ? "正在恢复…" : "执行 exact recovery"}
                </button>
              </div>
            ) : (
              <>
                {(["question", "revision", "reapply"] as const).map((kind) =>
                  available.has(kind) ? (
                    <InteractionForm
                      key={kind}
                      kind={kind}
                      pending={action.isPending || blocked}
                      onSubmit={(message) => {
                        if (!run.data.plan_hash || blocked) return;
                        action.mutate({
                          kind,
                          message,
                          planHash: run.data.plan_hash,
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
                    审批并执行此 exact plan
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
                  <button
                    className="danger-button wide"
                    disabled={deleteRun.isPending || blocked}
                    onClick={() => setDeleteConfirmOpen(true)}
                  >
                    删除运行记录
                  </button>
                ) : null}
              </>
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
            {deleteRun.error instanceof ApiError ? (
              <PageError code={deleteRun.error.code} />
            ) : null}
            {deleteAttemptKey && !deleteRun.isPending ? (
              <button
                className="secondary wide"
                onClick={() => deleteRun.mutate(deleteAttemptKey)}
              >
                使用原请求键重试删除
              </button>
            ) : null}
          </section>

          <section className="panel timeline-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">DURABLE EVENTS</p>
                <h2>事件时间线</h2>
              </div>
            </div>
            <ol className="timeline">
              {events.slice(-40).reverse().map((event) => (
                <li key={event.event_id}>
                  <span className="timeline-dot" />
                  <div>
                    <strong>{event.event_type}</strong>
                    <small>event {event.event_id}</small>
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
      {deleteConfirmOpen && available.has("delete_run") ? (
        <DeleteRunDialog
          runId={runId}
          pending={deleteRun.isPending}
          onCancel={() => setDeleteConfirmOpen(false)}
          onConfirm={() => {
            const key = idempotencyKey();
            setDeleteAttemptKey(key);
            deleteRun.mutate(key);
          }}
        />
      ) : null}
    </main>
  );
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

function mergeEvents(current: RunEvent[], incoming: RunEvent[]) {
  const byId = new Map(current.map((event) => [event.event_id, event]));
  for (const event of incoming) byId.set(event.event_id, event);
  return [...byId.values()].sort((left, right) => left.event_id - right.event_id);
}

function RunMetric({ label, value }: { label: string; value: number }) {
  return (
    <article className="metric compact-metric">
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
                <article key={item.index}>
                  <span className={`kind-icon ${item.kind}`}>
                    {item.kind === "video" ? "V" : "S"}
                  </span>
                  <div>
                    <code>{item.source}</code>
                    {item.destination ? (
                      <>
                        <span className="path-arrow">↓</span>
                        <code className="destination">{item.destination}</code>
                      </>
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
        <p className="eyebrow">EXACT PLAN APPROVAL</p>
        <h2 id="approve-title">确认文件移动</h2>
        <p>
          你将审批 v{preview.version} {preview.plan_kind}。服务端会再次验证
          hash、审批、源文件 identity、collision 和目标不存在。
        </p>
        <div className="exact-hash">
          <span>Exact plan hash</span>
          <code>{preview.plan_hash}</code>
        </div>
        <div className="count-strip">
          <Count label="移动" value={preview.counts.move} />
          <Count label="未映射" value={preview.counts.unmapped} />
          <Count label="保持不变" value={preview.counts.unchanged} />
        </div>
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
            我已审查 exact hash 与路径预览，并理解文件移动可能需要 recovery。
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
        <p className="eyebrow">EXACT FOLDER DISPOSITION</p>
        <h2 id="folder-disposition-title">确认文件夹收尾</h2>
        <p>
          服务端会重新验证目录 identity、完整 inventory 与目标不存在，
          浏览器不会推断移动成功。
        </p>
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
          <span>我已核对 exact hash、目标和文件数量。</span>
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

export function DeleteRunDialog({
  runId,
  pending,
  onCancel,
  onConfirm,
}: {
  runId: string;
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
        aria-labelledby="delete-run-title"
      >
        <p className="eyebrow">RUN RECORD</p>
        <h2 id="delete-run-title">删除运行记录？</h2>
        <p>
          {runId} 将从控制台和公开 API 中永久隐藏。媒体文件不会改变，
          底层计划、事件和事务审计会继续保留。
        </p>
        <label className="risk-check">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            autoFocus
          />
          <span>我理解此操作不会删除媒体，但无法恢复显示这条记录。</span>
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
            {pending ? "正在删除记录…" : "确认删除记录"}
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
  return entries.length ? (
    <p>{entries.map(([key, item]) => `${key}: ${String(item)}`).join(" · ")}</p>
  ) : null;
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
