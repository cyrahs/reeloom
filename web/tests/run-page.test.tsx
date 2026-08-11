import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TOKEN_STORAGE_KEY } from "../src/api";
import { AuthGate } from "../src/auth";
import { RunPage } from "../src/pages/RunPage";
import "../src/styles.css";

const runId = "run-0dae51fc45a8db238e0b901e8f420272";
const encodedRunId = encodeURIComponent(runId);
const planHash = `sha256:${"a".repeat(64)}`;

test("executes only the opaque action advertised by the current lifecycle", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  const actionId = `runaction-v1:${"c".repeat(64)}`;
  let executed = false;
  const response = () => ({
    ...runResponse(),
    status: executed ? "running" : "awaiting_approval",
    phase: executed ? "execution_queued" : "awaiting_approval",
    available_actions: executed ? [] : ["execute"],
    lifecycle: {
      schema_version: 1,
      mode: "forward_v2",
      state: executed ? "execution_queued" : "awaiting_approval",
      terminal: false,
      revision: executed ? 2 : 1,
      active_plan: { family: "media_move", plan_hash: planHash },
      operation_id: executed ? "operation:m14-6" : null,
      operation_status: executed ? "authorized" : null,
      rescan_state: null,
      successor_run_id: null,
      housekeeping: { state: null, warning: null },
      actions: executed
        ? []
        : [{
            action_id: actionId,
            kind: "execute",
            input: "confirmation",
            destructive: true,
          }],
      etag: `runpresentation-v1:${"d".repeat(64)}`,
    },
  });
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === `/api/v1/runs/${encodedRunId}`) {
      return jsonResponse(response());
    }
    if (path === `/api/v1/runs/${encodedRunId}/plans?limit=100`) {
      return jsonResponse({
        items: [{
          run_id: runId,
          version: 1,
          plan_hash: planHash,
          parent_plan_hash: null,
          plan_kind: "initial",
          created_at: "2026-08-08T00:00:00Z",
        }],
      });
    }
    if (
      path ===
      `/api/v1/runs/${encodedRunId}/plans/1/preview?after=0&limit=100`
    ) {
      return jsonResponse(previewResponse());
    }
    if (path.startsWith(`/api/v1/runs/${encodedRunId}/interactions?`)) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events?after=0&limit=100`) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events/stream`) {
      return new Response(": keepalive\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (
      path ===
        `/api/v1/runs/${encodedRunId}/actions/${encodeURIComponent(actionId)}` &&
      init?.method === "POST"
    ) {
      expect(JSON.parse(String(init.body))).toEqual({ message: null });
      expect(init.headers).toMatchObject({
        "Idempotency-Key": expect.any(String),
      });
      executed = true;
      return jsonResponse({
        action_id: actionId,
        kind: "execute",
        run: response(),
        assistant_reply: null,
      });
    }
    throw new Error(`unexpected request: ${path}`);
  });

  renderRunPage();
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", { name: "审批并执行此计划" }),
  );
  const dialog = screen.getByRole("dialog", { name: "确认文件移动" });
  await user.click(within(dialog).getByRole("checkbox"));
  await user.click(
    within(dialog).getByRole("button", { name: "批准并执行" }),
  );

  expect(
    await screen.findByText("请求已按服务端当前状态处理。"),
  ).toBeVisible();
  expect(executed).toBe(true);
  expect(screen.queryByText("执行定向恢复")).toBeNull();
});

