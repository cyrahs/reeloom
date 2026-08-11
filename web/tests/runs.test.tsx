import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunSummary } from "../src/api";
import { RunsPage } from "../src/pages/Runs";

function run(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "run-1",
    config_id: "config-1",
    folder_name: "[Group] Show",
    state: "done",
    title: "Show",
    year: 2024,
    tmdb_id: 123,
    file_count: 3,
    move_count: 2,
    result: {
      moved: 2,
      duplicates: [],
      missing: [],
      archived: 1,
      subtitles_acquired: 0,
      subtitle_note: "",
    },
    error: null,
    attempts: 0,
    ...overrides,
  };
}

function mockRuns(runs: RunSummary[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ runs }),
    })),
  );
}

describe("runs page", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows a finished run with its outcome", async () => {
    mockRuns([run()]);

    render(<RunsPage />);

    expect(await screen.findByText("[Group] Show")).toBeInTheDocument();
    expect(screen.getByText("Show (2024)")).toBeInTheDocument();
    expect(screen.getByText(/移动 2/)).toBeInTheDocument();
    expect(screen.getByText(/归档 1/)).toBeInTheDocument();
  });

  it("counts the runs that need a human", async () => {
    mockRuns([
      run({ id: "a", state: "needs_attention", result: null }),
      run({ id: "b", state: "failed", result: null }),
      run({ id: "c" }),
    ]);

    render(<RunsPage />);

    expect(await screen.findByText("2 个任务需要处理")).toBeInTheDocument();
  });

  it("reports duplicates and missing files in the summary", async () => {
    mockRuns([
      run({
        result: {
          moved: 1,
          duplicates: ["ep01.mkv"],
          missing: ["ep02.mkv"],
          archived: 0,
          subtitles_acquired: 0,
          subtitle_note: "",
        },
      }),
    ]);

    render(<RunsPage />);

    await waitFor(() =>
      expect(screen.getByText(/重复 1/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/缺失 1/)).toBeInTheDocument();
  });

  it("says so when there is nothing yet", async () => {
    mockRuns([]);

    render(<RunsPage />);

    expect(await screen.findByText("还没有任务。")).toBeInTheDocument();
  });
});
