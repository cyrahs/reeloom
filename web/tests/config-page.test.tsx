import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TOKEN_STORAGE_KEY } from "../src/api";
import { AuthGate } from "../src/auth";
import { ConfigPage } from "../src/pages/ConfigPage";

test("keeps editable config rows mounted while their identity changes", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({ api_version: "1.0.0", role: "admin" }))
    .mockResolvedValueOnce(jsonResponse({
      revision: 1,
      revision_id: "revision-1",
      watches: [{
        watch_id: "primary",
        work_type: "anime",
        poll_interval_seconds: 30,
        settle_interval_seconds: 120,
        root_configured: true,
      }],
      archive_routes: [{ work_type: "anime", root_configured: true }],
      provider: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        reasoning_effort: "medium",
        verbosity: "medium",
        api_key_configured: true,
      },
      apply_policy: "manual",
    }));

  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthGate>
        <ConfigPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  const user = userEvent.setup();
  const watchId = await screen.findByLabelText("Watch ID");
  await user.click(watchId);
  await user.keyboard("-renamed");
  expect(watchId).toHaveValue("primary-renamed");
  expect(watchId).toHaveFocus();

  const workTypes = screen.getAllByLabelText("内容类型");
  await user.selectOptions(workTypes[1]!, "movie");
  expect(workTypes[1]).toHaveFocus();
});

test("selects a pod directory without requiring manual path entry", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({ api_version: "1.0.0", role: "admin" }))
    .mockResolvedValueOnce(jsonResponse({
      revision: 1,
      revision_id: "revision-1",
      watches: [{
        watch_id: "primary",
        work_type: "anime",
        poll_interval_seconds: 30,
        settle_interval_seconds: 120,
        root_configured: true,
      }],
      archive_routes: [{ work_type: "anime", root_configured: true }],
      provider: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        reasoning_effort: "medium",
        verbosity: "medium",
        api_key_configured: true,
      },
      apply_policy: "manual",
    }))
    .mockResolvedValueOnce(jsonResponse({
      path: "srv/media",
      absolute_path: "/srv/media",
      parent: "srv",
      directories: [
        { name: "Anime", path: "srv/media/Anime" },
      ],
    }));

  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthGate>
        <ConfigPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  const user = userEvent.setup();
  await screen.findByLabelText("Watch ID");
  await user.click(screen.getAllByRole("button", { name: "替换" })[0]!);
  await user.click(screen.getByRole("button", { name: "浏览源目录" }));
  await screen.findByRole("dialog", { name: "选择源目录" });
  await screen.findByText("/srv/media");
  await user.click(screen.getByRole("button", { name: "选择当前目录" }));

  expect(screen.getByPlaceholderText("/media/incoming/anime")).toHaveValue(
    "/srv/media",
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