test("shows terminal v2 result without a duplicate queued rescan action", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  const executeActionId = `runaction-v1:${"e".repeat(64)}`;
  const deleteActionId = `runaction-v1:${"f".repeat(64)}`;
  let executed = false;
  const execution = {
    operation_id: "operation:m14",
    operation_kind: "media_move",
    plan_hash: planHash,
    status: "partial",
    attempt_count: 1,
    counts: {
      satisfied: 1,
      stale: 0,
      collision: 1,
      unsafe: 0,
      unavailable: 0,
    },
    items: [
      {
        source_id: "video:1",
        outcome: "satisfied",
        diagnostic: "checked_rename",
      },
      {
        source_id: "video:2",
        outcome: "collision",
        diagnostic: null,
      },
    ],
    warnings: ["directory_fsync_unsupported"],
    fresh_scan_required: true,
    rescan_state: "queued",
    successor_run_id: null,
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === `/api/v1/runs/${encodedRunId}`) {
      return jsonResponse({
        ...runResponse(),
        status: executed ? "failed" : "awaiting_approval",
        phase: executed ? "failed" : "awaiting_approval",
        available_actions: executed ? ["delete_run"] : ["execute"],
        recovery_approval_id: null,
        execution: executed ? execution : null,
        lifecycle: {
          schema_version: 1,
          mode: "forward_v2",
          state: executed ? "failed" : "awaiting_approval",
          terminal: executed,
          revision: executed ? 2 : 1,
          active_plan: { family: "media_move", plan_hash: planHash },
          operation_id: executed ? execution.operation_id : null,
          operation_status: executed ? "partial" : null,
          rescan_state: executed ? "queued" : null,
          successor_run_id: null,
          housekeeping: { state: null, warning: null },
          actions: executed
            ? [{
                action_id: deleteActionId,
                kind: "delete_run",
                input: "confirmation",
                destructive: true,
              }]
            : [{
                action_id: executeActionId,
                kind: "execute",
                input: "confirmation",
                destructive: true,
              }],
          etag: `runpresentation-v1:${"1".repeat(64)}`,
        },
      });
    }
    if (path === `/api/v1/runs/${encodedRunId}/plans?limit=100`) {
      return jsonResponse({
        items: [
          {
            run_id: runId,
            version: 1,
            plan_hash: planHash,
            parent_plan_hash: null,
            plan_kind: "initial",
            created_at: "2026-08-07T00:00:00Z",
          },
        ],
      });
    }
    if (
      path ===
      `/api/v1/runs/${encodedRunId}/plans/1/preview?after=0&limit=100`
    ) {
      return jsonResponse(previewResponse());
    }
    if (path.startsWith(`/api/v1/runs/${encodedRunId}/interactions?`)) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events?after=0&limit=100`) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events/stream`) {
      return new Response(": keepalive\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (
      path ===
        `/api/v1/runs/${encodedRunId}/actions/${encodeURIComponent(executeActionId)}` &&
      init?.method === "POST"
    ) {
      expect(JSON.parse(String(init.body))).toEqual({ message: null });
      executed = true;
      return jsonResponse({
        action_id: executeActionId,
        kind: "execute",
        run: {
          ...runResponse(),
          status: "failed",
          phase: "failed",
          available_actions: ["delete_run"],
          execution,
          lifecycle: {
            schema_version: 1,
            mode: "forward_v2",
            state: "failed",
            terminal: true,
            revision: 2,
            active_plan: { family: "media_move", plan_hash: planHash },
            operation_id: execution.operation_id,
            operation_status: "partial",
            rescan_state: "queued",
            successor_run_id: null,
            housekeeping: { state: null, warning: null },
            actions: [{
              action_id: deleteActionId,
              kind: "delete_run",
              input: "confirmation",
              destructive: true,
            }],
            etag: `runpresentation-v1:${"2".repeat(64)}`,
          },
        },
        assistant_reply: null,
      });
    }
    throw new Error(`unexpected request: ${path}`);
  });

  renderRunPage();
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", { name: "审批并执行此计划" }),
  );
  const dialog = screen.getByRole("dialog", { name: "确认文件移动" });
  expect(
    within(dialog).getByText(/部分成功会保留且不会自动回滚/),
  ).toBeVisible();
  await user.click(within(dialog).getByRole("checkbox"));
  await user.click(within(dialog).getByRole("button", { name: "批准并执行" }));

  expect(
    await screen.findByRole("heading", { name: "前向执行：部分完成" }),
  ).toBeVisible();
  expect(screen.queryByText("执行定向恢复")).toBeNull();
  expect(screen.getByText("directory_fsync_unsupported")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "重新扫描当前目录" }),
  ).toBeNull();
  expect(screen.getByRole("button", { name: "删除记录" })).toBeVisible();
});

