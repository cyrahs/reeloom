import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ReplaceDecision, RunDetail } from "../src/api";
import { RunDetailPage } from "../src/pages/RunDetail";

function detail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: "run-1",
    config_id: "config-1",
    folder_name: "[Group] Show",
    state: "done",
    title: "Show",
    year: 2024,
    tmdb_id: 123,
    file_count: 1,
    move_count: 1,
    result: {
      moved: 1,
      duplicates: [],
      missing: [],
      archived: 0,
      subtitles_moved: 0,
      subtitles_acquired: 0,
      subtitles_embedded: 0,
      subtitle_note: "",
      replaced: [],
      discarded: [],
    },
    error: null,
    attempts: 0,
    snapshot: [
      {
        candidate_id: "V1",
        relative_path: "ep01.mkv",
        kind: "video",
        size_bytes: 10,
        variant: null,
      },
    ],
    plan: {
      identity: { title: "Show", year: 2024, tmdb_id: 123 },
      moves: [
        {
          kind: "media",
          source_root: "inbound",
          source_path: "[Group] Show/ep01.mkv",
          dest_root: "library",
          dest_path: "Show (2024) {tmdb-123}/S01/Show S01E01.mkv",
          candidate_id: "V1",
        },
      ],
      unmapped: [],
      notes: "",
    },
    executed_moves: [],
    replace_decision: null,
    logs: [],
    interactions: [],
    ...overrides,
  };
}

function mockDetail(body: RunDetail) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => body,
    })),
  );
}

