# ADR 0002：M9 浏览器边界

状态：Accepted

日期：2026-07-26

## 决策

M9 是 M8 之后的交互式 Web UI，movie domain contract 顺延到 M10。UI 与 API
同源部署，但 `/api/v1/*` 继续强制 M8 Bearer role。公开 surface 仅包含空壳
`index.html` 和 build manifest 中的 immutable assets。

浏览器只接受 Admin token。token 经 `GET /api/v1/session` 验证后存入
`localStorage`。这是显式接受的 XSS 残余风险，因此 production 不加载第三方
active content，不使用 HTML/Markdown renderer，所有外部文本只按 text 渲染。

Admin 可以读取显式 interaction request message 和 final assistant reply。这个
投影不从 SDK session 重建，不包含 prompt、中间模型项、tool call 或 observation。
旧记录没有显式 request message 时报告不可用。

Plan preview 只投影已验证 immutable plan 中的 candidate ID、kind、relative
source/destination 和 disposition。absolute root、source identity、digest、
canonical bytes、journal 和 rollback 永不进入 browser response。

## 后果

- UI 不能成为 authorization 或 filesystem safety boundary。
- non-Admin token 仍可供现有 API client 使用，但不能登录 UI。
- Cloudflare Access 只是可选的外层 defense，不影响 Reeloom authorization。
- movie contract 与 UI 可分别验收，不产生 episode/movie hybrid schema。
