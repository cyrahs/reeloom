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
      return jsonResponse({ items: [] });
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

  await user.click(
    within(deletableRow!).getByRole("button", { name: "删除记录" }),
  );
  expect(
    screen.getByRole("heading", { name: "删除运行记录？" }),
  ).toBeVisible();
  await user.click(screen.getByRole("checkbox"));
  await user.click(
    screen.getByRole("button", { name: "确认删除记录" }),
  );
  await waitFor(() =>
    expect(screen.queryByText("run-deletable")).toBeNull(),
  );
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
