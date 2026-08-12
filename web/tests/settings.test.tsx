import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WatchConfig } from "../src/api";
import { SettingsPage } from "../src/pages/Settings";

const CONFIG: WatchConfig = {
  id: "config-1",
  name: "动画收件箱",
  inbound_root: "/data/inbound",
  library_root: "/data/library",
  media_type: "anime",
  enabled: true,
  stability_seconds: 120,
  acquire_subtitles: false,
  subtitle_variant: "chs",
  notify: true,
  replace_enabled: false,
  replace_extra_dirs: [],
  replace_auto_ratio: 1.2,
};

const SETTINGS = {
  llm_base_url: "",
  llm_model: "",
  llm_reasoning_effort: "",
  telegram_chat_id: "",
  trash_retention_days: 3,
  tmdb_api_key_set: false,
  llm_api_key_set: false,
  telegram_bot_token_set: false,
};

function mockSettingsApi(config: WatchConfig = CONFIG) {
  const patches: Record<string, unknown>[] = [];
  const puts: Record<string, unknown>[] = [];
  const posts: Record<string, unknown>[] = [];
  const llmTest = {
    calls: 0,
    response: { ok: true, reply: "ok" } as Record<string, unknown>,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/settings/test-llm") && method === "POST") {
        llmTest.calls += 1;
        return { ok: true, status: 200, json: async () => llmTest.response };
      }
      if (url.endsWith("/settings") && method === "PUT") {
        puts.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return { ok: true, status: 200, json: async () => ({ updated: true }) };
      }
      if (url.endsWith("/settings")) {
        return { ok: true, status: 200, json: async () => SETTINGS };
      }
      if (url.endsWith("/configs") && method === "GET") {
        return { ok: true, status: 200, json: async () => ({ configs: [config] }) };
      }
      if (url.endsWith("/configs") && method === "POST") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        posts.push(body);
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...config, ...body, id: "config-2" }),
        };
      }
      if (url.endsWith(`/configs/${config.id}`) && method === "PATCH") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        patches.push(body);
        return { ok: true, status: 200, json: async () => ({ ...config, ...body }) };
      }
      throw new Error(`unexpected ${method} ${url}`);
    }),
  );
  return { patches, puts, posts, llmTest };
}