describe("run detail page", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows each chat message exactly once", async () => {
    mockDetail(
      detail({
        interactions: [
          { role: "user", content: "which season?", ts: "2026-01-01T00:00:00Z" },
          { role: "agent", content: "It is season 1.", ts: "2026-01-01T00:00:01Z" },
        ],
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findAllByText("It is season 1.")).toHaveLength(1);
  });

  it("marks revision messages as the user's", async () => {
    mockDetail(
      detail({
        interactions: [
          { role: "revision", content: "season 2 please", ts: "2026-01-01T00:00:00Z" },
        ],
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    const bubble = (await screen.findByText("season 2 please")).closest(".chat");
    expect(bubble).toHaveClass("user");
    expect(screen.getByText("修订")).toBeInTheDocument();
  });

  it("renders markdown in agent replies instead of raw markers", async () => {
    mockDetail(
      detail({
        interactions: [
          {
            role: "agent",
            content:
              "It is **season 1** of `Show`.\n- ep01 → S01E01\n- ep02 → S01E02",
            ts: "2026-01-01T00:00:00Z",
          },
        ],
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    const bold = await screen.findByText("season 1");
    expect(bold.tagName).toBe("STRONG");
    expect(screen.getByText("Show").tagName).toBe("CODE");
    const bubble = bold.closest(".chat")!;
    expect(bubble.querySelectorAll("li")).toHaveLength(2);
    expect(bubble.textContent).not.toContain("**");
  });

  it("keeps underscored file names literal in chat", async () => {
    mockDetail(
      detail({
        interactions: [
          {
            role: "agent",
            content: "Renamed show_name_v2.mkv to match.",
            ts: "2026-01-01T00:00:00Z",
          },
        ],
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    expect(
      await screen.findByText(/show_name_v2\.mkv/),
    ).toBeInTheDocument();
  });

  it("keeps the result line to counts", async () => {
    mockDetail(
      detail({
        result: {
          moved: 1,
          duplicates: ["dup02.mkv"],
          missing: [],
          archived: 2,
          subtitles_moved: 0,
          subtitles_acquired: 0,
          subtitles_embedded: 3,
          subtitle_note: "",
          replaced: ["old.mkv"],
          discarded: ["dup01.mkv"],
        },
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    expect(
      await screen.findByText(
        "移动 1 · 归档 2 · 字幕 0 · 下载字幕 0 · 内封 3 · 洗版 1 · 重复 2",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/dup01\.mkv/)).not.toBeInTheDocument();
  });

  it("groups trash moves by destination directory", async () => {
    mockDetail(
      detail({
        snapshot: [
          {
            candidate_id: "V1",
            relative_path: "ep01.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "V2",
            relative_path: "ep02.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
        ],
        plan: {
          identity: { title: "Show", year: 2024, tmdb_id: 123 },
          moves: [
            {
              kind: "trash_duplicate",
              source_root: "inbound",
              source_path: "[Group] Show/ep01.mkv",
              dest_root: "inbound",
              dest_path: ".reeloom-trash/run-1/inbound/[Group] Show/ep01.mkv",
              candidate_id: "V1",
            },
            {
              kind: "trash_duplicate",
              source_root: "inbound",
              source_path: "[Group] Show/ep02.mkv",
              dest_root: "inbound",
              dest_path: ".reeloom-trash/run-1/inbound/[Group] Show/ep02.mkv",
              candidate_id: "V2",
            },
            {
              kind: "trash_replaced",
              source_root: "library",
              source_path: "Show (2024) {tmdb-123}/S01/Show S01E03.mkv",
              dest_root: "inbound",
              dest_path:
                ".reeloom-trash/run-1/library/Show (2024) {tmdb-123}/S01/Show S01E03.mkv",
              candidate_id: null,
            },
          ],
          unmapped: [],
          notes: "",
        },
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    expect(
      await screen.findByText(/重复（2 个）移入/),
    ).toBeInTheDocument();
    expect(screen.getByText(".reeloom-trash/run-1/inbound/")).toBeInTheDocument();
    expect(screen.getByText(/洗版替换（1 个）移入/)).toBeInTheDocument();
    expect(screen.getByText(".reeloom-trash/run-1/library/")).toBeInTheDocument();
    // The per-file rows list only the sources, never the trash destinations.
    expect(screen.getByText("[Group] Show/ep01.mkv")).toBeInTheDocument();
    expect(
      screen.queryByText(".reeloom-trash/run-1/inbound/[Group] Show/ep01.mkv"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("trash_duplicate")).not.toBeInTheDocument();
  });

  it("collapses a fully archived subfolder to its name", async () => {
    mockDetail(
      detail({
        snapshot: [
          {
            candidate_id: "V1",
            relative_path: "ep01.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "V2",
            relative_path: "Extras/nc.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "O1",
            relative_path: "Scans/cover.jpg",
            kind: "other",
            size_bytes: 1,
            variant: null,
          },
          {
            candidate_id: "O2",
            relative_path: "Scans/back.jpg",
            kind: "other",
            size_bytes: 1,
            variant: null,
          },
          {
            candidate_id: "O3",
            relative_path: "readme.txt",
            kind: "other",
            size_bytes: 1,
            variant: null,
          },
        ],
        plan: {
          identity: { title: "Show", year: 2024, tmdb_id: 123 },
          moves: [],
          unmapped: ["O1", "O2", "O3"],
          notes: "",
        },
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    const line = await screen.findByText(/未映射（3 个）移入/);
    const block = within(line.closest(".trash-moves") as HTMLElement);
    expect(block.getByText("archive/")).toBeInTheDocument();
    expect(block.getByText("Scans/")).toBeInTheDocument();
    expect(block.getByText("readme.txt")).toBeInTheDocument();
    expect(block.queryByText(/cover\.jpg/)).not.toBeInTheDocument();
    // Extras holds a mapped file, so it is not reported as archived.
    expect(block.queryByText(/Extras/)).not.toBeInTheDocument();
  });

  it("collapses a fully unmapped run to the intake folder itself", async () => {
    mockDetail(
      detail({
        snapshot: [
          {
            candidate_id: "V1",
            relative_path: "nc01.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "O1",
            relative_path: "Scans/cover.jpg",
            kind: "other",
            size_bytes: 1,
            variant: null,
          },
        ],
        plan: {
          identity: { title: "Show", year: 2024, tmdb_id: 123 },
          moves: [],
          unmapped: ["V1", "O1"],
          notes: "",
        },
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    const line = await screen.findByText(/未映射（2 个）移入/);
    const block = within(line.closest(".trash-moves") as HTMLElement);
    expect(block.getByText("[Group] Show/")).toBeInTheDocument();
    expect(block.queryByText(/nc01\.mkv/)).not.toBeInTheDocument();
    expect(block.queryByText(/Scans/)).not.toBeInTheDocument();
  });

  it("collapses a nested fully unmapped subfolder", async () => {
    mockDetail(
      detail({
        snapshot: [
          {
            candidate_id: "V1",
            relative_path: "Discs/ep01.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "V2",
            relative_path: "Discs/SPs/nc01.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "V3",
            relative_path: "Discs/SPs/nc02.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
        ],
        plan: {
          identity: { title: "Show", year: 2024, tmdb_id: 123 },
          moves: [],
          unmapped: ["V2", "V3"],
          notes: "",
        },
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    const line = await screen.findByText(/未映射（2 个）移入/);
    const block = within(line.closest(".trash-moves") as HTMLElement);
    expect(block.getByText("Discs/SPs/")).toBeInTheDocument();
    expect(block.queryByText(/nc01\.mkv/)).not.toBeInTheDocument();
  });

  it("lists leftover files of a partially archived subfolder", async () => {
    mockDetail(
      detail({
        snapshot: [
          {
            candidate_id: "V1",
            relative_path: "Discs/ep01.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "O1",
            relative_path: "Discs/log.txt",
            kind: "other",
            size_bytes: 1,
            variant: null,
          },
        ],
        plan: {
          identity: { title: "Show", year: 2024, tmdb_id: 123 },
          moves: [],
          unmapped: ["O1"],
          notes: "",
        },
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    const line = await screen.findByText(/未映射（1 个）移入/);
    const block = within(line.closest(".trash-moves") as HTMLElement);
    expect(block.getByText("Discs/log.txt")).toBeInTheDocument();
    expect(block.queryByText("Discs/")).not.toBeInTheDocument();
  });

  it("shows acquired subtitle renames inside the plan section", async () => {
    mockDetail(
      detail({
        executed_moves: [
          {
            move: {
              kind: "media",
              source_root: "inbound",
              source_path: "[Group] Show/ep01.mkv",
              dest_root: "library",
              dest_path: "Show (2024) {tmdb-123}/S01/Show S01E01.mkv",
              candidate_id: "V1",
            },
            outcome: "moved",
          },
          {
            move: {
              kind: "acquired_subtitle",
              source_root: "inbound",
              source_path:
                "archive/[Group] Show/.acquired/[Sub组] Show - 01 [CHS].ass",
              dest_root: "library",
              dest_path: "Show (2024) {tmdb-123}/S01/Show S01E01.chs.ass",
              candidate_id: null,
            },
            outcome: "moved",
          },
          {
            move: {
              kind: "acquired_subtitle",
              source_root: "inbound",
              source_path: "archive/[Group] Show/.acquired/dup.ass",
              dest_root: "library",
              dest_path: "Show (2024) {tmdb-123}/S01/Show S01E02.chs.ass",
              candidate_id: null,
            },
            outcome: "duplicate",
          },
        ],
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    // The staging prefix is stripped down to the downloaded filename.
    const source = await screen.findByText("[Sub组] Show - 01 [CHS].ass");
    expect(source).toBeInTheDocument();
    expect(
      screen.getByText("Show (2024) {tmdb-123}/S01/Show S01E01.chs.ass"),
    ).toBeInTheDocument();
    // Acquired subtitles render like ordinary subtitle moves: no tag.
    expect(screen.queryByText("下载字幕")).not.toBeInTheDocument();
    // Executed media moves already show as plan moves; not repeated. A
    // non-moved acquired entry is not shown either.
    expect(screen.getAllByText(/Show S01E01\.mkv/)).toHaveLength(1);
    expect(screen.queryByText(/dup\.ass/)).not.toBeInTheDocument();
  });

  it("renders the snapshot as a tree with folders collapsed", async () => {
    mockDetail(
      detail({
        snapshot: [
          {
            candidate_id: "V1",
            relative_path: "ep01.mkv",
            kind: "video",
            size_bytes: 10,
            variant: null,
          },
          {
            candidate_id: "O1",
            relative_path: "Scans/cover.jpg",
            kind: "other",
            size_bytes: 1,
            variant: null,
          },
          {
            candidate_id: "O2",
            relative_path: "Scans/Booklet/p01.jpg",
            kind: "other",
            size_bytes: 1,
            variant: null,
          },
        ],
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    // Folders collapse to a summary row with aggregate count and size.
    expect(await screen.findByText("Scans/")).toBeVisible();
    expect(screen.getByText("2 个文件 · 2 B")).toBeInTheDocument();
    expect(screen.getByText("cover.jpg")).not.toBeVisible();
    expect(screen.getByText("p01.jpg")).not.toBeVisible();
    // Top-level files stay visible.
    expect(screen.getByText("ep01.mkv")).toBeVisible();
  });

  it("shows the replacement decision and resolves it", async () => {
    const decision: ReplaceDecision = {
      groups: [
        {
          season: 1,
          verdict: "manual",
          ratio: 1.08,
          quality: "unknown",
          overlap: [
            {
              span: { season: 1, episode_start: 1, episode_end: 1 },
              candidate_id: "V1",
              incoming_bytes: 110,
              existing: [
                {
                  root: "library",
                  extra_base: null,
                  relative_path:
                    "Show (2024) {tmdb-123}/S01/Show S01E01.mkv",
                  size_bytes: 100,
                  span: { season: 1, episode_start: 1, episode_end: 1 },
                },
              ],
              existing_bytes: 100,
            },
          ],
          new_episodes: [2],
          reason: "ambiguous_upgrade",
        },
      ],
      existing_subtitles: [],
      needs_confirmation: true,
      resolution: null,
    };
    const posts: string[] = [];
    const body = detail({
      state: "needs_attention",
      error: { code: "replace_confirmation" },
      replace_decision: decision,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "POST") {
          posts.push(url);
          return {
            ok: true,
            status: 200,
            json: async () => ({ state: "comparing" }),
          };
        }
        return { ok: true, status: 200, json: async () => body };
      }),
    );

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findByText("需人工确认")).toBeInTheDocument();
    expect(screen.getByText(/体积比 1.08/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("确认替换旧版"));
    await waitFor(() =>
      expect(posts).toContain("/api/runs/run-1/replace"),
    );
  });

  it("omits the quality tag for groups with nothing to compare", async () => {
    const decision: ReplaceDecision = {
      groups: [
        {
          season: 1,
          verdict: "import",
          ratio: null,
          quality: "unknown",
          overlap: [],
          new_episodes: [1, 2],
          reason: "no_overlap",
        },
        {
          season: 2,
          verdict: "manual",
          ratio: 1.08,
          quality: "better",
          overlap: [
            {
              span: { season: 2, episode_start: 1, episode_end: 1 },
              candidate_id: "V1",
              incoming_bytes: 110,
              existing: [
                {
                  root: "library",
                  extra_base: null,
                  relative_path:
                    "Show (2024) {tmdb-123}/S02/Show S02E01.mkv",
                  size_bytes: 100,
                  span: { season: 2, episode_start: 1, episode_end: 1 },
                },
              ],
              existing_bytes: 100,
            },
          ],
          new_episodes: [],
          reason: "ambiguous_upgrade",
        },
      ],
      existing_subtitles: [],
      needs_confirmation: false,
      resolution: null,
    };
    mockDetail(detail({ replace_decision: decision }));

    render(<RunDetailPage runId="run-1" />);

    const importTag = await screen.findByText("全新入库");
    expect(screen.queryByText("画质未知")).not.toBeInTheDocument();
    expect(screen.getByText("画质更好")).toBeInTheDocument();
    // Nothing to expand for a pure import, so it is a plain row.
    expect(importTag.closest("details")).toBeNull();
    expect(screen.getByText("需人工确认").closest("details")).not.toBeNull();
  });

  it("hides the resolve buttons once the run moved on", async () => {
    const decision: ReplaceDecision = {
      groups: [
        {
          season: 1,
          verdict: "replace",
          ratio: 1.6,
          quality: "unknown",
          overlap: [],
          new_episodes: [],
          reason: "clear_upgrade",
        },
      ],
      existing_subtitles: [],
      needs_confirmation: false,
      resolution: "replace",
    };
    mockDetail(detail({ replace_decision: decision }));

    render(<RunDetailPage runId="run-1" />);

    expect(await screen.findByText("洗版替换")).toBeInTheDocument();
    expect(screen.queryByText("确认替换旧版")).not.toBeInTheDocument();
  });

  it("puts the chat section between the result and the files", async () => {
    mockDetail(detail());

    render(<RunDetailPage runId="run-1" />);
    await screen.findByText("交流");

    const order = screen
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent ?? "");
    const files = order.findIndex((text) => text.startsWith("文件"));
    expect(order.indexOf("结果")).toBeLessThan(order.indexOf("交流"));
    expect(order.indexOf("交流")).toBeLessThan(files);
  });
});