test("hides empty history, then shows the durable reply before plan review", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  const reply = `<script>alert("plain text only")</script>`;
  const persistedReply = `${reply} persisted`;
  let historyReads = 0;
  let releaseHistory: ((response: Response) => void) | undefined;
  const persistedHistory = new Promise<Response>((resolve) => {
    releaseHistory = resolve;
  });

  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === `/api/v1/runs/${encodedRunId}`) {
      return jsonResponse(runResponse());
    }
    if (path === `/api/v1/runs/${encodedRunId}/plans?limit=100`) {
      return jsonResponse({
        items: [
          {
            run_id: runId,
            version: 1,
            plan_hash: planHash,
            parent_plan_hash: null,
            plan_kind: "initial",
            created_at: "2026-07-29T00:00:00Z",
          },
        ],
      });
    }
    if (
      path ===
      `/api/v1/runs/${encodedRunId}/plans/1/preview?after=0&limit=100`
    ) {
      return jsonResponse(previewResponse());
    }
    if (
      path.startsWith(
        `/api/v1/runs/${encodedRunId}/interactions?limit=100`,
      )
    ) {
      historyReads += 1;
      return historyReads === 1
        ? jsonResponse({ items: [] })
        : persistedHistory;
    }
    if (
      path ===
      `/api/v1/runs/${encodedRunId}/events?after=0&limit=100`
    ) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events/stream`) {
      return new Response(": keepalive\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (
      path === `/api/v1/runs/${encodedRunId}/interactions` &&
      init?.method === "POST"
    ) {
      expect(JSON.parse(String(init.body))).toEqual({
        kind: "question",
        message: "为什么这样整理？",
      });
      return jsonResponse({
        interaction_id: "interaction-new",
        kind: "question",
        assistant_reply: reply,
        plan_hash: null,
        model_tokens: 42,
      });
    }
    throw new Error(`unexpected request: ${path}`);
  });

  renderRunPage();

  expect(
    await screen.findByRole("heading", { name: "运行详情" }),
  ).toBeVisible();
  expect(screen.getByText("run-0dae51fc…8f420272")).toBeVisible();
  await screen.findByRole("heading", { name: "计划审查" });
  await waitFor(() => expect(historyReads).toBe(1));
  expect(screen.queryByRole("heading", { name: "交互历史" })).toBeNull();

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "向 Agent 提问" }));
  await user.type(
    screen.getByLabelText("向 Agent 提问"),
    "为什么这样整理？",
  );
  await user.click(screen.getByRole("button", { name: "提交" }));

  expect(await screen.findByText(reply)).toBeVisible();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getAllByText(reply)).toHaveLength(1);
  const historyPanel = screen
    .getByRole("heading", { name: "交互历史" })
    .closest("section");
  expect(historyPanel).not.toBeNull();
  expect(historyPanel).toBe(document.querySelector(".run-main > section"));

  releaseHistory?.(
    jsonResponse({
      items: [
        {
          interaction_id: "interaction-new",
          kind: "question",
          status: "completed",
          request_message: "为什么这样整理？",
          assistant_reply: persistedReply,
          content_available: true,
          plan_hash: null,
          created_at: "2026-07-29T00:00:00Z",
          finished_at: "2026-07-29T00:00:01Z",
        },
      ],
    }),
  );
  expect(await screen.findByText(persistedReply)).toBeVisible();
  expect(screen.queryByText(reply)).toBeNull();
});

test("executes only the independently authorized subtitle action", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  const subtitlePlanHash = `sha256:${"b".repeat(64)}`;
  const actionId = `runaction-v1:${"3".repeat(64)}`;
  let published = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === `/api/v1/runs/${encodedRunId}`) {
      return jsonResponse({
        ...runResponse(),
        status: published ? "running" : "awaiting_approval",
        phase: published ? "execution_queued" : "awaiting_approval",
        plan_hash: subtitlePlanHash,
        available_actions: published ? [] : ["execute"],
        lifecycle: {
          schema_version: 1,
          mode: "forward_v2",
          state: published ? "execution_queued" : "awaiting_approval",
          terminal: false,
          revision: published ? 2 : 1,
          active_plan: {
            family: "subtitle_acquire",
            plan_hash: subtitlePlanHash,
          },
          operation_id: published ? "operation:subtitle" : null,
          operation_status: published ? "authorized" : null,
          rescan_state: null,
          successor_run_id: null,
          housekeeping: { state: null, warning: null },
          actions: published
            ? []
            : [{
                action_id: actionId,
                kind: "execute",
                input: "confirmation",
                destructive: true,
              }],
          etag: `runpresentation-v1:${"4".repeat(64)}`,
        },
        subtitle_acquisition: {
          plan_hash: subtitlePlanHash,
          policy: "manual",
          status: published ? "approved" : "planned",
          transaction_id: published ? "operation:subtitle" : null,
          failure_code: null,
          failure_diagnostic: null,
          successor_status: null,
        },
      });
    }
    if (path.startsWith(`/api/v1/runs/${encodedRunId}/interactions?`)) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events?after=0&limit=100`) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events/stream`) {
      return new Response(": keepalive\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (
      path ===
        `/api/v1/runs/${encodedRunId}/actions/${encodeURIComponent(actionId)}` &&
      init?.method === "POST"
    ) {
      expect(JSON.parse(String(init.body))).toEqual({ message: null });
      published = true;
      return jsonResponse({
        action_id: actionId,
        kind: "execute",
        run: {
          ...runResponse(),
          status: "running",
          phase: "execution_queued",
          plan_hash: subtitlePlanHash,
          available_actions: [],
          lifecycle: {
            schema_version: 1,
            mode: "forward_v2",
            state: "execution_queued",
            terminal: false,
            revision: 2,
            active_plan: {
              family: "subtitle_acquire",
              plan_hash: subtitlePlanHash,
            },
            operation_id: "operation:subtitle",
            operation_status: "authorized",
            rescan_state: null,
            successor_run_id: null,
            housekeeping: { state: null, warning: null },
            actions: [],
            etag: `runpresentation-v1:${"5".repeat(64)}`,
          },
          subtitle_acquisition: {
            plan_hash: subtitlePlanHash,
            policy: "manual",
            status: "approved",
            transaction_id: "operation:subtitle",
            failure_code: null,
            failure_diagnostic: null,
            successor_status: null,
          },
        },
        assistant_reply: null,
      });
    }
    throw new Error(`unexpected request: ${path}`);
  });

  renderRunPage();

  const button = await screen.findByRole("button", {
    name: "审批并获取字幕",
  });
  await userEvent.click(button);

  expect(await screen.findByText("请求已按服务端当前状态处理。"))
    .toBeVisible();
  expect(screen.queryByRole("button", {
    name: "审批并获取字幕",
  })).toBeNull();
});

test("shows subtitle operation terminal actions without recovery controls", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  const subtitlePlanHash = `sha256:${"b".repeat(64)}`;
  const rescanActionId = `runaction-v1:${"6".repeat(64)}`;
  const deleteActionId = `runaction-v1:${"7".repeat(64)}`;
  let deleted = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === `/api/v1/runs/${encodedRunId}`) {
      return jsonResponse({
        ...runResponse(),
        status: "failed",
        phase: "failed",
        runtime_status: "failed",
        plan_hash: subtitlePlanHash,
        available_actions: ["rescan", "delete_run"],
        execution: {
          operation_id: "operation:subtitle",
          operation_kind: "subtitle_acquire",
          plan_hash: subtitlePlanHash,
          status: "collision",
          attempt_count: 1,
          counts: {
            satisfied: 0,
            stale: 0,
            collision: 1,
            unsafe: 0,
            unavailable: 0,
          },
          items: [{
            source_id: "subtitle-publication",
            outcome: "collision",
            diagnostic: "collision",
          }],
          warnings: [],
          fresh_scan_required: true,
          rescan_state: null,
          successor_run_id: null,
        },
        lifecycle: {
          schema_version: 1,
          mode: "forward_v2",
          state: "failed",
          terminal: true,
          revision: 3,
          active_plan: {
            family: "subtitle_acquire",
            plan_hash: subtitlePlanHash,
          },
          operation_id: "operation:subtitle",
          operation_status: "collision",
          rescan_state: null,
          successor_run_id: null,
          housekeeping: { state: null, warning: null },
          actions: [
            {
              action_id: rescanActionId,
              kind: "request_rescan",
              input: "none",
              destructive: false,
            },
            {
              action_id: deleteActionId,
              kind: "delete_run",
              input: "confirmation",
              destructive: true,
            },
          ],
          etag: `runpresentation-v1:${"8".repeat(64)}`,
        },
        subtitle_acquisition: {
          plan_hash: subtitlePlanHash,
          policy: "automatic",
          status: "blocked",
          transaction_id: "operation:subtitle",
          failure_code: "destination_collision",
          failure_diagnostic: null,
          successor_status: null,
        },
      });
    }
    if (path.startsWith(`/api/v1/runs/${encodedRunId}/interactions?`)) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events?after=0&limit=100`) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events/stream`) {
      return new Response(": keepalive\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (
      path ===
        `/api/v1/runs/${encodedRunId}/actions/${encodeURIComponent(deleteActionId)}` &&
      init?.method === "POST"
    ) {
      expect(JSON.parse(String(init.body))).toEqual({ message: null });
      deleted = true;
      return jsonResponse({
        action_id: deleteActionId,
        kind: "delete_run",
        run: null,
        assistant_reply: null,
      });
    }
    throw new Error(`unexpected request: ${path}`);
  });

  renderRunPage();

  expect(
    await screen.findByRole("heading", { name: "字幕发布：目标碰撞" }),
  ).toBeVisible();
  expect(screen.queryByRole("button", { name: "重试字幕获取" })).toBeNull();
  expect(screen.queryByRole("button", { name: "结束此运行" })).toBeNull();
  expect(screen.queryByText("执行定向恢复")).toBeNull();
  expect(
    screen.getByRole("button", { name: "重新扫描当前目录" }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "删除记录" }),
  ).toBeVisible();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "删除记录" }));
  const armed = screen.getByRole("button", { name: "再点一次删除" });
  await new Promise((resolve) => setTimeout(resolve, 450));
  await user.click(armed);
  await waitFor(() => expect(deleted).toBe(true));
});


test("offers opaque controls for a planless needs-attention run", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  const retryActionId = `runaction-v1:${"9".repeat(64)}`;
  const failActionId = `runaction-v1:${"a".repeat(64)}`;
  const askActionId = `runaction-v1:${"b".repeat(64)}`;
  const calls: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === `/api/v1/runs/${encodedRunId}`) {
      return jsonResponse({
        ...runResponse(),
        status: "needs_attention",
        phase: "map_episodes",
        event_sequence: 37,
        plan_hash: null,
        available_actions: ["question", "retry_run", "fail_run"],
        lifecycle: {
          schema_version: 1,
          mode: "forward_v2",
          state: "needs_attention",
          terminal: false,
          revision: 1,
          active_plan: null,
          operation_id: null,
          operation_status: null,
          rescan_state: null,
          successor_run_id: null,
          housekeeping: { state: null, warning: null },
          actions: [
            {
              action_id: retryActionId,
              kind: "retry_agent",
              input: "none",
              destructive: false,
            },
            {
              action_id: failActionId,
              kind: "mark_failed",
              input: "confirmation",
              destructive: true,
            },
            {
              action_id: askActionId,
              kind: "ask_agent",
              input: "message",
              destructive: false,
            },
          ],
          etag: `runpresentation-v1:${"c".repeat(64)}`,
        },
      });
    }
    if (path.startsWith(`/api/v1/runs/${encodedRunId}/interactions?`)) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events?after=0&limit=100`) {
      return jsonResponse({ items: [] });
    }
    if (path === `/api/v1/runs/${encodedRunId}/events/stream`) {
      return new Response(": keepalive\n\n", {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (init?.method === "POST") {
      if (path.endsWith(`/actions/${encodeURIComponent(askActionId)}`)) {
        calls.push("question");
        return jsonResponse({
          action_id: askActionId,
          kind: "ask_agent",
          run: null,
          assistant_reply: "字幕证据不明确。",
        });
      }
      if (path.endsWith(`/actions/${encodeURIComponent(retryActionId)}`)) {
        calls.push("retry");
        return jsonResponse({
          action_id: retryActionId,
          kind: "retry_agent",
          run: null,
          assistant_reply: null,
        });
      }
      if (path.endsWith(`/actions/${encodeURIComponent(failActionId)}`)) {
        calls.push("fail");
        return jsonResponse({
          action_id: failActionId,
          kind: "mark_failed",
          run: null,
          assistant_reply: null,
        });
      }
    }
    throw new Error(`unexpected request: ${path}`);
  });

  renderRunPage();
  expect(await screen.findByText("需要处理")).toBeVisible();
  const actionList = screen.getByRole("group", { name: "可用操作" });
  expect(getComputedStyle(actionList).display).toBe("grid");
  expect(getComputedStyle(actionList).gap).toBe("10px");
  expect(
    within(actionList).getAllByRole("button").map((button) => button.textContent),
  ).toEqual(["重新尝试", "标记失败", "向 Agent 提问"]);
  const user = userEvent.setup();

  await user.click(screen.getByRole("button", { name: "向 Agent 提问" }));
  await user.type(screen.getByLabelText("向 Agent 提问"), "为什么？");
  await user.click(screen.getByRole("button", { name: "提交" }));
  expect(await screen.findByText("字幕证据不明确。")).toBeVisible();

  await user.click(screen.getByRole("button", { name: "重新尝试" }));
  expect(await screen.findByText("请求已按服务端当前状态处理。"))
    .toBeVisible();

  await user.click(screen.getByRole("button", { name: "标记失败" }));
  expect(calls).toEqual(["question", "retry", "fail"]);
});