describe("settings page", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("edits a watch config through the inline form", async () => {
    const { patches } = mockSettingsApi();

    render(<SettingsPage />);

    fireEvent.click(await screen.findByText("编辑"));

    const nameInput = screen.getByDisplayValue("动画收件箱");
    fireEvent.change(nameInput, { target: { value: "新番收件箱" } });
    const form = nameInput.closest("form")!;
    fireEvent.change(within(form).getByDisplayValue("120"), {
      target: { value: "300" },
    });
    fireEvent.click(within(form).getByText("保存"));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toMatchObject({
      name: "新番收件箱",
      inbound_root: "/data/inbound",
      library_root: "/data/library",
      media_type: "anime",
      stability_seconds: 300,
    });
    // Saving closes the form.
    await waitFor(() =>
      expect(screen.queryByDisplayValue("新番收件箱")).not.toBeInTheDocument(),
    );
  });

  it("closes the edit form on cancel without saving", async () => {
    const { patches } = mockSettingsApi();

    render(<SettingsPage />);

    fireEvent.click(await screen.findByText("编辑"));
    fireEvent.click(screen.getByText("取消"));

    expect(screen.queryByDisplayValue("动画收件箱")).not.toBeInTheDocument();
    expect(patches).toHaveLength(0);
  });

  it("configures replacement through the edit form", async () => {
    const { patches } = mockSettingsApi();

    render(<SettingsPage />);

    fireEvent.click(await screen.findByText("编辑"));
    const form = screen.getByDisplayValue("动画收件箱").closest("form")!;

    fireEvent.click(within(form).getByLabelText("洗版"));
    fireEvent.click(within(form).getByText("添加目录"));
    fireEvent.change(within(form).getByLabelText("目录 1"), {
      target: { value: "/data/anirss" },
    });
    fireEvent.click(within(form).getByText("保存"));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toMatchObject({
      replace_enabled: true,
      replace_extra_dirs: ["/data/anirss"],
      replace_auto_ratio: 1.2,
    });
  });

  it("keeps the add form collapsed behind a button", async () => {
    const { posts } = mockSettingsApi();

    render(<SettingsPage />);
    await screen.findByText("动画收件箱");

    // Collapsed by default: just the button, no form fields.
    expect(screen.queryByLabelText("名称")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "添加监控" }));
    expect(screen.getByLabelText("名称")).toBeInTheDocument();

    // Cancel collapses without creating anything.
    fireEvent.click(screen.getByText("取消"));
    expect(screen.queryByLabelText("名称")).not.toBeInTheDocument();
    expect(posts).toHaveLength(0);
  });

  it("creates a config through the add form", async () => {
    const { posts } = mockSettingsApi();

    render(<SettingsPage />);
    await screen.findByText("动画收件箱");

    fireEvent.click(screen.getByRole("button", { name: "添加监控" }));
    const form = screen.getByLabelText("名称").closest("form")!;
    fireEvent.change(within(form).getByLabelText("名称"), {
      target: { value: "电影收件箱" },
    });
    fireEvent.change(within(form).getByLabelText("监控目录"), {
      target: { value: "/data/movies-inbound" },
    });
    fireEvent.change(within(form).getByLabelText("媒体库目录"), {
      target: { value: "/data/movies" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "添加监控" }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      name: "电影收件箱",
      inbound_root: "/data/movies-inbound",
      library_root: "/data/movies",
      media_type: "anime",
    });
    // Creation collapses the form back to the button.
    await waitFor(() =>
      expect(screen.queryByLabelText("名称")).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "添加监控" })).toBeInTheDocument();
  });

  it("shows a summary card with a Chinese type tag and enabled state", async () => {
    mockSettingsApi();

    render(<SettingsPage />);

    const card = (await screen.findByText("动画收件箱")).closest("li")!;
    expect(within(card).getByText("动画")).toBeInTheDocument();
    expect(within(card).getByText("已启用")).toBeInTheDocument();
    // Enabled features show up as read-only tags; disabled ones stay hidden.
    expect(within(card).getByText("通知")).toBeInTheDocument();
    expect(within(card).queryByText("字幕")).not.toBeInTheDocument();
    expect(within(card).queryByText("洗版")).not.toBeInTheDocument();
    // The per-option controls live in the edit form, not on the card.
    expect(within(card).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("shows tags for subtitle acquisition and replacement when enabled", async () => {
    mockSettingsApi({
      ...CONFIG,
      acquire_subtitles: true,
      subtitle_variant: "cht",
      replace_enabled: true,
      notify: false,
    });

    render(<SettingsPage />);

    const card = (await screen.findByText("动画收件箱")).closest("li")!;
    expect(within(card).getByText("字幕")).toBeInTheDocument();
    expect(within(card).getByText("洗版")).toBeInTheDocument();
    expect(within(card).queryByText("通知")).not.toBeInTheDocument();
  });

  it("edits subtitle and notify options through the edit form", async () => {
    const { patches } = mockSettingsApi();

    render(<SettingsPage />);

    fireEvent.click(await screen.findByText("编辑"));
    const form = screen.getByDisplayValue("动画收件箱").closest("form")!;

    fireEvent.click(within(form).getByLabelText("自动找字幕"));
    fireEvent.change(within(form).getByLabelText("字幕偏好"), {
      target: { value: "cht" },
    });
    fireEvent.click(within(form).getByLabelText("通知"));
    fireEvent.click(within(form).getByLabelText("启用监控"));
    fireEvent.click(within(form).getByText("保存"));

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toMatchObject({
      acquire_subtitles: true,
      subtitle_variant: "cht",
      notify: false,
      enabled: false,
    });
  });

  it("hides the subtitle options for movie and tv types", async () => {
    mockSettingsApi();

    render(<SettingsPage />);

    fireEvent.click(await screen.findByText("编辑"));
    const form = screen.getByDisplayValue("动画收件箱").closest("form")!;

    expect(within(form).getByLabelText("自动找字幕")).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("媒体类型"), {
      target: { value: "movie" },
    });
    expect(within(form).queryByLabelText("自动找字幕")).not.toBeInTheDocument();
    expect(within(form).queryByLabelText("字幕偏好")).not.toBeInTheDocument();

    fireEvent.change(within(form).getByLabelText("媒体类型"), {
      target: { value: "anime" },
    });
    expect(within(form).getByLabelText("自动找字幕")).toBeInTheDocument();
  });

  it("always sends the reasoning effort, even the default", async () => {
    const { puts } = mockSettingsApi();

    render(<SettingsPage />);

    const select = await screen.findByLabelText("推理强度");
    const form = select.closest("form")!;

    // Untouched save: the default ("") still reaches the server, while
    // empty text fields stay omitted (they mean "keep unchanged").
    fireEvent.click(within(form).getByText("保存"));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({
      llm_reasoning_effort: "",
      trash_retention_days: "3",
    });

    fireEvent.change(select, { target: { value: "high" } });
    fireEvent.click(within(form).getByText("保存"));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1]).toEqual({
      llm_reasoning_effort: "high",
      trash_retention_days: "3",
    });
  });

  it("tests the model connection from the credentials form", async () => {
    const { llmTest, puts } = mockSettingsApi();

    render(<SettingsPage />);

    fireEvent.click(await screen.findByText("测试模型"));
    await waitFor(() => expect(llmTest.calls).toBe(1));
    expect(await screen.findByText("模型连通")).toBeInTheDocument();
    // The test button never submits the surrounding form.
    expect(puts).toHaveLength(0);
  });

  it("shows the error when the model test fails", async () => {
    const { llmTest } = mockSettingsApi();
    llmTest.response = { ok: false, error: "model_unreachable" };

    render(<SettingsPage />);

    fireEvent.click(await screen.findByText("测试模型"));
    expect(await screen.findByText("model_unreachable")).toBeInTheDocument();
  });

  it("edits the trash retention", async () => {
    const { puts } = mockSettingsApi();

    render(<SettingsPage />);

    const input = await screen.findByLabelText(/保留天数/);
    const form = input.closest("form")!;
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.click(within(form).getByText("保存"));

    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toMatchObject({ trash_retention_days: "0" });
  });
});
