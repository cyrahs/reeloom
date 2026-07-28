import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TOKEN_STORAGE_KEY } from "../src/api";
import { AuthGate } from "../src/auth";
import { ConfigPage } from "../src/pages/ConfigPage";

test("keeps internal watch identity hidden and displays configured paths", async () => {
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
        root: "/media/incoming/anime",
        library_root: "/media/library/anime",
      }],
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
  await screen.findByText("/media/incoming/anime");
  expect(screen.getByText("/media/library/anime")).toBeVisible();
  expect(screen.queryByLabelText("Watch ID")).not.toBeInTheDocument();

  const workTypes = screen.getAllByLabelText("内容类型");
  await user.selectOptions(workTypes[0]!, "movie");
  expect(workTypes[0]).toHaveFocus();
  expect(screen.getByPlaceholderText("/media/incoming/anime")).toHaveValue("");
  expect(screen.getByPlaceholderText("/media/library/anime")).toHaveValue("");
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
        root: "/media/incoming/anime",
        library_root: "/media/library/anime",
      }],
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
  await screen.findByText("/media/incoming/anime");
  await user.click(screen.getAllByRole("button", { name: "替换" })[0]!);
  await user.click(screen.getByRole("button", { name: "浏览入站目录" }));
  await screen.findByRole("dialog", { name: "选择入站目录" });
  await screen.findByText("/srv/media");
  await user.click(screen.getByRole("button", { name: "选择当前目录" }));

  expect(screen.getByPlaceholderText("/media/incoming/anime")).toHaveValue(
    "/srv/media",
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("shows provider probe result inside the provider section", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({ api_version: "1.0.0", role: "admin" }))
    .mockResolvedValueOnce(jsonResponse({
      revision: 1,
      revision_id: "revision-1",
      watches: [],
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
      available: true,
      status_code: 200,
    }));

  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthGate>
        <ConfigPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  const provider = (await screen.findByRole("heading", {
    name: "模型 Provider",
  })).closest("section");
  expect(provider).not.toBeNull();
  await userEvent.click(
    within(provider!).getByRole("button", { name: "探测当前 Provider" }),
  );

  expect(
    await within(provider!).findByText("Provider 连接正常。"),
  ).toBeVisible();
});

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
