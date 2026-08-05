import { expect, test } from "@playwright/test";

const adminToken = "admin-e2e-token-strong";

test("serves the dashboard from the real API and PostgreSQL control plane", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Admin Bearer token").fill(adminToken);
  await page.getByRole("button", { name: "进入控制台" }).click();

  await expect(
    page.getByRole("heading", { name: "今天的整理进度，一眼看清。" }),
  ).toBeVisible();
  await expect(page.getByText(/PostgreSQL \d+ · Schema \d+/)).toBeVisible();
  const session = await page.evaluate(async () => {
    const response = await fetch("/api/v1/session", {
      headers: {
        Authorization: `Bearer ${window.localStorage.getItem(
          "reeloom.admin_bearer.v1",
        )}`,
      },
    });
    return response.json();
  });
  expect(session).toEqual({ api_version: "1.0.0", role: "admin" });
});

test("admin can enter the same-origin dashboard and untrusted text stays text", async ({
  page,
}) => {
  const maliciousRunId = '<img src=x onerror="window.pwned=true">';
  await page.route("**/health", (route) =>
    route.fulfill({
      json: { status: "ok", postgres_major: 17, schema_version: 19 },
    }),
  );
  await page.route("**/api/v1/session", async (route) => {
    expect(route.request().headers().authorization).toBe(
      `Bearer ${adminToken}`,
    );
    await route.fulfill({
      json: { api_version: "1.0.0", role: "admin" },
    });
  });
  await page.route("**/api/v1/runs?limit=50", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            run_id: maliciousRunId,
            status: "awaiting_approval",
            work_type: "anime",
            created_at: "2026-07-26T00:00:00Z",
            phase: "awaiting_approval",
            plan_hash: `sha256:${"a".repeat(64)}`,
            source_folder: null,
            available_actions: [],
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/discoveries?limit=50", (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route("**/api/v1/admin/config", (route) =>
    route.fulfill({
      json: {
        revision: 1,
        revision_id: "config-1",
        watches: [],
        provider: {
          base_url: "https://api.openai.com/v1",
          model: "gpt-5",
          reasoning_effort: "medium",
          verbosity: "medium",
          api_key_configured: true,
        },
        apply_policy: "manual",
      },
    }),
  );

  await page.goto("/");
  await page.getByLabel("Admin Bearer token").fill(adminToken);
  await page.getByRole("button", { name: "进入控制台" }).click();
  await expect(page.getByRole("heading", { name: "今天的整理进度，一眼看清。" })).toBeVisible();
  await expect(page.locator(".runs-table tbody a[title]")).toHaveAttribute(
    "title",
    maliciousRunId,
  );
  await expect(page.locator("img")).toHaveCount(0);
  expect(await page.evaluate(() => Reflect.get(window, "pwned"))).toBeUndefined();
  expect(await page.evaluate(() => document.body.innerText)).not.toContain(
    "admin-e2e-token",
  );
});

