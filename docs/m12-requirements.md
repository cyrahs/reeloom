# M12 Requirement Matrix

状态：M12.0 complete；M12.1-M12.3 planned

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
| M12-R09 | PostgreSQL outbox 具有 dedupe、lease、retry、dead 和 receipt | Planned M12.1 | migration/repository/concurrency tests required |
| M12-R10 | worker 重启回收过期 lease，不在 HTTP 时持有 DB transaction | Planned M12.1 | restart/scripted sender tests required |
| M12-R11 | adapter 只访问固定 Telegram HTTPS host，secret write-only | Blocked until M12.2 authorization | fake transport/security tests required |
| M12-R12 | poster 优先 `sendPhoto`，无 poster 降级 `sendMessage` | Planned M12.2 | adapter contract tests required |
| M12-R13 | durable facts 确定性地产生 outbox row | Planned M12.3 | production journey + dedupe tests required |
| M12-R14 | 通知失败不改变 run/approval/execution/folder 状态 | Planned M12.3 | reducer/state invariance tests required |

## M12.0 test command

```text
uv run --with pytest pytest -q tests/server/test_notifications.py
```

测试必须保持离线。后续 fake Telegram transport 不得解析真实 `.env` 或访问网络。
