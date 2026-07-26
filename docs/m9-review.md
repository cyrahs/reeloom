# M9 实现评审

状态：Complete

日期：2026-07-26

## 结论

M9 已在 M8 PostgreSQL 控制面上交付同源 React 管理界面。浏览器只接受经
`GET /api/v1/session` 验证的 Admin Bearer；静态公开面只包含 `/` 和 Vite
manifest 中的哈希 JS/CSS。API、Agent、审批与 Executor 继续是唯一授权和副作用
边界，UI 不生成路径、不编译计划、不签发审批，也不根据网络断开推断执行成功。

Movie domain contract 未进入本里程碑，保留给 M10。

## 交付证据

- `web/`：React 19、TypeScript、Vite 8、TanStack Query、Zod、Vitest、
  React Testing Library 和 Playwright；npm lockfile 固定完整依赖图。
- Admin token：固定 localStorage key，启动和登录均重新验证；401、非 Admin 与
  logout 会清理并卸载 SSE。
- read model：run/discovery 聚合字段、server-derived `available_actions`、
  immutable lineage、initial/amendment relative preview 和 Admin interaction
  history。
- config：旧 M8 wire format 与新的 `retain | replace` 严格格式并存；GET 不返回
  root、secret 或 capability。
- SSE：Authorization fetch stream、15 秒 heartbeat、durable event cursor、
  exponential reconnect、dedupe 和 `cursor_ahead` REST resync。
- effects：UI 始终提交 `automatic: false`，审批对话框绑定 exact hash 和服务端
  counts；网络不确定时只重读 durable owner；recovery 使用服务端 exact
  approval ID。
- static boundary：no-follow manifest loader、无目录列表、无 SPA fallback、
  CSP、frame-ancestors、nosniff、no-referrer 与受限 Permissions Policy。
- contract：`docs/openapi-v1.json` 是 canonical snapshot，
  `scripts/export_openapi.py --check` 检查漂移；前端对实际响应做 Zod
  运行时校验，不保留一份未使用且容易漂移的重复 TypeScript 声明。
- production：Docker 使用 Node 24 build stage 执行 `npm ci`，只将构建产物带入
  Python 3.13 runtime image。

## 验收

```text
.venv/bin/python -m pytest -q -m "not postgres"
484 passed, 1 skipped, 20 deselected

.venv/bin/python scripts/run_postgres_tests.py
20 passed

npm run lint
npm run typecheck
npm test
npm run build
REELOOM_TEST_POSTGRES_DSN=... npm run e2e
全部通过；11 个 unit tests，9 个 E2E tests 覆盖
Chromium、Firefox、WebKit。E2E web server 是实际 Reeloom API/static
boundary，并连接 PostgreSQL 17 的专用 schema。
```

唯一 skip 是当前 macOS 文件系统在测试创建阶段直接拒绝非 UTF-8 文件名；Linux
仍执行该 scanner case。write-once 与 no-replace rename 分别使用 Linux
`O_TMPFILE/linkat`、`renameat2(RENAME_NOREPLACE)` 或 Darwin 的安全等价原语，
两端都保持目标不可覆盖。

## 残余风险

localStorage Admin token 的 same-origin XSS 风险按 ADR 显式接受。M9 通过无第三方
active content、严格 CSP、React text-only rendering 和无 Markdown/HTML/autolink
降低概率，但无法在同源脚本已失陷后保护 token。生产仍必须使用 TLS、受控反向
代理和短周期凭据，并在共享设备上 logout。