test("exact approval sends manual intent and waits for durable settlement", async ({
  page,
}) => {
  const planHash = `sha256:${"b".repeat(64)}`;
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "reeloom.admin_bearer.v1",
      "admin-e2e-token-strong",
    );
  });
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({ json: { api_version: "1.0.0", role: "admin" } }),
  );
  let settled = false;
  await page.route("**/api/v1/runs/run-1", (route) =>
    route.fulfill({
      json: {
        run_id: "run-1",
        status: "awaiting_approval",
        work_type: "anime",
        phase: "awaiting_approval",
        runtime_status: "stopped",
        event_sequence: 4,
        model_turns: 2,
        model_tokens: 320,
        tool_calls: 6,
        failures: 0,
        plan_hash: planHash,
        recovery_approval_id: null,
        apply_policy: "manual",
        available_actions: ["question", "revision", "approve_apply"],
        settlement: settled
          ? {
              approval_id: "approval-v1-test",
              plan_hash: planHash,
              transaction_id: "txn-v1-test",
              status: "completed",
              applied_count: 1,
              rolled_back_count: 0,
              failure_code: null,
              settled_at: "2026-07-26T00:00:02Z",
            }
          : null,
        source_folder: null,
        folder_disposition: null,
        archive_report: null,
      },
    }),
  );
  await page.route("**/api/v1/runs/run-1/plans?limit=100", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            run_id: "run-1",
            version: 1,
            plan_hash: planHash,
            parent_plan_hash: null,
            plan_kind: "initial",
            created_at: "2026-07-26T00:00:00Z",
          },
        ],
      },
    }),
  );
  await page.route("**/api/v1/runs/run-1/plans/1/preview?**", (route) =>
    route.fulfill({
      json: {
        run_id: "run-1",
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
      },
    }),
  );
  await page.route("**/api/v1/runs/run-1/interactions?**", (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route("**/api/v1/runs/run-1/events?**", (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route("**/api/v1/runs/run-1/events/stream", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: ": keepalive\n\n",
    }),
  );
  let approvalBody: unknown;
  let approvalHeaders: Record<string, string> = {};
  await page.route("**/api/v1/runs/run-1/approve-and-apply", async (route) => {
    approvalBody = route.request().postDataJSON();
    approvalHeaders = route.request().headers();
    settled = true;
    await route.fulfill({
      json: {
        transaction_id: "txn-v1-test",
        plan_hash: planHash,
        approval_id: "approval-v1-test",
        status: "completed",
        applied_count: 1,
        rolled_back_count: 0,
        folder_disposition: null,
      },
    });
  });

  await page.goto("/#/runs/run-1");
  await expect(
    page.getByRole("heading", { name: "运行详情" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "交互历史" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "计划说明" }),
  ).toBeVisible();
  await expect(page.getByText("仅系统证据")).toBeVisible();
  const approveButton = page.getByRole("button", {
    name: "审批并执行此计划",
  });
  await approveButton.click();
  await expect(page.getByLabel(/我已审查计划哈希/)).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(approveButton).toBeFocused();
  await approveButton.click();
  await expect(page.getByText(planHash)).toBeVisible();
  await page
    .getByLabel(/我已审查计划哈希/)
    .check();
  await page.getByRole("button", { name: "批准并执行" }).click();

  await expect(page.getByText("执行状态：已完成")).toBeVisible();
  expect(approvalBody).toEqual({
    automatic: false,
    folder_disposition_plan_hash: null,
  });
  expect(approvalHeaders["if-match"]).toBe(planHash);
  expect(approvalHeaders["idempotency-key"]).toMatch(/^ui-v1-/);
});

test("Movie review shows exact paths and completed reapply converges to no-op", async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "reeloom.admin_bearer.v1",
      "admin-e2e-token-strong",
    );
  });

  await page.goto("/");
  const movieRun = page.locator("tbody tr").filter({ hasText: "电影" }).first();
  await expect(movieRun).toContainText("已完成");
  await movieRun.getByRole("link").click();
  await expect(
    page.getByRole("heading", { name: "Journey" }),
  ).toBeVisible();
  const compactRunId = page.locator(".run-id-row code");
  await expect(compactRunId).toHaveText(/^run-.+….{8}$/);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".run-id-row")).toHaveCSS("flex-wrap", "nowrap");
  await expect(compactRunId).toHaveCSS("white-space", "nowrap");
  expect(
    await compactRunId.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true);
  await expect(page.getByText("电影", { exact: true })).toBeVisible();
  await expect(
    page.getByText("旅程电影 (2025) {tmdb-700}/旅程电影 (2025).mkv"),
  ).toBeVisible();
  await expect(page.getByText("zz-extra.mkv")).toBeVisible();
  await expect(page.getByText("执行状态：已完成")).toBeVisible();
  await page.getByRole("button", { name: "重新整理已完成布局" }).click();
  await page.getByLabel("重新整理已完成布局").fill("复验当前布局");
  const historyRefreshed = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().includes("/interactions?"),
  );
  await page.getByRole("button", { name: "提交" }).click();

  await expect(
    page.getByText(
      "布局没有变化；服务端保留了原有计划，未生成空的修订版本。",
    ),
  ).toBeVisible();
  await historyRefreshed;
  const historyHeading = page.getByRole("heading", { name: "交互历史" });
  await expect(historyHeading).toBeVisible();
  const newestInteraction = page.locator(".interaction-history article").first();
  await expect(newestInteraction).toContainText("复验当前布局");
  await expect(newestInteraction.locator(".assistant-message")).toHaveCount(1);
  await expect(page.locator(".run-main > section").first()).toHaveClass(
    /interaction-history/,
  );
});
