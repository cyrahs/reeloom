import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TOKEN_STORAGE_KEY } from "../src/api";
import { AuthGate } from "../src/auth";
import { DashboardPage } from "../src/pages/DashboardPage";

test("offers deletion for eligible runs directly from the dashboard", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  let deleted = false;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === "/health") {
      return jsonResponse({
        status: "ok",
        postgres_major: 18,
        schema_version: 21,
      });
    }
    if (path.startsWith("/api/v1/runs?")) {
      return jsonResponse({
        items: [
          {
            run_id: "run-deletable",
            status: "failed",
            work_type: "anime",
            created_at: "2026-07-28T12:00:00Z",
            phase: "stopped",
            plan_hash: null,
            source_folder: "folder-a",
            available_actions: ["delete_run"],
          },
          {
            run_id: "run-active",
            status: "running",
            work_type: "movie",
            created_at: "2026-07-28T11:00:00Z",
            phase: "identify_movie",
            plan_hash: null,
            source_folder: "folder-b",
            available_actions: [],
          },
        ].filter((item) => !deleted || item.run_id !== "run-deletable"),
      });
    }
    if (
      path === "/api/v1/runs/run-deletable" &&
      init?.method === "DELETE"
    ) {
      expect(
        (init.headers as Record<string, string>)["Idempotency-Key"],
      ).toMatch(/^ui-v1-/);
      deleted = true;
      return jsonResponse({
        run_id: "run-deletable",
        deleted_at: "2026-07-28T12:30:00Z",
      });
    }
    if (path.startsWith("/api/v1/discoveries?")) {
      return jsonResponse({ items: [] });
    }
    if (path.startsWith("/api/v1/folders?")) {
      return jsonResponse({
        items: [
          {
            watch_id: "watch-anime",
            source_folder: "folder-retrying",
            status: "settling",
            reason_code: null,
            stable_at: null,
            run_id: null,
            retry_count: 2,
          },
        ],
      });
    }
    if (path === "/api/v1/admin/config") {
      return jsonResponse(
        { error: { code: "config_not_found" } },
        404,
      );
    }
    throw new Error(`unexpected request: ${path}`);
  });

  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AuthGate>
        <DashboardPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  const user = userEvent.setup();
  const deletableRow = (await screen.findByText("run-deletable")).closest("tr");
  const activeRow = screen.getByText("run-active").closest("tr");
  expect(deletableRow).not.toBeNull();
  expect(activeRow).not.toBeNull();
  expect(
    within(deletableRow!).getByRole("button", { name: "删除记录" }),
  ).toBeVisible();
  expect(
    within(activeRow!).queryByRole("button", { name: "删除记录" }),
  ).toBeNull();
  expect(await screen.findByText("重试 2/3", { exact: false })).toBeVisible();

  // One click only arms the button; the run is still there.
  await user.click(
    within(deletableRow!).getByRole("button", { name: "删除记录" }),
  );
  const armed = within(deletableRow!).getByRole("button", {
    name: "再点一次删除",
  });
  expect(screen.getByText("run-deletable")).toBeVisible();

  // A double-click must not confirm; only a deliberate second click does.
  await user.click(armed);
  expect(screen.getByText("run-deletable")).toBeVisible();
  await settle();

  await user.click(armed);
  await waitFor(() =>
    expect(screen.queryByText("run-deletable")).toBeNull(),
  );
});

test("deletes a checkbox selection of runs in one action", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  const deleted = new Set<string>();
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/v1/session") {
      return jsonResponse({ api_version: "1.0.0", role: "admin" });
    }
    if (path === "/health") {
      return jsonResponse({
        status: "ok",
        postgres_major: 18,
        schema_version: 21,
      });
    }
    if (path.startsWith("/api/v1/runs?")) {
      return jsonResponse({
        items: [
          {
            run_id: "run-one",
            status: "failed",
            work_type: "anime",
            created_at: "2026-07-28T12:00:00Z",
            phase: "stopped",
            plan_hash: null,
            source_folder: "folder-a",
            available_actions: ["delete_run"],
          },
          {
            run_id: "run-two",
            status: "failed",
            work_type: "movie",
            created_at: "2026-07-28T11:00:00Z",
            phase: "stopped",
            plan_hash: null,
            source_folder: "folder-b",
            available_actions: ["delete_run"],
          },
          {
            run_id: "run-busy",
            status: "running",
            work_type: "movie",
            created_at: "2026-07-28T10:00:00Z",
            phase: "identify_movie",
            plan_hash: null,
            source_folder: "folder-c",
            available_actions: [],
          },
        ].filter((item) => !deleted.has(item.run_id)),
      });
    }
    if (init?.method === "DELETE" && path.startsWith("/api/v1/runs/")) {
      const runId = decodeURIComponent(path.slice("/api/v1/runs/".length));
      deleted.add(runId);
      return jsonResponse({
        run_id: runId,
        deleted_at: "2026-07-28T12:30:00Z",
      });
    }
    if (path.startsWith("/api/v1/discoveries?")) {
      return jsonResponse({ items: [] });
    }
    if (path.startsWith("/api/v1/folders?")) {
      return jsonResponse({ items: [] });
    }
    if (path === "/api/v1/admin/config") {
      return jsonResponse({ error: { code: "config_not_found" } }, 404);
    }
    throw new Error(`unexpected request: ${path}`);
  });

  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <AuthGate>
        <DashboardPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  const user = userEvent.setup();
  await screen.findByText("run-one");
  // A running run offers no checkbox, so "select all" cannot reach it.
  expect(screen.getByLabelText("选择运行 run-one")).toBeVisible();
  expect(screen.queryByLabelText("选择运行 run-busy")).toBeNull();

  await user.click(screen.getByLabelText("全选可删除的运行"));
  expect(screen.getByText("已选 2 条")).toBeVisible();

  await user.click(
    screen.getByRole("button", { name: "删除所选 2 条" }),
  );
  await settle();
  await user.click(
    screen.getByRole("button", { name: "再点一次删除 2 条" }),
  );

  await waitFor(() => expect(screen.queryByText("run-one")).toBeNull());
  expect(screen.queryByText("run-two")).toBeNull();
  expect(screen.getByText("run-busy")).toBeVisible();
  expect(deleted).toEqual(new Set(["run-one", "run-two"]));
});

/** Outlasts the settle window that swallows an accidental double-click. */
function settle() {
  return new Promise((resolve) => setTimeout(resolve, 450));
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
