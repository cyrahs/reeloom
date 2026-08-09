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

## M14.3a 边界

字幕 publication manifest 以 canonical marker 绑定 exact plan hash、plan-owned 最终目录和
每个 member 的 basename、size 与完整 SHA-256。独立 forward publisher 直接在最终目录中
以 `O_EXCL | O_NOFOLLOW` 写 member，只有 exact hash 才复用既有文件，最后排他写 marker；
文件或目录 fsync 不支持仅产生 warning。没有有效 marker、marker 损坏、member 缺失或 hash
不符的 reserved publication directory 对 watcher 完全不可见。

为保证增量提交可运行，M13 v1 staging publisher 在迁移窗口也补写完全相同的 marker；它的
旧 journal/recovery 行为尚未在本步骤删除。内容寻址缓存、普通 scan request、删除字幕专用
successor/recovery 以及 production 切换分别留给 M14.3b-M14.3c。

## M14.3b 边界

归档在首次 planning 下载并通过 inspector 后进入媒体根外的 SHA-256 cache；cache entry 以
完整 size/hash 读取，既有内容不一致时永不覆盖。marker executor 重入时优先使用 plan 中的
volume hash 从 cache 重建，cache entry 确实不存在时才重新解析同源附件、下载并精确核对
plan hash。持久的 archive `dev/inode/mtime/ctime` 不参与复用判断。

ACG.RIP production executor 已切换为 journal-free current-state publication。完成后写新的
semantic settlement 和 durable `subtitle_scan_requests_v2`，由 watcher/scheduler 的普通
稳定窗口生成 fresh discovery/run；一次性 acquisition lineage 在普通 run registration 时
传播，防止再次自动获取。marker publication 与 DB settlement 之间即使 watcher 抢先观察，
scan dispatch 也会对旧 observation、settling observation 或较新的 discovery/run 分别收敛。

新任务不再写 subtitle filesystem journal、staging rename 或 subtitle-specific successor
outbox；旧表与旧 worker 只为 M14.4 一次性迁移前已经存在的记录保留。由于 unified v2 ledger
当前仍由 media `plan_lineage + apply` 外键约束，字幕独立 plan family 的 ledger 合并留在
M14.4 schema migration，不能用伪造 media lineage 提前接入。

## M14.3c 边界

episode watch 不再按 apply policy 或 ACG.RIP 开关区分 identity：plan-only、manual 与
automatic 均生成 `RenamePlan v2`，Movie 仍留在 v1。media operation 结算时原子记录
handled semantic inventory；成功仅投递 archive housekeeping，partial/stale/collision
不归档并投递普通 fresh-scan intent。Watcher 对相同 handled inventory 保持静默，只有
当前 `path + kind + size` inventory 改变后才建立新 generation，避免来源残留循环。

archive/fail housekeeping 只有 `queued/leased/retry_wait/completed/warning`，没有 plan、
approval、filesystem journal 或 recovery。它使用确定性的 run-owned 目标名、no-follow
目录打开、全局 effect mutex、native no-replace 或 checked rename，并在远程文件系统可能
返回错误后按当前 source/destination 路径重新判断。collision、权限、fsync 与收尾失败只
形成 warning，不改变已终结的 media operation。Agent 终态失败使用相同的 fail queue。

## M14.4 边界

Movie 与 episode 共用 semantic snapshot、plan-only compiler、operation ledger 和 forward
executor，但保留独立的 canonical `MovieRenamePlan v2` family；不把 episode mapping
或命名规则渗入电影计划。所有新 watch generation 均为 v2，生产 API 与 background
不再允许 v1 media/folder effect。

一次性迁移把所有仍活跃的 v1 generation，以及存在未结算 media claim、folder claim
或已批准/阻塞 subtitle transaction 的 run 记入 immutable supersession history。迁移只
终结 control-plane job/run、释放 `run_operations` 协调锁，并把来源 observation 放回
普通跨 poll 稳定扫描；不会执行、回滚、删除或改名任何文件，也不会伪造 settlement。
completed/rolled-back v1 历史保持原终态。迁移后的 superseded run 和已记录 handled
inventory 的 v2 terminal run 不再被旧 claim/observation 阻止删除。

终态 `partial/stale/collision/unsafe/unavailable` 的显式“重新扫描”与 read model 使用同一
`forward_available_actions` 函数。命令只重投既有 operation 的 durable rescan outbox，
不复活 operation、不创建审批，也不接受 approval ID。live filesystem conformance smoke
必须显式指定空的绝对 throwaway 目录；普通 pytest/CI 仍不访问真实挂载。v1 executor
实现仅为部署稳定观察和历史读取暂留，生产开关固定关闭；观察窗口结束后再物理删除，
避免把代码删除与不可逆 schema/data 迁移放进同一次上线。

## M14.5 字幕垂直链收口

M14.4 的首次实现只升级了 semantic watcher/media 主链，生产字幕 consumer 仍要求
M13 的 folder device/inode 并编译 `SubtitleAcquisitionPlan v1`。这使 Agent 正确执行
`select_subtitle_release` 后在 deterministic planner 边界失败；内存 scheduler 和手工构造
v1 request 的测试又提供了生产中不存在的 stat 字段，因此没有发现断口。

M14.5 将 active 字幕链纳入同一个 v2 contract：planner 只接受 semantic root/watch/folder/
inventory/snapshot，输出 `SubtitleAcquisitionPlan v2`；`SUBTITLE_ACQUIRE` 保留独立 plan
family 和 approval scope，但 authorization、lease、terminal result 与普通 rescan 使用统一
operation ledger。marker publisher 以当前 path/hash 状态幂等收敛，不创建 subtitle recovery
claim、filesystem journal 或专用 successor。

普通 `execution_rescan_outbox_v2` 在 successor 注册时传播 acquisition lineage，并以
`successor_run_id` 唯一结算；active scheduler 不再读取或更新 legacy
`subtitle_scan_requests_v2`。因此新 snapshot 会进入普通 run，但不会再次暴露字幕获取工具。

worker 与 background 之间使用 closed typed failure envelope。只有明确的 current-state 变化
可以请求 fresh scan；provider、planner、Agent budget 与未知内部错误均保留来源并终结当前
run，不能推导 folder generation retry 或 fail housekeeping。lease 重领有固定上限，耗尽后
原子写入 unavailable result、handled inventory 和 rescan intent，保证不存在
`operation terminal + request approved` 的裂分状态。

测试边界也改为 producer-to-consumer 纵向验收：至少一条 mandatory PostgreSQL 测试必须从
真实 semantic watcher 和 Agents SDK scripted model 开始，经过字幕工具选择、v2 planning、
approval、共享 operation、marker 发布与 read model/API schema；禁止用手工 v1 RootBinding
替代这条测试。InMemory/PostgreSQL semantic discovery 必须遵守相同契约。

## 后果

- 牺牲跨文件全有或全无和自动 rollback，换取 forward progress、FUSE 兼容与可退出状态。
- 保留 never-follow-symlink、路径 containment、独立审批和默认 plan-only。
- checked rename 无法同时保证原地 move 和内核级 no-replace；个人自动化部署接受上述
  极小外部竞态，但 executor 仍不得主动覆盖已观察到的目标。
- ADR 0005 与 ADR 0007 描述的 v1 recovery/publish 行为只适用于迁移前 v1 写路径。
