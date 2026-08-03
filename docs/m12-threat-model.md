# M12 Telegram outbound notification threat model

状态：M12.0-M12.3 verified；fixed-purpose network gate approved

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

## M12.1 verified boundary

schema 26 只存 versioned closed payload 和有界 receipt/error enum。唯一 dedupe
消除本地重复生产；claim 使用 `FOR UPDATE SKIP LOCKED` 与短事务，sender 只在
claim 提交后运行；settlement 绑定 worker 与 attempt fencing。过期 lease 在启动时
回收到 retry/dead，瞬时错误使用有界指数退避与 jitter，`retry_after` 被原样尊重。
caller-owned transaction 接口保证 durable fact 与通知同时 commit/rollback。本阶段
没有 HTTP adapter、secret、任意 URL、文件读取或 Agent tool。

## Verified M12.2-M12.3 boundary

Telegram adapter 只接受经过校验的 token/chat ID，固定访问
`https://api.telegram.org`，禁用 redirect 与环境 proxy；响应、并发和超时有界。
Admin 配置与测试动作不回显 secret。确定性 projector 在 durable fact 同一事务中
入队；选定 TMDB poster path 随运行投影持久化，不能注入完整 URL。Worker 只改变
outbox 行，领域 reducer、审批、执行和文件夹状态不读取 delivery 结果。

## Residual risks

- Telegram 没有 caller-supplied idempotency key，网络成功但 receipt 未落库时可能
  重复发送；
- chat 管理员或 Telegram 平台本身仍能读取消息内容，因此内容必须保持最小化；
- poster availability and Telegram rate limits are external dependencies。
