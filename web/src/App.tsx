import { useEffect, useState } from "react";

import { api, clearToken, getToken, setToken } from "./api";
import { RunDetailPage } from "./pages/RunDetail";
import { RunsPage } from "./pages/Runs";
import { SettingsPage } from "./pages/Settings";

function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash.slice(1) || "/");
  useEffect(() => {
    const update = () => setHash(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return hash;
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
    } catch {
      clearToken();
      setError("令牌无效");
    }
  }

  return (
    <form className="signin" onSubmit={submit}>
      <h1>Reeloom</h1>
      <input
        type="password"
        placeholder="Admin token"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        autoFocus
      />
      <button type="submit">登录</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

export function App() {
  const [authorized, setAuthorized] = useState<boolean | null>(null);
  const route = useHashRoute();

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

  if (authorized === null) return <div className="loading">载入中…</div>;
  if (!authorized) return <SignIn onSignedIn={() => setAuthorized(true)} />;

  const runMatch = /^\/runs\/(.+)$/.exec(route);

  return (
    <div className="app">
      <nav>
        <a href="#/" className={route === "/" ? "active" : ""}>
          任务
        </a>
        <a href="#/settings" className={route === "/settings" ? "active" : ""}>
          设置
        </a>
        <button
          className="link"
          onClick={() => {
            clearToken();
            setAuthorized(false);
          }}
        >
          退出
        </button>
      </nav>
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
