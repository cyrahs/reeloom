# M9 计划：交互式 Web UI

状态：Approved for implementation

日期：2026-07-26

基线：M0-M8（`29d227a`）

## 1. 目标

M9 在 M8 PostgreSQL-first control plane 上建立同源 React Web UI。用户只通过
稳定 API/SSE 即可完成配置、观察、计划审查、Agent question/revision/reapply、
exact approval、apply 和 typed recovery。

UI 永远只是 API client：不解释路径、不编译 plan、不签发 approval、不访问
filesystem，也不根据浏览器本地状态推断 effect 已成功。movie domain contract
顺延到 M10。

## 2. 固定决策

- React 19、TypeScript、Vite 8、Node.js 24 LTS 和 npm lockfile。
- production build 作为 hash static assets 内置于同一 Reeloom Server。
- UI 使用 hash router；只有 `/` 和 manifest 中的 asset 可匿名读取。
- Admin Bearer 经 `/api/v1/session` 验证后保存在
  `localStorage["reeloom.admin_bearer.v1"]`；viewer/operator token 不进入 UI。
- 显式 question/revision/reapply 消息和最终回复可由 Admin 分页恢复；SDK
  transcript、prompt、tool observation 继续不可读。
- Cloudflare Access 可以作为外层保护，但不取代 Reeloom Bearer。

## 3. 交付

### M9.0 Browser contract

- M9 ADR、threat model、requirement matrix；
- strict named HTTP schema 与 OpenAPI security contract；
- 旧 M8 wire format 保持兼容。

### M9.1 Safe read models

- session bootstrap；
- enriched discovery/run projection 与 server-derived available actions；
- immutable plan lineage；
- hash-verified、分页的 initial/amendment preview；
- authenticated fetch SSE、durable cursor 和 heartbeat。

### M9.2 Config 与 interaction history

- config root/credential `retain | replace`；
- write-only path/secret round-trip；
- 新 interaction message 的 immutable persistence 和 Admin-only history。

### M9.3-M9.7 Web UI

- 登录、health、dashboard、run detail、event timeline、plan preview；
- 初始配置和安全编辑、provider probe；
- question、revision、reapply；
- exact approve/apply 与 recovery；
- stable idempotency key、stale head 和 uncertain outcome 的 fail-closed UX。

### M9.8 Production

- Node build stage 与 packaged manifest assets；
- CSP、no-sniff、no-referrer、frame/permission restrictions；
- Vitest、React Testing Library、Playwright 和完整 Python/PostgreSQL gates；
- deployment/API/README/review 文档。

## 4. 非目标

- movie mapping、movie naming 或 movie Agent；
- viewer/operator UI、Cookie session、OAuth 或 Cloudflare integration；
- 文件浏览器、shell、任意 URL/path tool、Markdown/HTML renderer；
- 多进程、多 worker、多主机或新的 filesystem effect；
- interaction 删除或独立 retention policy。

## 5. Definition of Done

1. Admin 可从 UI 完成配置到 apply/recovery 的完整旅程。
2. preview 只返回 exact plan 的 relative display text。
3. config GET 不返回 path/secret，retain 只绑定 exact revision。
4. interaction read model 只包含显式用户消息和 final reply。
5. token/API key/path/DSN/prompt/observation 不进入浏览器非必要表面。
6. 所有不可信文本只作为 text 渲染。
7. SSE 重连不丢 durable cursor，cursor drift 会显式 resync。
8. 网络不确定性不触发新的 effect 或 idempotency key。
9. M0-M8 canonical plan、approval、executor 和 recovery 保持兼容。
10. frontend、offline Python、PostgreSQL 17 和 browser E2E 全部通过。
