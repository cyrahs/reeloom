import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { IntakeFolder, RunSummary } from "../src/api";
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
      subtitles_moved: 0,
      subtitles_acquired: 0,
      subtitle_note: "",
    },
    error: null,
    attempts: 0,
    ...overrides,
  };
}

function intakeFolder(overrides: Partial<IntakeFolder> = {}): IntakeFolder {
  return {
    config_id: "config-1",
    config_name: "anime",
    folder_name: "[Group] Incoming",
    file_count: 2,
    total_bytes: 3 * 1024 ** 3,
    status: "settling",
    reason: null,
    remaining_seconds: 90,
    scanned_at: Date.now() / 1000,
    ...overrides,
  };
}

function mockRuns(runs: RunSummary[], folders: IntakeFolder[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => ({
      ok: true,
      status: 200,
      json: async () =>
        String(input).includes("/intake")
          ? { now: Date.now() / 1000, folders }
          : { runs },
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
          subtitles_moved: 2,
          subtitles_acquired: 3,
          subtitle_note: "",
        },
      }),
    ]);

    render(<RunsPage />);

    await waitFor(() =>
      expect(screen.getByText(/重复 1/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/缺失 1/)).toBeInTheDocument();
    expect(screen.getByText(/字幕 2/)).toBeInTheDocument();
    expect(screen.getByText(/下载字幕 3/)).toBeInTheDocument();
  });

  it("says so when there is nothing yet", async () => {
    mockRuns([]);

    render(<RunsPage />);

    expect(await screen.findByText("还没有任务。")).toBeInTheDocument();
  });

  it("shows a settling folder with its countdown", async () => {
    mockRuns([], [intakeFolder()]);

    render(<RunsPage />);

    expect(await screen.findByText("静置文件夹")).toBeInTheDocument();
    expect(screen.getByText("[Group] Incoming")).toBeInTheDocument();
    expect(screen.getByText("静置中")).toBeInTheDocument();
    expect(screen.getByText("01:30")).toBeInTheDocument();
    expect(screen.getByText(/3\.0 GB/)).toBeInTheDocument();
    expect(screen.queryByText("还没有任务。")).not.toBeInTheDocument();
  });

  it("explains folders the scanner skipped", async () => {
    mockRuns(
      [],
      [
        intakeFolder({
          folder_name: "Extras",
          status: "skipped",
          reason: "no_video",
          remaining_seconds: null,
        }),
      ],
    );

    render(<RunsPage />);

    expect(await screen.findByText("已跳过")).toBeInTheDocument();
    expect(screen.getByText("没有视频文件")).toBeInTheDocument();
  });

  it("hides the intake section when nothing is settling", async () => {
    mockRuns([run()]);

    render(<RunsPage />);

    await screen.findByText("[Group] Show");
    expect(screen.queryByText("静置文件夹")).not.toBeInTheDocument();
  });
});
