# Reeloom 初步实施计划

状态：Draft v0.9

日期：2026-08-07

当前进度：M0-M13、M14.0-M14.2b 已完成；M14.2c-M14.4 尚未开始。
M0 建立纯领域契约；M1 建立 typed runtime events、预算和真实 Agents SDK tool loop；M2
建立安全 scanner、immutable
candidate snapshot 和 path capability table；M3 建立 provider-neutral TMDB
port、固定目的地 HTTP adapter、候选 ID capability、受 phase 限制的识别工具，
由 trusted run `work_type` 限制的 anime/tv/movie 搜索，以及由
`SeriesSelected` 驱动的 `MAP_EPISODES` 转换；M4 建立 inventory、字幕变体检测、
strict mapping submission、结构化纠错和 token/time budget；M5 建立只读的
确定性 Plan Compiler、完整 source/root binding、canonical `RenamePlan`、
`plan_hash` 和 `AWAITING_APPROVAL` 暂停边界；M6.1 建立 canonical
`ApprovalRecord`、expiry/binding 校验与持久化原子 one-time claim；M6.2 建立
content-addressed plan store 和无 LLM、no-follow 的 Executor final
preflight；M6.3 建立 append-only journal、no-replace rename、rollback、
result、transaction lease、typed approval resume 和幂等崩溃恢复。M7 已完成：
严格版本化的 runtime event codec、no-follow append-only checkpoint 与 SDK
session、进程重启 replay、scripted transcript、固定 eval dataset、脱敏 trace、
任务指标、显式 OpenAI Responses provider 配置和 opt-in live eval 均已建立。
M8 从这条 M7 基线实现服务器控制面：PostgreSQL 从第一步起就是唯一
control-plane metadata owner，文件系统只保留 Secret、Plan、Journal 和媒体。
M8.0-M8.8 的详细分步见 [M8 计划](m8-plan.md)，完成证明见
[M8 Requirement Matrix](m8-requirements.md) 和
[M8 实现评审](m8-review.md)。M9 在稳定 API/SSE 上建立同源 Web UI；M10 以
独立 Movie identity、mapping 和 plan family 完成电影闭环，不复用 Episode
mapping。

## 1. 项目目标

从零构建一个 agent-native 动画剧集整理器。用户提供一个动画发布目录和
输出根目录后，系统应能够：

1. 安全扫描候选视频和外置字幕。
2. 自主查询 TMDB 并识别正确剧集。
3. 调查常规季、Specials、OVA/OAD 和现有输出库存。
4. 将视频和字幕映射为确定的 `SxxExx`。
5. 生成可审查、不可变、可复现的重命名计划。
6. 在用户批准精确计划后，由隔离 Executor 执行。
7. 生成执行结果、事务 journal 和 rollback plan。

“同功能”指用户可观察结果与 aninamer 一致，不要求复用其应用层结构。
Reeloom 不导入 aninamer 的 runtime；旧项目只作为规则来源、fixture 来源和
golden oracle。

## 2. Agent-native 的定义

Reeloom 的主控制流必须由 Agent runtime 表达：

```text
observe state
→ choose an allowed tool or domain action
→ execute behind policy boundary
→ append typed event
→ reduce event into new state
→ continue, pause, or stop
```

Agent 可以自主决定查询顺序和需要补充的证据，但不能改变安全规则。项目分成
三个相互隔离的平面：

```mermaid
flowchart TD
    U["User / Web UI / API"] --> S["Application Service"]
    S --> R["Agent Runner"]
    R --> A["Episode Organizer Agent"]
    A --> T["Typed, capability-scoped tools"]
    T --> K["Deterministic Safety Kernel"]
    K --> P["Immutable RenamePlan + plan_hash"]
    P --> H{"Independent approval"}
    H -- "reject" --> X["Stop or re-plan"]
    H -- "approve exact hash" --> E["Isolated Executor"]
    E --> O["Result + journal + rollback"]
```

### 2.1 Control Plane：Agent Runtime

Agents SDK 负责模型/tool loop、暂停、恢复和基础 trace；项目代码负责业务
phase、capability、预算、checkpoint 和 typed domain events。项目不重复实现
一套平行的模型 orchestration runtime。

### 2.2 Safety Kernel：确定性领域内核

负责候选快照、严格 mapping schema、ID 类型、TMDB 集数边界、已有库存、
命名、路径 containment、碰撞和 RenamePlan 编译。该层不依赖模型或 Agent
SDK。

### 2.3 Effect Plane：隔离 Executor

负责审批验证、最终 preflight、journal、移动和 rollback。该层不依赖 LLM、
无业务网络权限，也不解释自然语言。

## 3. 已确定的架构决策

