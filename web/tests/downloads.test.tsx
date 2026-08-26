import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MagnetDownload } from "../src/api";
import { DownloadsPage } from "../src/pages/Downloads";

const MAGNET =
  "magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056";

function download(overrides: Partial<MagnetDownload> = {}): MagnetDownload {
  return {
    id: "dl-1",
    magnet: MAGNET,
    info_hash: "C9E15763F722F23E98A29DECDFAE341B98D53056",
    download_dir: "/115/downloads",
    state: "downloading",
    name: "Show S01",
    progress: 42.5,
    size_bytes: 2 * 1024 ** 3,
    error: null,
    final_path: null,
    submitted_at: new Date().toISOString(),
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() - 60_000).toISOString(),
    ...overrides,
  };
}

function mockDownloads(
  downloads: MagnetDownload[],
  dirs: string[] = ["/115/downloads"],
) {
  const posts: { url: string; body: unknown }[] = [];
  const deletes: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/downloads") && method === "GET") {
        return {
          ok: true,
          status: 200,
          json: async () => ({ now: Date.now() / 1000, downloads, dirs }),
        };
      }
      if (url.endsWith("/downloads") && method === "POST") {
        posts.push({
          url,
          body: JSON.parse(String(init?.body)) as unknown,
        });
        return { ok: true, status: 201, json: async () => download() };
      }
      if (method === "POST" && /\/downloads\/[^/]+\/(delete|retry)$/.test(url)) {
        posts.push({ url, body: null });
        return { ok: true, status: 200, json: async () => download() };
      }
      if (method === "DELETE") {
        deletes.push(url);
        return { ok: true, status: 200, json: async () => ({ deleted: true }) };
      }
      throw new Error(`unexpected ${method} ${url}`);
    }),
  );
  return { posts, deletes };
}

beforeEach(() => {
  localStorage.setItem("reeloom.token", "token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DownloadsPage", () => {
  it("renders rows with state, progress and directory", async () => {
    mockDownloads([
      download(),
      download({
        id: "dl-2",
        state: "stalled",
        name: "Stuck",
        progress: 10,
        error: "no progress for 1d at 10%",
      }),
    ]);
    render(<DownloadsPage />);

    expect(await screen.findByText("Show S01")).toBeInTheDocument();
    expect(screen.getByText("下载中")).toBeInTheDocument();
    expect(screen.getByText("42.5%")).toBeInTheDocument();
    expect(screen.getByText("停滞")).toBeInTheDocument();
    // The stalled row offers a retry, the healthy one does not.
    expect(screen.getAllByRole("button", { name: "重试" })).toHaveLength(1);
  });

  it("splits multi-line input into one request per magnet", async () => {
    const { posts } = mockDownloads([], ["/dl"]);
    render(<DownloadsPage />);
    await screen.findByText("添加磁力下载");

    fireEvent.change(screen.getByPlaceholderText("magnet:?xt=urn:btih:…"), {
      target: { value: `${MAGNET}\n\n${MAGNET.replace(/6$/, "7")}\n` },
    });
    // The directory defaulted to the most recent history entry.
    const dirInput = screen.getByPlaceholderText("/115/downloads");
    expect(dirInput).toHaveValue("/dl");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));

    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[0].body).toEqual({ magnet: MAGNET, directory: "/dl" });
    expect(await screen.findByText("已添加 2 个下载")).toBeInTheDocument();
  });

  it("gates the cloud-side delete behind a confirm", async () => {
    const { posts } = mockDownloads([download()]);
    const confirmSpy = vi
      .spyOn(window, "confirm")
      .mockImplementation(() => false);
    render(<DownloadsPage />);

    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    expect(posts).toHaveLength(0);

    confirmSpy.mockImplementation(() => true);
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toMatch(/\/downloads\/dl-1\/delete$/);
  });

  it("offers record removal only for concluded downloads", async () => {
    const { deletes } = mockDownloads([
      download({ id: "dl-3", state: "completed", progress: null }),
    ]);
    render(<DownloadsPage />);

    fireEvent.click(await screen.findByRole("button", { name: "移除记录" }));
    await waitFor(() => expect(deletes).toHaveLength(1));
    expect(deletes[0]).toMatch(/\/downloads\/dl-3$/);
    expect(screen.queryByRole("button", { name: "删除" })).toBeNull();
  });
});
