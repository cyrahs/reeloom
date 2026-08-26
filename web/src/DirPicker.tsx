import { useEffect, useState } from "react";

import { ApiError, api, type DirListing } from "./api";

export function DirPicker({
  title,
  initial,
  onSelect,
  onClose,
  fetchDirs = api.listDirs,
}: {
  title: string;
  initial: string;
  onSelect: (path: string) => void;
  onClose: () => void;
  /** Directory source; defaults to the server filesystem. The downloads
   * page passes api.listCloudDirs to browse the CloudDrive tree instead. */
  fetchDirs?: (path: string) => Promise<DirListing>;
}) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [error, setError] = useState("");

  async function open(path: string, fallback = false) {
    setError("");
    try {
      setListing(await fetchDirs(path));
    } catch (thrown) {
      // The seed path may not exist yet; start from the root instead.
      if (fallback && thrown instanceof ApiError && thrown.status === 404) {
        open("/");
        return;
      }
      setError((thrown as Error).message);
    }
  }

  useEffect(() => {
    open(initial || "/", true);
    // Runs once: the picker browses freely after seeding from `initial`.
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal card"
        role="dialog"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <h3>{title}</h3>
        <code className="dir-current">{listing?.path ?? "…"}</code>
        {error && <p className="error">{error}</p>}
        <ul className="dir-list">
          {listing?.parent != null && (
            <li>
              <button
                type="button"
                className="dir-entry"
                onClick={() => open(listing.parent!)}
              >
                ← 上级目录
              </button>
            </li>
          )}
          {listing?.dirs.map((name) => (
            <li key={name}>
              <button
                type="button"
                className="dir-entry"
                onClick={() =>
                  open(
                    listing.path === "/"
                      ? `/${name}`
                      : `${listing.path}/${name}`,
                  )
                }
              >
                {name}
              </button>
            </li>
          ))}
          {listing && listing.dirs.length === 0 && (
            <li className="muted dir-none">没有子目录</li>
          )}
        </ul>
        <div className="form-actions">
          <button
            type="button"
            className="primary"
            disabled={!listing}
            onClick={() => listing && onSelect(listing.path)}
          >
            选择此目录
          </button>
          <button type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
