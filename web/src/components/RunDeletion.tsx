import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError, idempotencyKey } from "../api";
import { useAuth } from "../auth";
import { runDeletionSchema, runSchema } from "../schemas";
import { cursorKey } from "../sse";
import { IconTrash } from "./Icon";
import { errorMessage } from "./Status";

/** How long an armed delete button stays armed before it disarms itself. */
const ARM_TIMEOUT_MS = 6000;
/** A double-click must not sail straight through the confirmation. */
const ARM_SETTLE_MS = 400;

/**
 * Two-click confirmation: the first click arms the button, the second one
 * performs the delete. Arming lapses on its own so a stray click can never
 * leave a loaded gun in the table.
 */
function useArmedAction(onConfirm: () => void) {
  const [armed, setArmed] = useState(false);
  const armedAt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const disarm = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    setArmed(false);
  }, []);

  useEffect(() => disarm, [disarm]);

  const click = () => {
    if (armed) {
      // Too soon to be a considered second click — stay armed and wait.
      if (Date.now() - armedAt.current < ARM_SETTLE_MS) return;
      disarm();
      onConfirm();
      return;
    }
    setArmed(true);
    armedAt.current = Date.now();
    timer.current = setTimeout(() => {
      timer.current = null;
      setArmed(false);
    }, ARM_TIMEOUT_MS);
  };

  return { armed, click, disarm };
}

/** Drops every client-side trace of runs the server no longer exposes. */
function useForgetRuns() {
  const queryClient = useQueryClient();
  return useCallback(
    async (runIds: string[]) => {
      for (const runId of runIds) {
        window.localStorage.removeItem(cursorKey(runId));
        queryClient.removeQueries({ queryKey: ["run", runId] });
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["runs"] }),
        queryClient.invalidateQueries({ queryKey: ["discoveries"] }),
        queryClient.invalidateQueries({ queryKey: ["folders"] }),
      ]);
    },
    [queryClient],
  );
}

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
  const forgetRuns = useForgetRuns();
  const [attemptKey, setAttemptKey] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const encodedRunId = encodeURIComponent(runId);

  const finish = async () => {
    await forgetRuns([runId]);
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

  const { armed, click, disarm } = useArmedAction(() => {
    const key = idempotencyKey();
    setAttemptKey(key);
    deletion.mutate(key);
  });

  return (
    <>
      <button
        className={armed ? `${className} armed` : className}
        disabled={disabled || deletion.isPending}
        aria-live="polite"
        title={armed ? undefined : "仅隐藏控制台记录，不改动媒体文件"}
        onClick={() => {
          if (!armed) {
            deletion.reset();
            setNotice("");
          }
          click();
        }}
        onBlur={disarm}
      >
        <IconTrash size={14} />
        {deletion.isPending
          ? "正在删除…"
          : armed
            ? "再点一次删除"
            : "删除记录"}
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
    </>
  );
}

/**
 * Batch deletion for a checkbox selection. Each run keeps its own idempotency
 * key and its own request: a failure part-way through leaves the runs that did
 * succeed deleted, and reports the rest instead of claiming the whole batch.
 */
export function RunBulkDeletionAction({
  runIds,
  disabled = false,
  className = "danger-outline compact",
  onDeleted,
}: {
  runIds: string[];
  disabled?: boolean;
  className?: string;
  onDeleted?: (deleted: string[]) => void;
}) {
  const { api } = useAuth();
  const forgetRuns = useForgetRuns();
  const [notice, setNotice] = useState("");

  const deletion = useMutation({
    mutationFn: async (targets: string[]) => {
      const deleted: string[] = [];
      const failures: string[] = [];
      for (const runId of targets) {
        try {
          await api.request(
            `/api/v1/runs/${encodeURIComponent(runId)}`,
            runDeletionSchema,
            {
              method: "DELETE",
              headers: { "Idempotency-Key": idempotencyKey() },
            },
          );
          deleted.push(runId);
        } catch (error) {
          if (error instanceof ApiError && error.status === 404) {
            deleted.push(runId);
            continue;
          }
          failures.push(runId);
        }
      }
      return { deleted, failures };
    },
    onSuccess: async ({ deleted, failures }) => {
      await forgetRuns(deleted);
      setNotice(
        failures.length
          ? `已删除 ${deleted.length} 条，${failures.length} 条失败，可重试。`
          : "",
      );
      onDeleted?.(deleted);
    },
  });

  const { armed, click, disarm } = useArmedAction(() =>
    deletion.mutate(runIds),
  );

  return (
    <>
      <button
        className={armed ? `${className} armed` : className}
        disabled={disabled || !runIds.length || deletion.isPending}
        aria-live="polite"
        onClick={() => {
          if (!armed) setNotice("");
          click();
        }}
        onBlur={disarm}
      >
        <IconTrash size={14} />
        {deletion.isPending
          ? "正在删除…"
          : armed
            ? `再点一次删除 ${runIds.length} 条`
            : `删除所选 ${runIds.length} 条`}
      </button>
      {notice ? <p className="form-error" role="status">{notice}</p> : null}
    </>
  );
}
