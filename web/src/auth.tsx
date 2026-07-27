import {
  createContext,
  type FormEvent,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { ApiClient, ApiError, TOKEN_STORAGE_KEY } from "./api";
import { sessionSchema } from "./schemas";

type AuthValue = {
  api: ApiClient;
  logout: () => void;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthGate({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() =>
    window.localStorage.getItem(TOKEN_STORAGE_KEY),
  );
  const [validated, setValidated] = useState(false);
  const [error, setError] = useState("");

  const logout = () => {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    setToken(null);
    setValidated(false);
  };
  const api = useMemo(
    () => new ApiClient(token ?? "", logout),
    [token],
  );

  useEffect(() => {
    if (token === null) {
      return;
    }
    const controller = new AbortController();
    api
      .request("/api/v1/session", sessionSchema, {
        signal: controller.signal,
      })
      .then(() => {
        window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
        setError("");
        setValidated(true);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        logout();
        setError(
          reason instanceof ApiError && reason.status === 401
            ? "Token 无效或已过期。"
            : "无法验证 Token，请确认服务可用。",
        );
      });
    return () => controller.abort();
  }, [api, token]);

  if (token === null || !validated) {
    return (
      <Login
        checking={token !== null}
        error={error}
        onSubmit={(candidate) => {
          setError("");
          setToken(candidate);
        }}
      />
    );
  }
  return (
    <AuthContext.Provider value={{ api, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

function Login({
  checking,
  error,
  onSubmit,
}: {
  checking: boolean;
  error: string;
  onSubmit: (token: string) => void;
}) {
  const [candidate, setCandidate] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (candidate.trim()) onSubmit(candidate);
    setCandidate("");
  };
  return (
    <main className="login-shell">
      <section className="login-card" aria-labelledby="login-title">
        <div className="brand-mark" aria-hidden="true">
          R
        </div>
        <p className="eyebrow">REELOOM CONTROL PLANE</p>
        <h1 id="login-title">把剧集归位，过程始终可见。</h1>
        <p className="lede">
          输入管理员 Bearer token。验证通过后只保存在当前浏览器。
        </p>
        <form onSubmit={submit}>
          <label htmlFor="admin-token">Admin Bearer token</label>
          <input
            id="admin-token"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={candidate}
            onChange={(event) => setCandidate(event.target.value)}
            disabled={checking}
            required
          />
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          <button className="primary wide" disabled={checking}>
            {checking ? "正在验证…" : "进入控制台"}
          </button>
        </form>
        <p className="fine-print">
          Reeloom 不使用 Cookie，也不会把 Token 放进 URL 或日志。
        </p>
      </section>
    </main>
  );
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("AuthGate missing");
  return value;
}
