import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { DirListing } from "../src/api";
import { DirPicker } from "../src/DirPicker";

const TREE: Record<string, DirListing> = {
  "/": { path: "/", parent: null, dirs: ["data"] },
  "/data": { path: "/data", parent: "/", dirs: ["anime", "movies"] },
  "/data/anime": { path: "/data/anime", parent: "/data", dirs: [] },
};

function mockTree() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = decodeURIComponent(
        new URL(url, "http://localhost").searchParams.get("path") ?? "",
      );
      const listing = TREE[path];
      return {
        ok: listing !== undefined,
        status: listing ? 200 : 404,
        json: async () => listing ?? { detail: "not_a_directory" },
      };
    }),
  );
}

describe("directory picker", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("navigates into a subdirectory and selects it", async () => {
    mockTree();
    const onSelect = vi.fn();

    render(
      <DirPicker
        title="选择监控目录"
        initial="/data"
        onSelect={onSelect}
        onClose={() => {}}
      />,
    );

    fireEvent.click(await screen.findByText("anime"));
    expect(await screen.findByText("/data/anime")).toBeInTheDocument();
    expect(screen.getByText("没有子目录")).toBeInTheDocument();

    fireEvent.click(screen.getByText("选择此目录"));
    expect(onSelect).toHaveBeenCalledWith("/data/anime");
  });

  it("walks back up through the parent entry", async () => {
    mockTree();

    render(
      <DirPicker
        title="选择监控目录"
        initial="/data"
        onSelect={() => {}}
        onClose={() => {}}
      />,
    );

    fireEvent.click(await screen.findByText("← 上级目录"));
    expect(await screen.findByText("data")).toBeInTheDocument();
  });

  it("falls back to the root when the seed path is gone", async () => {
    mockTree();

    render(
      <DirPicker
        title="选择监控目录"
        initial="/vanished"
        onSelect={() => {}}
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText("data")).toBeInTheDocument();
  });
});
