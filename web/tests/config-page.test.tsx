import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { TOKEN_STORAGE_KEY } from "../src/api";
import { AuthGate } from "../src/auth";
import { ConfigPage } from "../src/pages/ConfigPage";
import "../src/styles.css";

test("uses sixteen failures for a new configuration", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({ api_version: "1.0.0", role: "admin" }))
    .mockResolvedValueOnce(new Response(null, { status: 404 }));

  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthGate>
        <ConfigPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  expect(
    await screen.findByLabelText("失败次数上限"),
  ).toHaveValue(16);
});

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
        subtitle_acquisition: {
          enabled: false,
          provider: "acgrip",
          policy: "automatic",
        },
      }],
      provider: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        reasoning_effort: "medium",
        verbosity: "medium",
        api_key_configured: true,
      },
      telegram: {
        enabled: false,
        notification_types: [
          "plan_ready",
          "archive_completed",
          "attention_required",
        ],
        destination_configured: false,
      },
      apply_policy: "manual",
      agent_budget: {
        max_model_turns: 64,
        max_tool_calls: 64,
        max_failures: 3,
        max_total_tokens: 100_000,
        max_elapsed_seconds: 600,
      },
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
  expect(screen.getByLabelText("单次操作时间上限（秒）")).toHaveValue(600);
  expect(screen.getByLabelText("Token 总上限")).toHaveValue(100_000);

  const workTypes = screen.getAllByLabelText("内容类型");
  await user.selectOptions(workTypes[0]!, "movie");
  expect(workTypes[0]).toHaveFocus();
  expect(screen.getByRole("option", {
    name: "当前类型暂无可用来源",
  })).toBeVisible();
  expect(screen.getByRole("button", { name: "启用" })).toBeDisabled();
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
        subtitle_acquisition: {
          enabled: false,
          provider: "acgrip",
          policy: "automatic",
        },
      }],
      provider: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        reasoning_effort: "medium",
        verbosity: "medium",
        api_key_configured: true,
      },
      telegram: {
        enabled: false,
        notification_types: ["attention_required"],
        destination_configured: false,
      },
      apply_policy: "manual",
      agent_budget: {
        max_model_turns: 64,
        max_tool_calls: 64,
        max_failures: 3,
        max_total_tokens: 100_000,
        max_elapsed_seconds: 600,
      },
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
      telegram: {
        enabled: true,
        notification_types: ["plan_ready", "archive_completed"],
        destination_configured: true,
      },
      apply_policy: "manual",
      agent_budget: {
        max_model_turns: 64,
        max_tool_calls: 64,
        max_failures: 3,
        max_total_tokens: 100_000,
        max_elapsed_seconds: 600,
      },
    }))
    .mockResolvedValueOnce(jsonResponse({
      available: true,
      status_code: 200,
    }))
    .mockResolvedValueOnce(jsonResponse({
      notification_id: "notification-test",
      state: "queued",
    }));

  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthGate>
        <ConfigPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  const telegram = (await screen.findByRole("heading", {
    name: "Telegram 通知",
  })).closest("section");
  expect(telegram).not.toBeNull();
  const telegramToggle = within(telegram!).getByRole("checkbox", {
    name: "启用 Telegram 推送",
  });
  expect(telegramToggle).toBeChecked();
  expect(getComputedStyle(telegramToggle).width).toBe("17px");
  expect(getComputedStyle(telegramToggle.closest("label")!).alignItems).toBe(
    "center",
  );
  expect(
    within(telegram!).getByText(/目标与 Bot Token 已配置/),
  ).toBeVisible();
  expect(
    within(telegram!).getByRole("checkbox", { name: "计划待批准" }),
  ).toBeChecked();
  expect(
    within(telegram!).getByRole("checkbox", { name: "需要处理" }),
  ).not.toBeChecked();

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
  await userEvent.click(
    within(telegram!).getByRole("button", { name: "发送测试通知" }),
  );
  expect(
    await within(telegram!).findByText("测试通知已加入发送队列。"),
  ).toBeVisible();
});

