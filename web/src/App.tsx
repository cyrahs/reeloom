import { useEffect, useState } from "react";

import {
  ApiError,
  UNAUTHORIZED_EVENT,
  api,
  clearToken,
  getToken,
  setToken,
} from "./api";
import { RunDetailPage } from "./pages/RunDetail";
import { RunsPage } from "./pages/Runs";
import { SettingsPage } from "./pages/Settings";
import { Link, useRoute } from "./router";

// Old bookmarks and previously shared links use "#/…" hash routes.
if (window.location.hash.startsWith("#/")) {
  window.history.replaceState(null, "", window.location.hash.slice(1));
}

function ReelMark({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <circle cx="16" cy="16" r="9.6" fill="var(--surface)" />
      <circle cx="16" cy="16" r="2.7" fill="var(--accent)" />
      <circle cx="16" cy="10" r="2.3" fill="var(--accent)" />
      <circle cx="16" cy="22" r="2.3" fill="var(--accent)" />
      <circle cx="10" cy="16" r="2.3" fill="var(--accent)" />
      <circle cx="22" cy="16" r="2.3" fill="var(--accent)" />
    </svg>
  );
}

function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setToken(value.trim());
    try {
      await api.listRuns();
      onSignedIn();
    } catch (thrown) {
      clearToken();
      // Only a 401 means the token is wrong; a dead backend or proxy must
      // not be reported as a credential problem.
      const rejected =
        thrown instanceof ApiError && thrown.status === 401;
      setError(rejected ? "令牌无效" : (thrown as Error).message);
    }
  }

  return (
    <div className="signin-wrap">
      <form className="signin" onSubmit={submit}>
        <span className="brand">
          <ReelMark size={26} />
          Reeloom
        </span>
        <input
          type="password"
          placeholder="Admin token"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          autoFocus
        />
        <button type="submit" className="primary">
          登录
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  );
}

export function App() {
  const [authorized, setAuthorized] = useState<boolean | null>(null);
  const route = useRoute();

  useEffect(() => {
    if (!getToken()) {
      setAuthorized(false);
      return;
    }
    api
      .listRuns()
      .then(() => setAuthorized(true))
      .catch(() => setAuthorized(false));
  }, []);

  // A token revoked mid-session (server restart with a new one) drops the
  // UI back to sign-in instead of leaving every page stuck on an error.
  useEffect(() => {
    const onUnauthorized = () => setAuthorized(false);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  if (authorized === null) return <div className="loading">载入中…</div>;
  if (!authorized) return <SignIn onSignedIn={() => setAuthorized(true)} />;

  const runMatch = /^\/runs\/(.+)$/.exec(route);

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand">
            <ReelMark />
            Reeloom
          </Link>
          <nav>
            <Link to="/" className={route === "/" ? "active" : ""}>
              任务
            </Link>
            <Link
              to="/settings"
              className={route === "/settings" ? "active" : ""}
            >
              设置
            </Link>
          </nav>
          <button
            className="link"
            onClick={() => {
              clearToken();
              setAuthorized(false);
            }}
          >
            退出
          </button>
        </div>
      </header>
      <main>
        {runMatch ? (
          <RunDetailPage runId={runMatch[1]} />
        ) : route === "/settings" ? (
          <SettingsPage />
        ) : (
          <RunsPage />
        )}
      </main>
    </div>
  );
}