| 决策 | 选择 | 原因 |
| --- | --- | --- |
| 仓库关系 | 独立 greenfield repo | 避免继承旧 pipeline 和服务层耦合 |
| Runtime | Python OpenAI Agents SDK | 让 run、tools、state、trace 和 approval 成为一等概念 |
| Agent 数量 | MVP 单 Agent | 当前任务有一个清晰目标，多 Agent 暂无收益 |
| 领域逻辑 | provider-neutral Safety Kernel | 可离线测试，也可替换模型供应商 |
| 文件访问 | run-scoped capability + opaque ID | 不向模型暴露任意文件系统能力 |
| 归档分类 | trusted watch → source root + library root | 目标与监听直接绑定；work_type 只选择领域流程，Agent 不能借类型选择路径 |
| mapping | Agent 提交语义映射，代码校验 | LLM 做判断，代码做 enforcement |
| 路径 | 只由 plan compiler 构造 | 永不接受模型生成路径 |
| apply | 独立审批和 Executor | 将副作用与 Agent 推理隔离 |
| 状态 | typed events + reducer + checkpoint | 可恢复、可回放、便于教学和调试 |
| 测试 | fake model / fake TMDB / tmp_path | 核心测试完全离线和确定 |

官方文档将 Agents SDK定位为由 SDK 管理 Agent loop，并提供 sessions、tracing、
guardrails 和可恢复审批的方案：

