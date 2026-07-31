import { Component, type ErrorInfo, type ReactNode } from "react";

import { useAuth } from "./auth";
import { ConfigPage } from "./pages/ConfigPage";
import { DashboardPage } from "./pages/DashboardPage";
import { RunPage } from "./pages/RunPage";
import { HashLink, HashNavLink, useHashPath } from "./router";

export function App() {
  const { logout } = useAuth();
  const path = useHashPath();
  const runMatch = /^\/runs\/([^/]+)$/.exec(path);
  const runId = runMatch?.[1] ? safeDecode(runMatch[1]) : null;
  let page: ReactNode;
  if (path === "/") {
    page = <DashboardPage />;
  } else if (path === "/config") {
    page = <ConfigPage />;
  } else if (runId !== null) {
    page = <RunPage key={runId} runId={runId} />;
  } else {
    page = (
      <main className="page">
        <h1>页面不存在</h1>
        <HashLink to="/">返回总览</HashLink>
      </main>
    );
  }
  return (
    <div className="app-shell">
      <header className="topbar">
        <HashLink to="/" className="brand" aria-label="Reeloom 首页">
          <span className="brand-mark small" aria-hidden="true">R</span>
          <span>
            <strong>Reeloom</strong>
            <small>控制台</small>
          </span>
        </HashLink>
        <nav aria-label="主导航">
          <HashNavLink to="/" owns={["/runs/"]}>总览</HashNavLink>
          <HashNavLink to="/config">配置</HashNavLink>
        </nav>
        <button className="ghost" onClick={logout}>退出</button>
      </header>
      <ErrorBoundary>{page}</ErrorBoundary>
    </div>
  );
}

function safeDecode(value: string): string | null {
  try {
    const decoded = decodeURIComponent(value);
    return decoded && new TextEncoder().encode(decoded).byteLength <= 128
      ? decoded
      : null;
  } catch {
    return null;
  }
}

class ErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    void error;
    void info;
    // Error detail is intentionally not sent to telemetry or rendered.
  }

  render() {
    if (this.state.failed) {
      return (
        <main className="page">
          <section className="empty-state" role="alert">
            <p className="eyebrow">界面已安全停止</p>
            <h1>此页面无法继续显示</h1>
            <p>请刷新页面。任何未确认的执行结果都应从服务端重新读取。</p>
          </section>
        </main>
      );
    }
    return this.props.children;
  }
}
