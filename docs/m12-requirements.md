# M12 Requirement Matrix

状态：M12.0-M12.3 complete

| ID | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| M12-R01 | 通知类型是 `plan_ready/archive_completed/attention_required/test` 闭集 | Complete | `NotificationType`、unsupported type test boundary |
| M12-R02 | payload 使用 frozen、typed、fail-closed 合同 | Complete | notification dataclasses 与 invalid field tests |
| M12-R03 | 三种正式样式为固定 MarkdownV2 模板 | Complete | 三个 golden caption tests |
| M12-R04 | 动态文本统一转义全部 MarkdownV2 保留字符 | Complete | reserved-character 与 injection tests |
| M12-R05 | title 正规化、控制字符替换、字段与 caption 有界 | Complete | normalization/bounds tests；900-byte rendered limit |
| M12-R06 | poster 不接受任意 URL/path，只能构造固定 TMDB image URL | Complete | `TmdbPosterRef` negative matrix 与 photo URL golden test |
| M12-R07 | `test` 类型没有任意文本/模板输入 | Complete | field-free `TelegramTestNotification` golden test |
| M12-R08 | M12.0 不读取文件、不访问 DB/网络、不改变领域状态 | Complete | pure module；offline unit suite |
| M12-R09 | PostgreSQL outbox 具有 dedupe、lease、retry、dead 和 receipt | Complete | schema 26；codec/repository/state tests；并发 claim 与同事务 rollback tests |
| M12-R10 | worker 重启回收过期 lease，不在 HTTP 时持有 DB transaction | Complete | fenced restart recovery test；sender observes committed leased row |
| M12-R11 | adapter 只访问固定 Telegram HTTPS host，secret write-only | Complete | 2026-08-03 授权；fixed-origin/fake transport 与 config redaction tests |
| M12-R12 | poster 优先 `sendPhoto`，无 poster 降级 `sendMessage` | Complete | adapter photo/text tests；TMDB poster state projection tests |
| M12-R13 | durable facts 确定性地产生 outbox row | Complete | transactional projector、stable dedupe 与 folder deferral tests |
| M12-R14 | 通知失败不改变 run/approval/execution/folder 状态 | Complete | delivery worker 独立状态机；完整 reducer、生产旅程与 PostgreSQL 回归 |

## M12 test commands

```text
uv run --with pytest pytest -q tests/server/test_notifications.py
uv run --with pytest pytest -q tests/server/test_notification_outbox.py
uv run --with pytest pytest -q tests/server/test_notification_projector.py
uv run --with pytest pytest -q tests/adapters/test_telegram_http.py
REELOOM_TEST_POSTGRES_DSN=<isolated-dsn> uv run --with pytest \
  pytest -q tests/server/test_notification_outbox_postgres.py
```

测试必须保持离线。fake Telegram transport 不得解析真实 `.env` 或访问网络。