test("shows bounded move capability below its watch", async () => {
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
        subtitle_acquisition: {
          enabled: false,
          provider: "acgrip",
          policy: "automatic",
        },
      }],
      provider: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        reasoning_effort: "medium",
        verbosity: "medium",
        api_key_configured: true,
      },
      telegram: {
        enabled: false,
        notification_types: ["attention_required"],
        destination_configured: false,
      },
      apply_policy: "manual",
      agent_budget: {
        max_model_turns: 64,
        max_tool_calls: 64,
        max_failures: 3,
        max_total_tokens: 100_000,
        max_elapsed_seconds: 600,
      },
    }))
    .mockResolvedValueOnce(jsonResponse({
      watch_id: "primary",
      move_backend: "fuse_checked_rename",
      folder_disposition: {
        status: "degraded",
        failure_code: null,
      },
      media_apply: {
        status: "cross_filesystem",
        failure_code: "cross_filesystem",
      },
    }));

  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthGate>
        <ConfigPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  await screen.findByText("/media/incoming/anime");
  await userEvent.click(
    screen.getByRole("button", { name: "检测移动兼容性" }),
  );

  expect(
    await screen.findByText(/文件夹收尾：FUSE 降级移动/),
  ).toBeVisible();
  expect(screen.getByText(/媒体执行：跨文件系统/)).toBeVisible();
});

test("shows watch-scoped subtitle acquisition sources and policy", async () => {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, "admin-token");
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(jsonResponse({ api_version: "1.0.0", role: "admin" }))
    .mockResolvedValueOnce(jsonResponse({
      revision: 1,
      revision_id: "revision-1",
      watches: [
        {
          watch_id: "anime-watch",
          work_type: "anime",
          poll_interval_seconds: 30,
          settle_interval_seconds: 120,
          root: "/media/incoming/anime",
          library_root: "/media/library/anime",
          subtitle_acquisition: {
            enabled: true,
            provider: "acgrip",
            policy: "manual",
          },
        },
        {
          watch_id: "movie-watch",
          work_type: "movie",
          poll_interval_seconds: 30,
          settle_interval_seconds: 120,
          root: "/media/incoming/movies",
          library_root: "/media/library/movies",
          subtitle_acquisition: {
            enabled: false,
            provider: null,
            policy: "automatic",
          },
        },
      ],
      provider: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        reasoning_effort: "medium",
        verbosity: "medium",
        api_key_configured: true,
      },
      telegram: {
        enabled: false,
        notification_types: ["attention_required"],
        destination_configured: false,
      },
      apply_policy: "plan_only",
      agent_budget: {
        max_model_turns: 64,
        max_tool_calls: 64,
        max_failures: 3,
        max_total_tokens: 100_000,
        max_elapsed_seconds: 600,
      },
    }));

  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthGate>
        <ConfigPage />
      </AuthGate>
    </QueryClientProvider>,
  );

  await screen.findByText("/media/incoming/anime");
  expect(screen.queryByRole("heading", {
    name: "动漫字幕自动获取",
  })).toBeNull();
  const animeCard = screen
    .getByText("/media/incoming/anime")
    .closest<HTMLElement>(".form-card");
  expect(animeCard).not.toBeNull();
  const acquisition = within(animeCard!).getByRole("group", {
    name: "字幕自动获取",
  });
  const enableButton = within(acquisition).getByRole("button", {
    name: "已启用",
    pressed: true,
  });
  const source = within(acquisition).getByRole("combobox", {
    name: "监听 1 字幕来源",
  });
  expect(enableButton).toBeVisible();
  expect(source).toHaveValue("acgrip");
  expect(within(acquisition).getByRole("option", {
    name: "ACG.RIP 动漫字幕论坛",
  })).toBeVisible();
  expect(within(acquisition).getByRole("radio", {
    name: "人工审批",
  })).toBeChecked();
  expect(within(acquisition).getAllByRole("radio")).toHaveLength(3);

  const movieCard = screen
    .getByText("/media/incoming/movies")
    .closest<HTMLElement>(".form-card");
  expect(movieCard).not.toBeNull();
  const movieAcquisition = within(movieCard!).getByRole("group", {
    name: "字幕自动获取",
  });
  expect(within(movieAcquisition).getByRole("button", {
    name: "启用",
  })).toBeDisabled();
  expect(within(movieAcquisition).getByRole("option", {
    name: "当前类型暂无可用来源",
  })).toBeVisible();

  await userEvent.click(enableButton);
  expect(within(acquisition).getByRole("button", {
    name: "启用",
    pressed: false,
  })).toBeVisible();
  expect(source).toBeDisabled();
  for (const radio of within(acquisition).getAllByRole("radio")) {
    expect(radio).toBeDisabled();
  }
});

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
