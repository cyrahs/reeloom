# M8 implementation review

结论：M8 通过验收。PostgreSQL control plane、受限 HTTP/SSE、watcher/scheduler、
logical Agent/session interaction、exact approval、apply/recovery 和
completed-layout/reapply 已组成单实例 production application，M0-M7 的
filesystem safety boundary 保持不变。

## Findings 关闭情况

- Apply/recover 共用 durable `run_operations` gate；startup 只清理上一进程的
  effect reservation。未 claim 的有效 approval 可重用，过期 approval 可追加替代；
  数据库约束保证同一 `run_id + plan_hash` 最多一个 claim。
- 已 claim 未 settlement 的 approval 会在 run read model 中暴露为
  `recovery_approval_id`。普通 apply 不会签发第二个 approval；typed resolver
  依据 exact claim、plan 和 immutable journal 完成 recovery/settlement。
- apply 预留事务不包含 filesystem I/O；执行前只重验该 run 的 exact watch/archive
  roots。initial plan 绑定 watch→archive，amendment 绑定 archive→archive。
- terminal journal summary 只按 manifest 中的 bounded candidate IDs 读取 immutable
  move/rollback/failure markers；重复 recovery 保留原 applied、rolled-back 和
  failure facts。
- logical run 的 turns、tokens、tool calls、failures、limits 和 deadline 全部持久化；
  interaction reserve 使用剩余额度，finalize 在同一事务结算 session、event、
  projection、budget 和 lineage。
- `PostgresEventStore` 启动只读取 versioned full-state projection；`run_events`
  仅用于 immutable history/API，不参与恢复或普通查询。
- interaction 按 run 绑定的 content-addressed `AgentDefinitionRevision` 读取并验证
  instructions、tool manifest、schema 和 session；代码升级不会静默替换旧 run
  identity。
- API idempotency 对 config/apply/recover 使用 typed durable resolver。effect 已
  完成但 response/finalize 不确定时，不重放 effect；从 config revision、
  approval settlement 或 exact journal recovery 重建结果。
- provider transport 固定 allowlisted origin/DNS/TLS Host，禁用 redirect/proxy，
  并限制整响应字节数和整请求 deadline。API 的同步 PostgreSQL reads 全部移出
  async event loop；HTTP cancellation 不会取消 shield 内的 mutation worker，
  durable idempotency 状态由 worker 继续结算。

## Requirement 主证明

- 10,000 次 unchanged poll soak 保持 observation mutation 为 2、audit 为 1；
- 8 路 discovery registration 与 approval claim 均只有一个胜者；
- PostgreSQL interaction competition 同一 run 只有一个 active operation；
- initial/amendment 共用 journal-before-rename、no-replace、TOCTOU 和 rollback
  contract；amendment destination race 不覆盖原文件或竞争目标；
- production fake journey 覆盖
  `discover → Agent → revision → apply → reapply → recover`，另有 automatic
  exact-approval journey；
- application-role introspection、history immutability、lineage FK、terminal
  mutation trigger 和 operation-kind constraint 在真实 PostgreSQL 17 验证。

## 验收命令与结果

```text
.venv/bin/python -m pytest -q
471 passed, 19 skipped

REELOOM_TEST_POSTGRES_DSN=... .venv/bin/python scripts/run_postgres_tests.py
19 passed, 471 deselected
```

第二条使用干净 PostgreSQL 17 数据库，并预建 no-login `reeloom_app` role，因此
权限测试无 skip。两套测试均离线；pytest、server 和默认 smoke 不读取 `.env*`。
