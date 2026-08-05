import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TOKEN_STORAGE_KEY } from "../src/api";
import { AuthGate } from "../src/auth";
import { RunPage } from "../src/pages/RunPage";

const runId = "run-0dae51fc45a8db238e0b901e8f420272";
const encodedRunId = encodeURIComponent(runId);
const planHash = `sha256:${"a".repeat(64)}`;

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
  let published = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === `/api/v1/runs/${encodedRunId}`) {
      return jsonResponse({
        ...runResponse(),
        status: published ? "superseded" : "running",
        phase: "build_subtitle_acquisition_plan",
        plan_hash: null,
        available_actions: published
          ? []
          : ["approve_subtitle_acquisition"],
        subtitle_acquisition: {
          plan_hash: subtitlePlanHash,
          policy: "manual",
          status: published ? "published" : "planned",
          approval_id: published ? "approval-subtitle-1" : null,
          transaction_id: published
            ? `subtitle-txn-v1-${"c".repeat(64)}`
            : null,
          failure_code: null,
          successor_status: published ? "queued" : null,
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
        `/api/v1/runs/${encodedRunId}/subtitle-acquisition/approve` &&
      init?.method === "POST"
    ) {
      expect(init.headers).toMatchObject({ "If-Match": subtitlePlanHash });
      expect(JSON.parse(String(init.body))).toEqual({});
      published = true;
      return jsonResponse({
        run_id: runId,
        plan_hash: subtitlePlanHash,
        policy: "manual",
        status: "published",
        approval_id: "approval-subtitle-1",
        transaction_id: `subtitle-txn-v1-${"c".repeat(64)}`,
        failure_code: null,
        successor_status: "queued",
      });
    }
    throw new Error(`unexpected request: ${path}`);
  });

  renderRunPage();

  const button = await screen.findByRole("button", {
    name: "审批并获取字幕",
  });
  await userEvent.click(button);

  expect(await screen.findByRole("heading", {
    name: "字幕获取：published",
  })).toBeVisible();
  expect(screen.queryByRole("button", {
    name: "审批并获取字幕",
  })).toBeNull();
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
