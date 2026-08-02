# M12 Telegram outbound notification threat model

状态：M12.0 baseline

## Assets

- Telegram bot token、目标 chat ID 和发送 receipt；
- durable domain fact 与 notification dedupe identity；
- 管理员对 plan、执行结果和恢复状态的正确认知；
- Reeloom 的 Agent、审批、Executor、媒体文件和控制面可用性。

## Trust boundaries

1. 不可信 filename/TMDB text 进入确定性 notification projector；
2. durable PostgreSQL fact 进入 notification outbox；
3. leased outbox row 进入独立 Telegram worker；
4. worker 跨固定 HTTPS 边界调用 Telegram Bot API；
5. Telegram response 以最小 receipt/error code 返回 PostgreSQL。

Telegram 通知是只读旁路，不能反向驱动 run phase、批准、apply、recovery 或文件夹
处置。

## Threats and controls

| Threat | Control |
| --- | --- |
| Markdown link/mention injection | NFKC、控制字符替换、统一 MarkdownV2 escaping；模板闭集 |
| SSRF / 本地文件泄漏 | 不接受 URL/path；poster 只接受单段 TMDB `.jpg/.jpeg` ref 并拼接固定 host |
| filename/path 泄漏 | payload schema 没有 path、filename、raw exception 或 observation 字段 |
| secret 泄漏 | token write-only；不进 payload、event、trace、日志或异常；请求 URL 脱敏 |
| Agent 越权发送 | 通知不是 Agent tool；projector 只响应 durable fact |
| 伪造成功 | 发送状态不参与领域 reducer；只有原领域事件决定 Reeloom 状态 |
| 重复领域事件 | 稳定 unique `dedupe_key` + `ON CONFLICT DO NOTHING` |
| worker 崩溃丢单 | PostgreSQL durable row、短 lease、过期 lease 回收 |
| retry storm | 单 worker、有界 attempts、指数退避+jitter、`retry_after`、dead state |
| forged queue payload | schema version、closed enum、strict codec、extra-key rejection |
| DB/HTTP 长事务 | claim/settle 各自短事务；HTTP 期间不持有 DB transaction |
| 任意 Telegram endpoint | fixed HTTPS host、redirect disabled、bounded response |
| caption/resource exhaustion | 字段 byte limits、count bounds、caption 900-byte hard limit |
| 错误正文污染日志 | 只保留 bounded error enum/HTTP class，不持久化 response body |
| 用户误以为 exactly-once | 明示 at-least-once；receipt 落库前崩溃可能产生重复 |

## M12.0 verified boundary

当前实现只有纯内存 payload validation 和 rendering：不访问 PostgreSQL、不读取
secret、不读取文件、不发起 HTTP，也没有新增 Agent tool。测试覆盖固定 golden
caption、完整 MarkdownV2 保留字符、控制字符、超长字段、非法计数/哈希、绝对 URL、
嵌套路径、路径逃逸和非 TMDB poster shape。

## Residual risks for later milestones

- Telegram 没有 caller-supplied idempotency key，网络成功但 receipt 未落库时可能
  重复发送；
- chat 管理员或 Telegram 平台本身仍能读取消息内容，因此内容必须保持最小化；
- poster availability and Telegram rate limits are external dependencies；
- 真实适配器在当前安全不变量中尚未获准，M12.2 前必须单独更新并评审网络白名单。
