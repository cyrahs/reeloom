# ADR 0008：M14 基于当前状态的前向收敛执行

状态：Accepted

日期：2026-08-07

## 背景

M6-M13 的执行模型把 candidate snapshot 中的 `device/inode/mtime/ctime`、root
identity、一次性 approval claim、filesystem journal、run head 和 folder
observation 同时当作跨进程、跨重启的恢复事实。这个模型在本地文件系统上可以支持
“全有或全无 + rollback”，但不适合 CloudDrive2/FUSE：inode 和时间戳可能不稳定，
目录 `fsync` 与 `RENAME_NOREPLACE` 能力不一致，远端 rename 的返回值也不一定能证明
最终 namespace 状态。

因此 `destination_collision`、`atomic_move_unsupported`、`source_drift`、
`recovery_required` 和 `interaction_conflict` 并不是五类独立故障，而是历史状态与
当前文件状态冲突后的组合爆炸。

参考：[CloudDrive2 帮助](https://www.clouddrive2.com/help.html)、
[Linux rename(2)](https://man7.org/linux/man-pages/man2/renameat2.2.html)、
[Linux mount(8)](https://man7.org/linux/man-pages/man8/mount.8.html)。

## 决策

M14 用一个 forward-only execution operation 取代 media、folder disposition 和
subtitle publication 各自面向用户的恢复状态机。文件系统当前路径状态是执行与重试
的唯一事实；journal 仅保留审计用途，不再决定 rollback 或 exact recovery。

### 语义身份

- 视频持久身份为 run-scoped candidate ID、相对路径、`regular` 类型和
  `size_bytes`；不包含内容 hash。
- 外置字幕继续绑定完整 SHA-256。M13 的下载归档、归档卷和解压 member 完整
  SHA-256 约束保持不变。
- `device/inode/mtime/ctime` 不进入 v2 snapshot、plan hash、freshness、watcher
  generation 或恢复判断；它们只可在同一次 no-follow open/syscall 前后作为瞬时
  TOCTOU 防护。
- root 只绑定 config revision、watch ID 和 no-follow 绝对路径。执行时只要求路径
  存在且可读，不增加 mount marker、inode 或持久类型身份。
- folder inventory 后续只使用排序后的 `path + kind + size`，稳定等待继续使用配置的
  settle interval。

这明确接受两项取舍：同路径同大小的视频被替换可能无法识别；挂载正常卸载后，底层
挂载点重新可见时可能仍通过路径检查。

### 当前状态真值表

每个 move 独立观察 source 和 destination 的 no-follow 路径状态：

| Source | Destination | 决策 |
|---|---|---|
| 匹配类型/大小 | 不存在 | 尝试前向 move |
| 不存在 | 匹配类型/大小 | 已满足 |
| 匹配 | 匹配 | collision，保留两者 |
| 任意 | 存在但不匹配 | collision，不修改 |
| 缺失或大小变化 | 不存在 | stale，不修改 |
| symlink/特殊文件 | 任意 | unsafe，不修改 |

各项互不回滚；一项失败不阻止其他独立安全项继续。rename 的 syscall 返回只进入诊断，
固定退避后重新打开 source/destination，并按同一真值表决定结果。已到目标视为成功，
尚在源则仍可向前，其他状态终结为 `partial/stale/collision/unsafe/unavailable` 并投递
普通 fresh scan。

原生 no-replace 不可用时，后续 executor 在全局 source-effect mutex 内重新确认目标
不存在，再执行普通 rename 并重新观察结果。这样兼容 CloudDrive2/FUSE，但明确接受
外部程序恰好在最终检查与 rename 之间创建同名目标时可能被覆盖的极小竞态。`fsync`
尽力执行；不支持或失败只产生 warning，不进入 rollback/recovery。

### Operation、审批与控制面

`ExecutionOperation v2` 状态为：

```text
authorized -> running -> completed | partial | stale | collision |
                         unsafe | unavailable | superseded
```

审批仍精确绑定 `run_id + plan_hash + scope + nonce`，只在 operation 首次启动时消费；
同一 operation 的后台 reconcile 不要求新审批，也不受原审批后来过期影响。v2 由统一
operation ledger 与 lease 驱动；API/UI 不再传 approval ID，也不暴露定向 recovery。
自动模式只能来自服务端保存的配置。

所有终态都可结束、删除或请求重新扫描。`available_actions` 和命令准入必须调用同一个
领域函数；`interaction_conflict` 只用于真实并发 CAS 冲突。业务结果使用
`stale_source`、`destination_collision`、`unsafe_entry`、`root_unavailable` 和
`partial_execution`。

### 字幕、watcher 与 folder housekeeping

后续增量把字幕归档保存在媒体根外的内容寻址缓存，并直接写入 plan-owned
`reeloom-acquired-<publication-id>` 目录。member 以 `O_EXCL|O_NOFOLLOW` 写入；已存在
member 仅在完整 SHA-256 相同时复用；最后写 immutable complete marker。watcher 忽略
无有效 marker 的 partial 目录。此设计删除 staging-directory rename、staging
inode/uid/mode 检查和 subtitle-specific recovery。

watcher 每轮生成一个语义 snapshot，以跨 poll 稳定期判断上传完成；handled inventory
阻止未成功归档的相同来源反复创建 run。folder archive/fail 变成 operation 结束后的
best-effort housekeeping，失败只产生 warning，不改变媒体 operation 终态。

### v1 迁移

已完成的 v1 历史只读保留。未结算 approval/recovery、folder disposition 和 subtitle
transaction 在迁移时统一标为 `superseded_v1`，不执行、不回滚、不修改文件，并按当前
来源目录投递 fresh scan。v2 稳定后删除 v1 写路径与 legacy recovery 分支。

## M14.0 边界

本增量只加入 strict frozen semantic identity、canonical `RenamePlan v2`、纯
`ExecutionOperation v2` 生命周期、当前状态真值表与失败测试。它不替换现有 scanner、
watcher、plan compiler、approval、journal、executor、API 或 UI，不执行 rename，也不
迁移任何 v1 记录。后续 M14.1-M14.4 必须分别在离线测试通过后才开放对应行为。

## M14.1 边界

M14.1 只切换 watcher identity、PostgreSQL discovery 与 episode plan-only 规划。语义
discovery payload 和 `RenamePlan v2` 不持久化 stat identity；规划开始前按授权路径进行
一次 no-follow 当前状态重扫。manual/automatic、Movie 以及启用 ACG.RIP 的 M13 流程仍
生成 v1 计划，避免在 M14.2 forward executor 和 M14.3 marker publisher 完成前把 v2
计划交给旧副作用路径。plan-only 不再创建 folder disposition plan。

## M14.2a 边界

v2 使用单一 `execution_operations_v2` ledger 保存 authorization、lease、attempt 与终态；
approval 首次绑定 operation 后不再创建 v1 claim/settlement 镜像。v1 claim 与 v2 operation
通过同一 run advisory lock 互斥消费 approval，lease 过期后可由后台重领，终态不可修改。
本小步没有连接 executor、API 或 UI，也不会产生文件系统 effect。

## M14.2b 边界

统一 forward executor 只依据每个计划项的当前 source/destination 语义状态执行：已满足
即采用、可移动则前移、其余项以 stale/collision/unsafe/unavailable 终结，且独立项继续、
从不 rollback。原生 no-replace 不支持时，在单进程 effect mutex 内做目标复检后使用
checked rename；rename 返回值只是诊断，最终以有界重观察为准。目录 fsync 失败只记录
warning。该层返回 durable fresh-scan intent，但在 M14.2c 前尚不注册 operation coordinator、
HTTP API 或 successor run。

## M14.2c 边界

`ForwardExecutionCoordinator` 只从持久配置决定 manual/automatic，首次执行把 exact
approval 直接绑定到 operation；后台 reconcile 重领同一 lease，不读取浏览器
`automatic` 或 approval ID。operation 终态、逐项 outcome/diagnostic、warning 和
fresh-scan intent 在同一 PostgreSQL transaction 中写入；独立 outbox worker 用 scheduler
audit key 幂等退休旧 folder generation，进程在 dispatch 与 outbox ack 之间崩溃也不会
重复产生 effect。

Run API 对 v2 返回 operation 状态、计数、warning、rescan 状态与可发现的 successor run，
并隐藏 legacy recovery approval。新的 strict `/execute` 接受空对象和 `If-Match` plan
hash；Web 使用同一 operation 安全重读，不再生成新的恢复请求。M14.2c 仍不把
manual/automatic 与 ACG.RIP production plan 切换到 v2；该开关等待 M14.3 完成 subtitle
marker publisher 和 non-blocking folder housekeeping。

## 后果

- 牺牲跨文件全有或全无和自动 rollback，换取 forward progress、FUSE 兼容与可退出状态。
- 保留 never-follow-symlink、路径 containment、独立审批和默认 plan-only。
- checked rename 无法同时保证原地 move 和内核级 no-replace；个人自动化部署接受上述
  极小外部竞态，但 executor 仍不得主动覆盖已观察到的目标。
- ADR 0005 与 ADR 0007 描述的 v1 recovery/publish 行为只适用于迁移前 v1 写路径。
