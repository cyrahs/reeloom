import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError, idempotencyKey } from "../api";
import { useAuth } from "../auth";
import { runDeletionSchema, runSchema } from "../schemas";
import { cursorKey } from "../sse";
import { errorMessage } from "./Status";

export function RunDeletionAction({
  runId,
  disabled = false,
  redirectOnSuccess = false,
  className = "danger-button",
}: {
  runId: string;
  disabled?: boolean;
  redirectOnSuccess?: boolean;
  className?: string;
}) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [attemptKey, setAttemptKey] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const encodedRunId = encodeURIComponent(runId);

  const finish = async () => {
    window.localStorage.removeItem(cursorKey(runId));
    queryClient.removeQueries({ queryKey: ["run", runId] });
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["runs"] }),
      queryClient.invalidateQueries({ queryKey: ["discoveries"] }),
      queryClient.invalidateQueries({ queryKey: ["folders"] }),
    ]);
    if (redirectOnSuccess) window.location.hash = "/";
  };

  const deletion = useMutation({
    mutationFn: async (key: string) =>
      api.request(
        `/api/v1/runs/${encodedRunId}`,
        runDeletionSchema,
        {
          method: "DELETE",
          headers: { "Idempotency-Key": key },
        },
      ),
    onSuccess: finish,
    onError: async (error) => {
      setOpen(false);
      if (!(error instanceof ApiError) || error.code !== "network_uncertain") {
        setAttemptKey(null);
        return;
      }
      try {
        await api.request(`/api/v1/runs/${encodedRunId}`, runSchema);
        setNotice("删除结果不确定；可使用原请求键安全重试。");
      } catch (readError) {
        if (readError instanceof ApiError && readError.status === 404) {
          await finish();
          return;
        }
        setNotice("删除结果暂时无法对账；请稍后使用原请求键重试。");
      }
    },
  });

  return (
    <>
      <button
        className={className}
        disabled={disabled || deletion.isPending}
        onClick={() => {
          deletion.reset();
          setNotice("");
          setOpen(true);
        }}
      >
        删除记录
      </button>
      {notice ? <p className="form-error" role="status">{notice}</p> : null}
      {deletion.error instanceof ApiError &&
      deletion.error.code !== "network_uncertain" ? (
        <p className="form-error" role="alert">
          {errorMessage(deletion.error.code)}
          {" "}
          <code>{deletion.error.code}</code>
        </p>
      ) : null}
      {attemptKey && !deletion.isPending && notice ? (
        <button
          className="secondary"
          onClick={() => deletion.mutate(attemptKey)}
        >
          使用原请求键重试
        </button>
      ) : null}
      {open ? (
        <DeleteRunDialog
          runId={runId}
          pending={deletion.isPending}
          onCancel={() => setOpen(false)}
          onConfirm={() => {
            const key = idempotencyKey();
            setAttemptKey(key);
            deletion.mutate(key);
          }}
        />
      ) : null}
    </>
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
