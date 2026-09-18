import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SubtitleRecheck, SubtitleRecheckReport } from "../src/api";
import { SubtitlesPage } from "../src/pages/Subtitles";

const NOW = Date.now() / 1000;

function item(overrides: Partial<SubtitleRecheck> = {}): SubtitleRecheck {
  return {
    run_id: "run-1",
    config_name: "anime",
    folder_name: "[VCB-Studio] Demon Lord 2099",
    title: "魔王2099",
    year: 2024,
    tmdb_id: 234538,
    status: "waiting",
    count: 3,
    last_at: NOW - 3600,
    next_at: NOW + 82_800,
    deadline: NOW + 27 * 86400,
    note: "未找到合适的字幕发布",
    created_at: new Date((NOW - 3 * 86400) * 1000).toISOString(),
    ...overrides,
  };
}

function mockReport(report: Partial<SubtitleRecheckReport>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const url = String(input);
      if (url.endsWith("/subtitle-rechecks")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ now: NOW, days: 30, items: [], ...report }),
        };
      }
      throw new Error(`unexpected ${url}`);
    }),
  );
}

beforeEach(() => {
  localStorage.setItem("reeloom.token", "token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SubtitlesPage", () => {
  it("lists active rechecks apart from the ones that gave up", async () => {
    mockReport({
      items: [
        item({ run_id: "searching", status: "searching", next_at: null }),
        item(),
        item({
          run_id: "old",
          title: "Old Show",
          status: "given_up",
          count: 30,
          next_at: null,
          deadline: NOW - 86400,
        }),
      ],
    });

    render(<SubtitlesPage />);

    expect(await screen.findByText("每日搜索中 · 2")).toBeInTheDocument();
    expect(screen.getByText("已停止 · 1")).toBeInTheDocument();
    expect(screen.getByText("搜索中")).toBeInTheDocument();
    expect(screen.getByText("等待下次")).toBeInTheDocument();
    expect(screen.getByText("已停止")).toBeInTheDocument();
    expect(screen.getAllByText("字幕：未找到合适的字幕发布")).toHaveLength(3);
    expect(screen.getByText(/已复查 30 次/)).toBeInTheDocument();
    const link = screen.getByText("Old Show (2024)").closest("a");
    expect(link).toHaveAttribute("href", "/runs/old");
  });

  it("shows an empty state and a switched-off state", async () => {
    mockReport({ items: [] });
    const { unmount } = render(<SubtitlesPage />);
    expect(await screen.findByText("没有在等字幕的项目。")).toBeInTheDocument();
    unmount();

    mockReport({ days: 0 });
    render(<SubtitlesPage />);
    expect(
      await screen.findByText("字幕复查已在设置中关闭。"),
    ).toBeInTheDocument();
  });
});
