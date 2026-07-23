# M5 Definition of Done

日期：2026-07-23

结论：M5 已完成。经过校验的 episode/subtitle mapping 会由确定性代码编译为
canonical、不可变且带 `plan_hash` 的 `RenamePlan`，run 随后停在
`AWAITING_APPROVAL`。本阶段没有审批记录、Executor 或文件写入能力。

## 交付检查

- [x] destination 仅由 `SeriesIdentity`、episode span、受控扩展名和字幕变体
  编译；Agent schema 不接受路径。
- [x] 标题 sanitization、扩展名白名单、exact/casefold plan collision 与同
  variant 字幕稳定消歧继续由 M0 kernel 强制执行。
- [x] `RenamePlan` 绑定 schema/policy version、run、trusted work type、注入
  UTC 时间、source/output root path 与 identity、candidate snapshot ID、全部
  source relative path/stat identity、mapping、字幕变体、moves 和未映射清单。
- [x] 字幕 source 同时绑定扫描时 64 KiB sample digest；任一 source 缺失完整
  identity 时 fail closed。
- [x] canonical JSON 使用显式 payload、稳定排序和固定编码；`plan_hash` 为
  `sha256:<hex>`，并可独立复核。
- [x] 文件系统 compiler 使用固定 output root 和 code-compiled relative
  destinations，只读拒绝已有目标、symlink parent 和 output root identity
  漂移；source identity 漂移由 M6 final preflight 拒绝。
- [x] preview 提供 move 与未映射清单，不含任何执行动作。
- [x] `PlanBuilt` 校验 exact run/snapshot/series/mapping/source set/hash；
  `ApprovalRequested` 必须引用 exact `plan_hash`。
- [x] 成功 run 以 `AWAITING_APPROVAL` reason 停止；没有 Agent 工具可以批准或
  apply。

## 离线验证

- kernel：canonical bytes/hash、确定性、source identity 变化、bytes 篡改、
  exact preflight set、root containment、未映射 preview。
- filesystem adapter：dry-run 零写入、existing destination collision、
  symlink parent、output root drift 和 fd 异常清理。
- runtime：`PlanBuilt → ApprovalRequested → AWAITING_APPROVAL` replay、wrong
  hash、tampered plan、普通文本终止拒绝和 compiler wall-clock budget。
- integration：真实 Agents SDK Runner + scripted model 完成调查、纠错、
  mapping 后无需额外模型轮次即可自动编译，并确认输出目录保持为空。
- 完整离线测试：`272 passed`。

## M5 与 M6 的边界

M5 的 destination preflight 是 plan 创建时的只读事实，不能消除检查后的
TOCTOU。M6 仍需实现结构化审批、expiry/nonce/一次性 claim、持久化 plan
读取、最终 no-follow preflight、journal、rename、rollback 和幂等恢复。在
这些能力完成前，`AWAITING_APPROVAL` 只是安全暂停点。