function renderRunPage() {
  render(
    <QueryClientProvider
      client={new QueryClient({
        defaultOptions: { queries: { retry: false } },
      })}
    >
      <AuthGate>
        <RunPage runId={runId} />
      </AuthGate>
    </QueryClientProvider>,
  );
}

function runResponse() {
  return {
    run_id: runId,
    status: "awaiting_approval",
    work_type: "anime",
    phase: "awaiting_approval",
    runtime_status: "stopped",
    event_sequence: 1,
    model_turns: 2,
    model_tokens: 300,
    tool_calls: 4,
    failures: 0,
    plan_hash: planHash,
    recovery_approval_id: null,
    apply_policy: "manual",
    available_actions: ["question"],
    settlement: null,
    source_folder: null,
    folder_disposition: null,
    archive_report: null,
  };
}

function previewResponse() {
  return {
    run_id: runId,
    version: 1,
    plan_hash: planHash,
    plan_kind: "initial",
    counts: { move: 1, unmapped: 0, unchanged: 0 },
    review: {
      status: "system_only",
      agent_summary: null,
      advisory_only: true,
      coverage: {
        total_unmapped: 0,
        agent_explained: 0,
        system_verified: 0,
        fallback: 0,
      },
    },
    items: [
      {
        index: 0,
        disposition: "move",
        candidate_id: "video:1",
        kind: "video",
        source: "episode.mkv",
        destination: "Series/Season 01/Series - S01E01.mkv",
        explanation: null,
      },
    ],
    next_after: null,
  };
}

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}
