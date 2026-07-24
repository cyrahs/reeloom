# M6 Definition of Done

日期：2026-07-24

结论：M6 已完成。只有绑定 exact `run_id + plan_hash + apply scope + expiry +
nonce` 的一次性批准能启动无 LLM Executor；plan、approval、journal 或文件系统
状态存在任何歧义时均 fail closed。

## 交付检查

- [x] canonical `ApprovalRecord` 严格绑定 run、plan、scope、UTC expiry 和
  nonce；字段、编码或 identity 篡改被拒绝。
- [x] approval store 使用授权根、no-follow、有界读取和原子 no-replace claim；
  wrong binding/expiry 不消费，并发与重放只有一个成功。
- [x] content-addressed plan store 只接受有效 `RenamePlan`，Executor API 只
  接受持久化 `plan_hash + approval_id`，不接受自然语言、路径或新 moves。
- [x] plan 在 `ApprovalRequested` 前持久化；独立 resume service 只接受
  结构化 approval，并用 typed events 驱动 apply/rollback 终态。
- [x] final preflight 重新验证 plan schema/policy/hash、root identity、全部
  mapped/unmapped source identity、字幕 digest、symlink、目标 absent 和同一
  文件系统。
- [x] rollback manifest 在 claim、任何目录创建或 rename 前持久化；同一事务的
  apply/recover 由进程级 lease 互斥；journal event 和 terminal result
  append-only、canonical、no-follow、可幂等复核。
- [x] forward 与 rollback 都使用 `renameat2(RENAME_NOREPLACE)`；没有普通
  rename、copy/unlink、覆盖或跨文件系统降级。
- [x] 每次 rename 后核对 identity 并 fsync 两端目录；partial failure 自动
  逆序 rollback，未映射文件保持不变。
- [x] recovery 要求 exact claimed approval、plan 和 rollback manifest；能处理
  forward rename 后未记 event，以及 rollback rename 后未记 event 的重复崩溃。
- [x] source 被重新创建、目标 identity 不符、两端同时存在或 journal 不确定时
  返回 `recovery_required`，不覆盖也不删除任何文件。
- [x] 当前 rename 或 completed durability 不确定时不进入普通 rollback；冲突
  terminal markers 被拒绝，不产生伪 `rolled-back`。

## 离线验证

- approval：wrong hash、expiry、tamper、并发 claim、重启 replay、record
  symlink。
- plan/preflight：plan tamper、root/source/unmapped drift、source open race、
  destination appeared、parent symlink、cross-filesystem；advisory preflight
  不消费 approval。
- apply：journal-before-rename、正常多文件 apply、rename-time target race、
  partial failure rollback、unmapped untouched、apply/recover 互斥、current
  move 与 completed 写入不确定。
- recovery：forward rename/event 间崩溃、rollback rename/event 间再次崩溃、
  重复恢复，以及重建 source 时拒绝覆盖。
- journal：immutable/idempotent event、tampered header、header/lock symlink、
  transaction ID binding。
- resume：plan-before-request、真实 Executor 完成路径、崩溃后的 typed rollback。
- 完整离线测试：`320 passed`。

## M6 与 M7 的边界

M6 已接入进程内 typed resume control flow，但尚未接入真实用户界面、长期 run
checkpoint、生产 trace 或 eval。M7 可以持久化控制面状态和观测数据，但不得让
SDK/model 类型、自然语言或 trace 数据进入 Executor 权限与路径决策。
