import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunDetail } from "../src/api";
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
      subtitles_acquired: 0,
      subtitle_note: "",
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

    const bubble = (await screen.findByText("season 2 please")).closest("p");
    expect(bubble).toHaveClass("user");
    expect(screen.getByText("修订")).toBeInTheDocument();
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

    const line = await screen.findByText(/未映射（将归档）/);
    expect(line).toHaveTextContent("Scans/、readme.txt");
    expect(line).not.toHaveTextContent("cover.jpg");
    // Extras holds a mapped file, so it is not reported as archived.
    expect(line).not.toHaveTextContent("Extras");
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

    const line = await screen.findByText(/未映射（将归档）/);
    expect(line).toHaveTextContent("Discs/log.txt");
    expect(line).not.toHaveTextContent("Discs/、");
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
    expect(screen.getByText("字幕")).toBeInTheDocument();
    // Executed media moves already show as plan moves; not repeated. A
    // non-moved acquired entry is not shown either.
    expect(screen.getAllByText(/Show S01E01\.mkv/)).toHaveLength(1);
    expect(screen.queryByText(/dup\.ass/)).not.toBeInTheDocument();
  });

  it("puts the chat section between the result and the files", async () => {
    mockDetail(detail());

    render(<RunDetailPage runId="run-1" />);
    await screen.findByText("交流");

    const order = screen
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);
    expect(order.indexOf("结果")).toBeLessThan(order.indexOf("交流"));
    expect(order.indexOf("交流")).toBeLessThan(order.indexOf("文件"));
  });
});