- [Agents SDK](https://developers.openai.com/api/docs/guides/agents)
- [Tools](https://developers.openai.com/api/docs/guides/tools)
- [Guardrails and human review](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)

## 4. Run 状态模型

初始 `RunState`：

```text
RunState
├── run_id
├── phase
├── work_type
├── authorized_series_root
├── authorized_output_root
├── candidate_snapshot_id
├── tmdb_candidates: (work_type, tmdb_id)
├── selected_work_type / selected_tmdb_id
├── mapping_draft
├── validation_issues
├── plan_id / plan_hash
├── approval_status
├── budgets
└── last_event_sequence
```

固定 phase：

```text
BOOTSTRAP
→ IDENTIFY_SERIES
→ MAP_EPISODES
→ BUILD_PLAN
→ AWAITING_APPROVAL
→ APPLYING
→ COMPLETED

任意阶段 → FAILED
APPLYING → ROLLED_BACK
```

状态只能通过 typed event 转换。普通 assistant 文本不能改变 phase。

核心事件：

- `RunStarted`
- `CandidateSnapshotCreated`
- `ToolRequested`
- `ToolSucceeded`
- `ToolRejected`
- `SeriesSelected`
- `MappingSubmitted`
- `MappingRejected`
- `PlanBuilt`
- `ApprovalRequested`
- `PlanApproved`
- `ApplyStarted`
- `MoveApplied`
- `ApplyFailed`
- `RollbackCompleted`
- `RunCompleted`

## 5. MVP 工具协议

| 工具 | Phase | 输入 | 输出与限制 |
| --- | --- | --- | --- |
| `list_candidates` | IDENTIFY/MAP | kind、cursor、limit | opaque ID、相对展示名、有限元数据；分页有上限 |
| `search_tmdb` | IDENTIFY | query、work_type | 类型化候选；filter 必须匹配 run；anime 仅对空 genre 的标准化精确标题允许 fallback；只能访问 TMDB adapter |
| `get_tmdb_series` | IDENTIFY/MAP | work_type、tmdb_id、language | 仅 series 类型；白名单字段和大小受限的文本 |
| `get_tmdb_season` | MAP | work_type、tmdb_id、season、language | 仅已选 series；集号、标题、限长 overview |
| `search_dir` | MAP | selected identity 或 literal name、cursor、limit | 只缓存并过滤媒体库根的直接子目录，返回 run-scoped opaque ID |
| `list_dir` | MAP | directory_id、cursor、limit | 每次只列一层目录和视频，最大深度 3；不返回路径或文件 identity |
| `detect_subtitle_variant` | MAP | subtitle_id | `chs`、`cht` 或 `chi`；限字节采样 |
| `select_series` | IDENTIFY | tmdb_id | 领域事件；ID 必须来自当前候选 |
| `submit_mapping` | MAP | strict mapping object | valid result 或结构化 validation issues |

工具规则：

- 工具只接受当前 run 中的 ID，不接受绝对路径。
- schema 禁止额外字段。
- 每次调用经过 phase、capability、预算和参数策略。
- 文件名、overview 和字幕样本作为不可信数据，不作为指令。
- 历史媒体库观察仅用于库存冲突与审查提示，不决定或放宽 canonical 目标。
- validation observation 只包含错误码和最小必要上下文。
- 第一版不提供 shell、网页搜索、任意 HTTP、任意读文件或任意写文件。
- `apply`、`move_file` 和 `delete_file` 不是 Agent 工具。

## 6. RenamePlan 与审批协议

`RenamePlan` 必须是 canonical JSON snapshot，至少绑定：

- schema version 和 policy version；
- `run_id`；
- 授权的 source/output roots；
- candidate snapshot hash；
- 每个 source 的相对路径和 identity；
- 每个确定性 destination 相对路径；
- mapping 与字幕 variant；
- 未映射文件；
- collision/preflight 结果；
- 创建时间。

对 canonical bytes 计算 `plan_hash`。用户批准的是：

```text
run_id + plan_hash + scope + expiry + one-time nonce
```

Executor 执行前重新验证：

1. plan hash 和策略版本未变化；
2. 审批有效、未过期、未使用；
3. source identity 与扫描时一致；
4. source 仍位于授权输入根目录；
5. destination 仍位于授权输出根目录；
6. source、destination 和父目录不存在 symlink escape；
7. 目标仍不存在且 collision 状态未变化。

任一项失败都停止执行并要求重新规划、重新批准。

## 7. 目标目录结构

```text
src/reeloom/
├── agents/
│   ├── organizer.py
│   └── prompts.py
├── runtime/
│   ├── state.py
│   ├── events.py
│   ├── reducer.py
│   ├── budgets.py
│   └── policy.py
├── tools/
│   ├── registry.py
│   ├── candidates.py
│   ├── tmdb.py
│   ├── inventory.py
│   └── submission.py
├── kernel/
│   ├── models.py
│   ├── scanner.py
│   ├── mapping.py
│   ├── naming.py
│   ├── path_policy.py
│   └── plan.py
├── executor/
│   ├── approval.py
│   ├── apply.py
│   └── rollback.py
├── adapters/
│   ├── openai.py
│   ├── tmdb.py
│   ├── filesystem.py
│   └── storage.py
└── observability/
    ├── traces.py
    └── evals.py

tests/
├── runtime/
├── tools/
├── kernel/
├── executor/
├── integration/
└── evals/
```

## 8. 分阶段课程与实施里程碑

### M0：领域契约与威胁模型

学习目标：区分 Agent 能力和不能交给 Agent 的不变量。

交付：

- 项目模型、错误分类和安全不变量；
- candidate/mapping/plan 的严格 schema；
- 完整命名契约：`{series_zh_cn} ({year}) {tmdb-{tmdb_id}}` 根目录和 `Sxx`
  目录；视频使用 `动画名 SxxExx{ext}` 或 `动画名 SxxExx-Eyy{ext}`，
  禁止 episode title；字幕使用同一 base 加 `.chs/.cht/.chi` 后缀；
- `S00` 与 OVA/OAD 规则：优先使用 TMDB 任意 season 中明确的 OVA/OAD 线索；
  无明确线索时，按本地 OVA/OAD 顺序与剩余 TMDB Specials（S00）顺序对应；
- 威胁模型；
- 从 aninamer 提取的行为 fixture，不导入其 runtime。

测试：

- extra keys、错误 ID 类型和重复 ID；
- season/episode 越界与 range overlap；
- destination collision；
- 路径清洗；
- “只处理映射剧集和关联字幕”。

完成条件：纯领域测试离线通过，不依赖 Agents SDK。

### M1：最小 Agents SDK Run

学习目标：理解 Agent、tool、observation、state、event 和 stop condition，
并从第一轮就使用 SDK 的 Agent loop，而不是自建后再迁移。

交付：

- `RunState`、phase、typed events 和 reducer；
- OpenAI Agents SDK 的单 `EpisodeOrganizerAgent` 和 Runner；
- 实现 SDK model protocol 的 scripted fake model；
- SDK function tools、tool guardrails、phase policy 和总预算；
- append-only 内存 event store。

`RunState`、events 和 reducer 只表达 Reeloom 领域状态。模型调用、tool-call
调度、暂停和恢复使用 SDK 提供的运行循环，不在项目里复制第二套 loop。

测试：

- 正常结束；
- 未知工具与 phase 不允许的工具；
- 非法参数；
- 可重试/致命错误；
- 重复失败和预算耗尽；
- 相同 transcript 可确定性 replay。

完成条件：fake Agent 可完成一个无文件副作用的多轮工具循环。

### M2：安全候选快照

学习目标：把操作系统资源封装成受限 capability。

交付：

- 不跟随 symlink 的 scanner；
- 不可变 candidate snapshot；
- `video:N` / `subtitle:N` run-scoped ID；
- 分页 `list_candidates`；
- 文件扩展名和排除规则。

测试：

- 稳定 ID、分页和大小上限；
- `..`、绝对路径和 root escape；
- 文件及父目录 symlink escape；
- `.env*` 字面路径和解析后路径；
- 恶意文件名 prompt injection。

完成条件：Agent 无法用工具读取 candidate snapshot 之外的数据。

### M3：TMDB 识别

学习目标：让 Agent 自主选择信息获取顺序，同时保持网络边界可替换。

交付：

- mockable TMDB adapter；
- search/series/season 工具；
- `select_series` 领域动作；
- TMDB `media_type` 与 archive `work_type` 分离；
- run-scoped `anime/tv_series/movie` search filter；
- `anime` 严格映射为 TV + Animation genre 16，不做类型降级；
- 候选 capability 使用 `(work_type, tmdb_id)`，避免 TV/Movie ID namespace
  混淆；
- zh-CN 标题优先和年份规则；
- 请求超时、缓存与结果大小限制。

测试：

- 全部使用 fake TMDB；
- 单候选、歧义候选和无候选；
- 非候选 TMDB ID 被拒绝；
- OVA/OAD/Specials 中英文数据；
- 网络失败映射为结构化 observation。

完成条件：fake Agent 能识别 series 并进入 `MAP_EPISODES`。

### M4：剧集与字幕映射

学习目标：实现“模型做 mapping，代码做 enforcement”的反馈循环。

交付：

- `submit_mapping` strict schema；
- 集数边界、重叠、库存和字幕归属校验；
- `.chs/.cht/.chi` 判定；
- 结构化、脱敏、限长 validation issues；
- 最大轮数、工具数、token 和时间预算；生产配置默认总时间 600 秒，并由
  Admin 为后续新 run 在有界范围内调整。

测试：

- 第一次 mapping 失败，Agent 根据 observation 修正；
- 普通集、多集文件、Specials、OVA/OAD；
- 字幕重复关联和未知 ID；
- 已有库存冲突；
- 恶意文本不能改变工具策略。

完成条件：有效 mapping 只能通过领域事件进入 `BUILD_PLAN`。

### M5：确定性 Plan Compiler

学习目标：把 Agent 结果编译为不可变、可审批的事务输入。

交付：

- 文件名 sanitization 和确定性 destination；
- collision detection；
- canonical `RenamePlan`；
- candidate/source identity；
- `plan_hash`；
- plan preview 和未映射清单。

测试：

- 任何 destination 都位于 output root；
- 模型不能指定路径；
- 改动 snapshot 任一字节都会改变或使 hash 失效；
- dry-run 不改变文件系统；
- 完整领域输入、candidate snapshot、policy version 和注入时钟相同时，
  plan canonical bytes 相同。

完成条件：run 到达 `AWAITING_APPROVAL` 后停止，文件系统完全未变化。

### M6：审批、Executor 与 Rollback

学习目标：理解 Agent 暂停/恢复与副作用隔离。

交付：

- 结构化审批记录；
- expiry、nonce 和一次性 consumption；
- 无 LLM、无外网的 Executor；
- 审批 nonce 在任何移动前原子 claim，拒绝并发执行和重放；
- 最终 preflight，以及基于目录句柄的 relative/no-follow 操作策略；
- MVP 要求源和目标处于同一文件系统，跨文件系统 rename 直接拒绝；
- transaction journal、result 和 rollback plan；
- 崩溃后的幂等恢复。

测试：

- wrong hash、审批过期和审批重放；
- 批准后源文件变化；
- 目标临时出现；
- plan 被篡改；
- partial failure 和 rollback；
- 未映射资源保持不变；
- 永不覆盖、永不删除。

完成条件：只有精确批准的 plan 能执行，任何状态漂移都 fail closed。

### M7：真实模型、持久状态、Trace 与 Eval

学习目标：从“代码正确”转向“Agent 行为可测量、可改进”。

交付：

- 真实 OpenAI model/provider 配置；
- 持久 session/checkpoint 恢复；
- 脱敏 trace；
- 离线 scripted transcripts；
- eval dataset 和任务级指标；
- 真实 API smoke test 与离线测试分离。

指标：

- 最终 mapping 成功率；
- validator 首次/最终通过率；
- 工具调用数、token、延迟和成本；
- 人工澄清率；
- 未映射文件正确保留率；
- 安全策略拒绝的误报/漏报。

完成条件：核心 test suite 不依赖网络；真实模型行为可由固定 eval 重放比较。

### M8：服务器控制面、配置 API 与交互式修订

学习目标：理解长生命周期 Agent 服务、配置能力、后台调度、human-in-the-loop
修订、PostgreSQL 事务边界和 HTTP 信任边界。

当前状态：M8.0-M8.8 已全部完成并通过验收。实现没有继承实验性的 filesystem
control-plane，而是从 PostgreSQL 17 foundation 开始逐阶段构建；验收证据见
[M8 Requirement Matrix](m8-requirements.md) 和
[M8 实现评审](m8-review.md)。

交付：

- PostgreSQL 唯一控制面和 transport-neutral application services；
- watch-bound source/library root、轮询、provider profile、secret 和 apply policy
  的版本化配置 API；
- 由 Admin 明确配置且受 HTTPS-only transport policy 限制的 OpenAI-compatible provider；
- watcher/discovery、bounded current observations、幂等 run/job 创建；
- run/plan/approval/apply HTTP API 与脱敏 SSE；
- 在原 run/Agent session 上只读 question，并以 immutable plan revision
  接受人工纠正或额外要求；
- `plan_only/manual/automatic` orchestration，全部继续使用 exact
  `ApprovalRecord`；
- deterministic crash recovery 与自然语言 revision 的明确隔离；
- completed plan 的 reapply 继续原 logical Agent/session 接收人工纠正或附加
  要求，以 fresh 完整 mapping 编译独立 immutable amendment plan 和新事务；
  原 completed 记录不变，也不复用 crash recovery。

安全边界：

- 配置只属于 authenticated admin，Agent 和普通 run API 不接受路径、URL 或
  secret；
- `work_type = null` 必须先建立 trusted type；Anime/TV/Movie 都只能由
  trusted watch 配置确定；
- API key write-only，永不进入 config response、event、session、trace 或日志；
- question 不改变领域状态；revision 继续原 Agent session、生成新 plan hash，
  不修改旧 plan/approval；
- completed reapply 同样继续原 Agent session，但只通过确定性 compiler 把 fresh
  mapping 与当前 completed layout 的差异变成 amendment；无差异不创建空事务；
- automatic apply 由确定性 policy 签发一次性系统 approval，不允许 Agent
  自批；
- PostgreSQL 事务不跨 Agent、TMDB、扫描、plan/secret 文件写入或 rename；
- 第一版固定单实例、单进程、单 worker，不用 TTL lease 假装多进程 fencing。

完成条件：交互式前端只凭稳定 API 即可管理配置、查看 run、在原 Agent 上提问和
迭代 plan revision、审查或自动批准计划并观察执行；completed plan 的 reapply
使用独立安全事务；全部核心测试离线，后台并发与重启 fail closed。

详细架构、分步、测试和 API 边界见 [M8 计划](m8-plan.md)。

### M9：交互式 Web UI

学习目标：理解浏览器认证、同源静态边界、runtime response validation、durable
SSE、CAS 配置编辑和网络不确定性下的 effect-safe UX。

当前状态：M9.0-M9.8 已完成。React UI 只消费稳定 API；Admin 可完成配置、观察、
exact plan 审查、Agent question/revision/reapply、人工审批、执行结算和 recovery。
UI 不编译 plan、不生成路径、不签发审批，也不推断 filesystem 结果。

安全边界：

- 只有 `/` 和 manifest 中的 hash assets 可匿名 GET/HEAD；
- Admin token 经 session 验证后才保存在固定 localStorage key；
- config retain 绑定 exact revision，Admin GET 返回结构化路径但不返回 secret；
- preview 绑定 exact lineage/hash 并只投影相对路径；
- interaction history 只包含显式用户消息和 final reply；
- authenticated fetch SSE 使用 durable cursor，cursor ahead 完整 resync；
- UI approval 始终 `automatic: false`，recovery 只使用服务端 exact approval ID。

完成证明见 [M9 计划](m9-plan.md)、[M9 Requirement Matrix](m9-requirements.md)
和 [M9 实现评审](m9-review.md)。

### M10：Movie 领域支持

学习目标：理解同一安全执行平面上多个严格 plan family 的兼容方式，以及
single-feature Movie mapping、整目录不存在约束和 completed-layout reapply。

当前状态：M10.0-M10.6 已完成。Movie 使用独立 `MovieIdentity`、
`MovieMappingDraft`、`MovieRenamePlan v1` 与 `MovieAmendmentPlan v1`；复用
既有配置、lineage/preview、interaction、approval、Executor、rollback、
recovery 和 Web 页面。

固定边界：

- 每个 Movie run 只选择一个正片视频；未选视频和字幕保持 unmapped；
- Movie Agent 只开放 Movie TMDB capability、字幕检测和完整 mapping；
- 缺少可靠上映年份时停止规划；
- initial Movie 根目录必须完全不存在，compile、preflight 与原子 mkdir 均检查；
- Movie destination 只允许两层；Episode 继续只允许 `Sxx` 三层；
- amendment 绑定当前 completed parent hash 与 transaction，执行前再次核对 head；
- reapply 只复验 durable completed layout，后来出现的文件不进入；
- no-op 不创建 plan、approval 或 transaction。

完成证明见 [M10 计划](m10-plan.md)、[M10 Requirement Matrix](m10-requirements.md)
和 [M10 实现评审](m10-review.md)。

### M11：以文件夹为单位的入站归档

当前状态：M11 已完成。每个 watch 根的直接子文件夹独立稳定并创建一个 run；
媒体仍由 M0-M10 immutable plan 执行，随后由独立
`FolderDispositionPlan v1` 将残留归入 watch-local `archive`、安全移除已验证
空目录，或将 eligible deterministic failure 归入 `fail`。其他 Agent/Provider
失败先保留源目录并最多重试三个新 generation；第四次失败才以
`agent_retry_exhausted` 归入 `fail`。终态 generation 的源目录持续缺失且没有
未结算 effect 时，observation 收敛为 `blocked/source_folder_missing`。目标名
只由当前活动计划或未结算事务占用；脱离 generation 的不可变历史计划不再制造
虚假的 `.1`、`.2` 后缀。

`archive`、`fail`、顶层隐藏目录、散落文件和顶层 symlink 不参与发现；嵌套
symlink 只作为不透明残留，任意 `.env*` 会阻断整个文件夹。Folder disposition
使用 exact approval、独立 journal、atomic no-replace rename 和 exact recovery；
迟到内容重新稳定并生成新 hash，不能复用旧组合。

完成证明见 [M11 ADR](adr/0004-m11-folder-intake.md)、
[M11 Requirement Matrix](m11-requirements.md) 和
[M11 threat model](m11-threat-model.md)。

### M12：Telegram 出站通知与 PostgreSQL durable outbox

当前状态：M12.0-M12.3 已完成。已建立 closed
notification payload、MarkdownV2 转义、受控 TMDB poster ref、900-byte caption
上限和三种固定样式，以及 PostgreSQL schema 26 durable outbox、严格 codec、
dedupe、短 lease、fenced settlement、retry/dead、receipt 与重启回收；固定
Telegram adapter、write-only 配置、Admin 测试、health 指标和单 worker 生命周期
也已接通。

实现使用 PostgreSQL transactional outbox、稳定 dedupe key、短 lease、
`FOR UPDATE SKIP LOCKED` claim、有界 retry/dead 和单 worker 恢复网络波动或进程
重启。通知只有 `plan_ready`、`archive_completed`、`attention_required` 与内部
`test`；Telegram 只读旁路不提供批准或命令。

固定用途 Telegram 出站已于 2026-08-03 获得显式授权；它只允许固定 HTTPS host，
不允许 redirect、proxy URL、入站 webhook、命令或网络化测试。

详细分步、边界和验收见 [M12 计划](m12-plan.md)、
[M12 Requirement Matrix](m12-requirements.md)、
[M12 threat model](m12-threat-model.md) 和
[ADR 0006](adr/0006-telegram-outbound-notifications.md)。

### M13：动漫字幕探测与自动获取

当前状态：M13 已完成。已建立 strict frozen embedded-subtitle、论坛搜索
capability、Agent selection 和 archive inspection 领域 schema，新增
provider-neutral `VideoSubtitleInspector`、`SubtitleSearchProvider` 与独立
`SubtitleAcquisitionPlanStore` port，并实现 canonical
`SubtitleAcquisitionPlan v1`、完整 hash binding、固定资源上限、确定性字幕目标名
和语义篡改校验。动漫 Agent 现可在 snapshot 完全没有独立字幕时，以 opaque
`video:N` 每季调用一次 `check_sub_from_video`；adapter 通过 no-follow FD 和
before/after identity 将固定 `ffprobe` 绑定到 snapshot，结果由 typed event、
reducer 和 v6 state projection 持久化。FFmpeg 7.1.5 与资源限制 argv 均固定；
超时、异常输出、不支持格式、symlink 和 identity drift 全部 fail closed 为
indeterminate 或结构化错误。TV/Movie 工具集未改变，M13.1 不访问论坛且不产生
文件副作用。M13.2 已实现固定 origin、无通用 redirect/proxy/登录 Cookie 的 ACG.RIP Discuz
只读 provider，以离线 fixtures 覆盖 fid 37/46、搜索/回复分页、动态 aid、多版本、
多卷 grouping、挑战/限流/预算与 parser drift。`search_sub` 只在季度样本明确无可
识别中文内嵌字幕时开放；`select_subtitle_release` 是唯一接受事件，即使只有一个
候选也必须显式选择。Agent loop hardening 已将 runtime projection 升至 v8、Anime
AgentDefinition 升至 v7：配置关闭时不注册 M13 tools，配置开启时 probe/search/
select/attention/mapping 共享确定性状态投影，禁止漏季、未翻完分页或直接 submit
mapping 绕过流程；TV v4/Movie v4 未改变，生产 provider 也要等显式 opt-in 配置后
才实例化。M13.3 新增媒体根外的 restricted
attachment fetcher、固定官方 `7zz 26.02` 的 magic/header/technical-manifest
inspector、逐字幕 stdout extraction/hash、完整多卷 RAR 校验和独立 write-once
acquisition plan store。归档工具不控制输出路径；动态签名 URL 不进入 capability、
plan、state 或 trace。加密、嵌套归档、危险路径/特殊文件、资源超限、Unicode 重名、
远端身份变化和内容漂移均 fail closed。M13.4a 建立独立 acquisition policy、
`SUBTITLE_ACQUIRE` approval scope、deterministic transaction 和 write-once journal；
M13.4b 的独立 executor 在 claim 后重新获取并精确复核 archive、manifest、member 与
source identity，以 `O_EXCL|O_NOFOLLOW` 逐 member 写入隐藏 staging，经文件和目录
fsync 后优先使用 native no-replace directory rename 发布；仅当源与目标均确认位于
FUSE 挂载且原生能力明确不受支持时，允许在目标 no-follow 不存在预检后使用
checked-rename 兼容 CloudDrive2 类挂载。碰撞、symlink、hash 漂移、非 FUSE 的原子
rename 不支持和 crash window 均 fail closed 或由 journal 幂等恢复。M13.4c
新增 schema 27 durable successor outbox：发布 settlement、旧 run `superseded`、旧
job terminal、lineage 单次获取记录与 outbox enqueue 构成一个事务。worker 通过
watch/folder capability 触发 fresh no-follow scan，只有原 source folder identity、
新 snapshot、发布目录 identity 与全部计划字幕一致时，才原子注册新 discovery/run/job；
successor 继承 lineage，因此不能再次自动获取。M13.4d 已将显式 opt-in 的生产
search/planner/executor、automatic/manual/plan-only coordinator、successor worker、
独立人工审批 API 与 bounded read model 接入 composition；控制面仅显示 acquisition
计划/策略/状态和 successor 状态，不暴露论坛正文、URL 或路径 capability。配置页提供
显式 provider opt-in，运行页只能执行服务端授权的独立字幕动作。离线跨层验收已
覆盖 selected capability → persisted plan → exact approval → re-fetch/publish → fresh
snapshot/successor，后继字幕重新成为普通 `subtitle:N` 且 lineage 闸门永久关闭。

后续步骤固定为：

- M13.1（完成）：单视频 ffprobe 探测、每季一个 advisory quota、typed
  event/reducer/state codec；
- M13.2（完成）：ACG.RIP fid 37/46 只读搜索、opaque capability 和 Agent 显式
  选择；
- M13.3（完成）：受限下载、ZIP/7z/RAR/多卷 RAR inspector、独立 durable store 与
  acquisition plan compiler；
- M13.4：独立审批、journal、no-replace/FUSE checked-rename 发布、durable successor 和 lineage
  循环闸门。其中 M13.4a 已完成 config schema v5、显式 ACG.RIP opt-in、独立
  acquisition policy、`SUBTITLE_ACQUIRE` scope，以及 deterministic transaction/
  write-once journal；M13.4b 已完成 re-fetch、逐 member 安全写入、native 优先且仅限
  已确认 FUSE 的 checked-rename 兼容发布与 crash recovery executor；M13.4c 已完成
  durable successor outbox、fresh scan
  registration、supersede 与 lineage 循环闸门；M13.4d 已完成 production composition、
  独立 API/UI/read model、blocked successor attention projection 与端到端审计。

ACG.RIP 是唯一新增 fixed-purpose origin；不得接受自定义 URL、通用 redirect、proxy、
登录或反爬规避。仅保留站点当次签发的有界匿名 Discuz 会话 Cookie，并手工验证一次
搜索 POST 的精确同源结果跳转；所有 pytest 保持离线。robots.txt 不属于运行时配置或控制面。边界见
[M13 计划](m13-plan.md) 与
[ADR 0007](adr/0007-m13-subtitle-acquisition-boundary.md)。

### M14：基于当前状态的前向收敛执行器

当前状态：M14.0-M14.1b 已完成。已增加不含持久 stat identity 的 strict frozen v2 语义身份、
canonical `RenamePlan v2`、纯 `ExecutionOperation v2` 状态机和完整 source/destination
真值表。Watcher 现在单次 poll 只生成一个 namespace snapshot，同时计算保留兼容性的
v1 identity 与 `path + kind + size` v2 inventory；视频 v2 identity 不读取内容，字幕使用
完整文件 SHA-256。离线 scheduler contract 已可按 v2 identity 跨 poll 判断稳定，因此
metadata-only 变化与同路径同大小的视频替换不会重启 generation。PostgreSQL discovery
与 episode production plan-only 已切换到语义 snapshot/plan v2；Agent 在规划前按当前路径
no-follow 重扫并核对语义身份。manual/automatic、Movie 与启用 ACG.RIP 的 M13 流程继续
使用 v1，直至 M14.2/M14.3 分别接管；本增量不开放新的文件副作用。

固定决策：

- 视频持久身份只使用相对路径、regular-file 和 size；字幕、下载归档及解压 member
  继续使用完整 SHA-256；`device/inode/mtime/ctime` 不进入 v2 snapshot、plan hash、
  freshness 或恢复。
- root 只绑定 config revision、watch ID 和 no-follow 绝对路径。当前路径状态是执行与
  reconcile 的唯一事实。
- 执行 forward-only；独立安全项继续，部分成功不 rollback。原生 no-replace 不可用时，
  在全局 effect mutex 内做 checked rename，并接受与外部 writer 之间的极小竞态。
- approval 只授权 operation 首次启动；后台重试收敛同一 operation。v2 不再向用户暴露
  approval ID 或定向 recovery。
- 字幕改为 plan-owned 目录逐 member 排他写入并以 complete marker 发布；folder
  archive/fail 降为 non-blocking best-effort housekeeping。

后续增量：

- M14.1a（完成）：watcher 双 identity、完整字幕 hash、单次 namespace scan 与离线
  scheduler semantic generation contract。
- M14.1b（完成）：PostgreSQL discovery、runtime/store codec 与 episode production
  plan-only 切换到语义 snapshot/plan v2；v1 manual/automatic、Movie 与 M13 写路径保持
  隔离。
- M14.2：统一 operation ledger/lease、forward executor、自动 rescan 和 v2 API/UI；先
  使用 semantic fake filesystem 离线验收。
  - M14.2a（完成）：增加单一 PostgreSQL `execution_operations_v2` ledger、严格 lease
    CAS 与 approval/operation 互斥消费；尚未连接文件系统 effect、API 或 UI。
  - M14.2b（完成）：实现 current-state filesystem adapter、forward executor、固定有界
    重观察与 fresh-scan intent；覆盖 checked rename、延迟可见、rename 报错但已完成、
    stat identity 不稳定和 fsync 不支持。durable rescan 投递随 M14.2c coordinator 接入。
  - M14.2c（完成）：接入 server-owned execute/reconcile coordinator、原子终态结果与
    fresh-scan outbox、后台 lease worker、v2 Run read model、strict `/execute` API 和
    Web UI。浏览器不提交 automatic 或 approval ID，v2 UI 不显示定向 recovery；
    manual/automatic production 切换仍等待 M14.3 housekeeping/marker 发布完成。
- M14.3：字幕 marker 发布、普通 durable scan request 和非阻塞 folder housekeeping；
  移除三套面向用户的 recovery 入口。
- M14.4：一次性 supersede 未结算 v1 effect、投递 fresh scan、清除旧锁，并在稳定后
  删除 legacy recovery 与 v1 executor 写路径。

完整决策、已接受的完整性取舍与 M14.0 副作用边界见
[ADR 0008](adr/0008-m14-forward-convergence.md)。

## 9. 第一条端到端验收测试

```text
fake model + fake TMDB + tmp_path
→ 创建受限 run 和 candidate snapshot
→ Agent 查询候选和 TMDB
→ Agent 选择 series
→ Agent 提交一次越界 mapping
→ submit_mapping 返回结构化 validation issue
→ Agent 修正并再次提交
→ kernel 编译 RenamePlan 和 plan_hash
→ run 停在 AWAITING_APPROVAL
→ 文件系统内容、文件名和目录结构完全未变化
```

第二条端到端测试才覆盖批准后执行：

```text
approve exact run_id + plan_hash
→ Executor final preflight
→ 写 journal/rollback
→ 执行 rename
→ approval consumed
→ 同一审批重放被拒绝
```

## 10. MVP 非目标

- 多 Agent、handoff 和 specialist。
- 向量数据库或跨 run 长期记忆。
- shell、代码执行、除固定用途 ACG.RIP adapter 外的任意网页搜索或任意 MCP
  工具。
- Agent 自己改变 prompt、policy、tool schema 或授权根目录。
- 未经管理员显式 `automatic` policy 的后台无人值守执行。
- 跨文件系统 copy + delete。
- 单个 run 内混合多个作品。
- 用 prompt 代替 schema、权限、路径校验或审批。

## 11. 每个里程碑的 Definition of Done

- 有 pytest 正常路径和失败路径覆盖。
- 测试离线、确定、可 replay。
- 新能力只暴露最小权限。
- trace 和错误信息不包含凭据或不必要的文件内容。
- 除显式 opt-in TMDB/OpenAI live smoke 对固定 `.env` 的 allowlist、no-follow
  只读例外外，不读取或访问 `.env*`。
- 未映射文件和非目标资源保持不变。
- 文档同步更新状态、决策和下一步。
- 不提前实现后续里程碑。

## 12. 开放问题

尚未解决的问题在对应里程碑前通过 ADR 决定：

1. source identity 是否在高风险场景增加内容 hash。
2. Executor 是否采用单独进程、单独系统用户或容器隔离。
3. TMDB cache 的位置、TTL 和离线导入格式。
4. trace 的保留周期和用户可见脱敏视图。
5. Movie multipart、extras 和既有目录增量合并是否应作为独立后续里程碑。
