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

function mockSettingsApi() {
  const patches: Record<string, unknown>[] = [];
  const puts: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/settings") && method === "PUT") {
        puts.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return { ok: true, status: 200, json: async () => ({ updated: true }) };
      }
      if (url.endsWith("/settings")) {
        return { ok: true, status: 200, json: async () => SETTINGS };
      }
      if (url.endsWith("/configs") && method === "GET") {
        return { ok: true, status: 200, json: async () => ({ configs: [CONFIG] }) };
      }
      if (url.endsWith(`/configs/${CONFIG.id}`) && method === "PATCH") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        patches.push(body);
        return { ok: true, status: 200, json: async () => ({ ...CONFIG, ...body }) };
      }
      throw new Error(`unexpected ${method} ${url}`);
    }),
  );
  return { patches, puts };
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

    fireEvent.click(
      within(form).getByLabelText("洗版：发现已有旧版本时自动替换/去重"),
    );
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

  it("toggles replacement from the config row", async () => {
    const { patches } = mockSettingsApi();

    render(<SettingsPage />);

    fireEvent.click(await screen.findByLabelText("洗版"));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ replace_enabled: true });
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
