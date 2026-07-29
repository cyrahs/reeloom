# Reeloom 开发内部状态

M10 在 PostgreSQL 16–18 单实例 control plane 和同源 React Web UI 上增加
独立 Movie 领域支持。默认操作仍只生成 immutable plan；只有 exact
`ApprovalRecord` 被一次性 claim 后，隔离 Executor 才能移动文件。

部署与接口见 [deployment](deployment.md) 和 [HTTP API](api.md)；Movie
边界与完成条件见 [M10 plan](m10-plan.md)。

Reeloom 是一个从零设计的 **agent-native 动画、电视剧与电影整理器**。它能识别
剧集或单部电影及其外置中文字幕，结合
TMDB 元数据生成安全的重命名计划，并在用户批准后执行。

这个仓库不是 `aninamer` 的迁移分支。旧项目只作为需求说明、领域规则
参考和 golden test oracle；Reeloom 不在运行时依赖旧项目。

## 当前状态

**M10 / Movie 领域支持（已完成并通过验收）**

Movie 使用独立 identity、单正片 mapping、initial/amendment plan 与专用 Agent
tool set，但复用既有浏览器 preview、interaction、approval、Executor、rollback
和 recovery。详细见 [M10 验收结论](m10-review.md)。

M0-M5 已能把 Agent 的语义结果编译为可精确审批的事务输入；M6 建立了独立于
Agent/LLM 的审批消费、最终检查、执行与恢复边界：

- Plan Compiler 不是 Agent 工具；mapping 成功后由确定性代码根据 snapshot
  relative path 和扩展名生成 destination，模型没有路径输入通道；
- `RenamePlan` 绑定 run、source/output root identity、完整 candidate/source
  identity、trusted work type、mapping、字幕变体、moves、未映射清单、策略
  版本和注入时间；
- canonical JSON bytes 使用固定字段和排序计算 `sha256` `plan_hash`，任何被
  绑定内容的变化都会得到不同 hash 或验证失败；
- 文件系统 adapter 只读检查目标不存在、父目录不是 symlink，并拒绝已有目标；
  M6 执行前仍会进行最终 preflight；
- `PlanBuilt` 和精确 hash 的 `ApprovalRequested` 事件将 run 停在
  `AWAITING_APPROVAL`；plan 在请求批准前已经持久化，普通模型文本不能建立或
  批准计划；
- preview 只展示确定性 source/destination 和未映射文件，整个 dry-run 不创建
  目录、不移动、不覆盖文件。
- `ApprovalRecord` canonical 绑定 `run_id + plan_hash + scope + expiry +
  nonce`，严格拒绝多余字段、非规范编码和记录篡改；
- filesystem approval store 只在独立授权根中 no-follow、有界读取记录，并以
  匿名 inode + no-replace link 在任何未来移动前原子发布持久 claim；
- wrong binding 和 expiry 不消费批准；并发 claim 只有一个成功，重启后的重放
  仍被拒绝。
- content-addressed plan store 只按 `plan_hash` 保存和 no-follow 读取 canonical
  bytes，Executor API 只接受持久化 hash 与 approval ID，不接受路径或 move；
- apply 先持久化幂等 transaction/rollback header，再原子 claim approval，随后
  重新打开并核对 source/output root、全部 mapped/unmapped source identity 与
  字幕 sample digest，拒绝 symlink、目标出现和目录竞态；
- 每个 move 的 source 与目标现存父目录必须处于同一文件系统；MVP 不提供
  copy/unlink 跨文件系统降级。
- apply 在任何目录创建和 rename 前持久化显式 rollback manifest；journal
  event 使用原子发布的独立 immutable 文件 append-only 记录，不原地覆盖状态；
- media rename 使用 Linux `renameat2(RENAME_NOREPLACE)` 或 Darwin
  `renameatx_np(RENAME_EXCL)`，目标在最终检查后临时出现也不会被覆盖；
  Linux FUSE 明确返回不支持时允许 `fuse_checked_rename` 降级：再次检查目标后
  使用普通 rename，并保留 exact recovery；该后端接受外部并发写入的残余竞态；
- 每次 rename 返回或报错后都重新验证 source/destination identity；只读 API
  不触发 recovery。配置页的显式探测仅操作自身创建的空目录；
- partial failure 自动逆序 rollback；崩溃恢复依据 exact plan、claimed
  approval、immutable journal 和源/目标 identity 唯一判定状态，歧义时返回
  `recovery_required`；
- 同一 transaction 的 apply/recover 共享进程级 lease；当前 move 或 terminal
  durability 不确定时不写伪终态，冲突终态直接 fail closed；
- unmapped 文件永不移动，rollback 不覆盖重建的 source，也不删除媒体文件；
  已创建的空归档目录可以保留。

adapter 只接受调用方显式注入的凭据，不加载配置文件。自动化测试不访问真实
网络。真实媒体副作用只存在于不属于 Agent tool 的 `FilesystemExecutor.apply`
中，并且只能由持久化 plan hash 与一次性 approval ID 启动。确定性的
`ApprovalResumeService` 将停止的 run 转换为 `PlanApproved → ApplyStarted →
RunCompleted/RollbackCompleted`，不会把 apply 暴露为 Agent tool。

M7 将全部 runtime event 编码为严格、版本化的 canonical envelope，并新增
run-scoped filesystem checkpoint store。每条事件在 reducer 验证后先写匿名
inode 并 `fsync`，再以 no-replace link 原子发布；sequence、run binding、
前序 digest、record digest 或 replay transition 任一不一致都会 fail closed。
Organizer 和审批恢复现在依赖 `EventStore` 协议，因此同一领域流程可使用内存
测试 store 或在进程重启后恢复的文件 store，而不把 checkpoint 细节泄漏给
Agent。

Agents SDK 对话 history 使用单独的 append-only `Session` adapter；`add`、
`pop` 和 `clear` 都是不可变操作记录，不删除旧数据，也不参与领域 phase 判定。
版本化 scripted transcript 与固定 eval dataset 通过真实 SDK Runner 离线重放，
并输出语义 mapping、validator/tool、input/output token、延迟、成本估算、
人工澄清、unmapped 保留和类型化安全拒绝指标。
redacted trace 只投影 allowlist 元数据，不包含 prompt、tool observation、文件名、
字幕正文或 TMDB 标题。

真实 OpenAI adapter 使用显式注入的 key 与 model 配置、官方 Responses API
endpoint，并强制 `store=False` 和单一顺序 tool call。线上 eval 必须显式
`--live --model ...`，只读取进程环境的 `OPENAI_API_KEY`；pytest 和默认 eval
始终离线。

M10 已在不放宽 `select_series` 的前提下加入独立 `select_movie`、单正片
mapping 和两层 Movie 命名；Movie 不会被伪装成 `SxxExx`。

详细路线见 [初步实施计划](initial-plan.md)，M0 验收结论见
[M0 Definition of Done](m0-review.md)，M1 验收结论见
[M1 Definition of Done](m1-review.md)，M2 验收结论见
[M2 Definition of Done](m2-review.md)，M3 验收结论见
[M3 Definition of Done](m3-review.md)，M4 验收结论见
[M4 Definition of Done](m4-review.md)，M5 验收结论见
[M5 Definition of Done](m5-review.md)，M6 验收结论见
[M6 Definition of Done](m6-review.md)，M7 验收结论见
[M7 Definition of Done](m7-review.md)，M8 验收结论见
[M8 implementation review](m8-review.md)，M9 验收结论见
[M9 implementation review](m9-review.md)，M10 验收结论见
[M10 implementation review](m10-review.md)。

## 本地验证

```bash
.venv/bin/python -m pytest -q -m "not postgres"
REELOOM_TEST_POSTGRES_DSN=... .venv/bin/python scripts/run_postgres_tests.py
cd web
npm ci
npm run lint
npm run typecheck
npm test
npm run build
REELOOM_TEST_POSTGRES_DSN=... npm run e2e
```

固定离线 Agent eval：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_offline_eval.py
```

显式 opt-in 的 OpenAI 线上对比（不读取 `.env`）：

```bash
OPENAI_API_KEY=... PYTHONPATH=src .venv/bin/python \
  scripts/openai_live_smoke.py --live --model gpt-5.6
```

### 显式 opt-in 的 TMDB 线上 smoke

pytest 始终离线，不会收集或调用线上检查。需要核对真实 TMDB API 行为时，
显式执行：

```bash
PYTHONPATH=src .venv/bin/python scripts/tmdb_live_smoke.py --live
```

脚本优先使用进程环境中的 `TMDB_API_KEY`，不存在时才 no-follow 读取仓库根目录
固定 `.env` 中的同名单键。它不执行 dotenv 变量展开，不接受自定义文件或 URL，
也不会输出凭据和 TMDB 文本。脚本以固定查询和已知 ID 检查 Anime/TV/Movie
search、TV details、Season details，以及 adult 内容在显式关闭时隐藏、启用时
可搜索和 Movie metadata 的 `adult=true`。生产 adapter 与 Agent 搜索默认启用
adult；false 请求仅用于 live 对照验证。缺少 `--live` 或两处都没有凭据时，会
在发起网络请求前退出。

TMDB 将 adult 搜索建模为请求参数，不是 API key 的独立权限字段。脚本报告的
`adult_capability=available` 表示当前 key 的真实请求可以完成上述完整检查。

开发过程中的 Agent 概念说明见
[开发学习日志](development-journal.md)，安全边界见
[威胁模型](threat-model.md)。

## 核心架构

```text
Agent Runtime
  决定下一步调查什么、调用哪个受限工具、何时停止
        |
        v
Deterministic Safety Kernel
  强制 schema、ID、集数、路径、碰撞和计划不变量
        |
        v
Immutable RenamePlan + plan_hash
        |
   独立人工批准
        |
        v
Isolated Executor
  无 LLM、无外网，只执行已批准的精确计划
```

## 非协商原则

- 默认只生成计划，不修改文件。
- Agent 不接受或生成可执行路径、shell 命令和任意 URL。
- Agent 不能直接删除、覆盖、移动或改写文件。
- LLM 负责不确定的语义判断；代码负责所有安全约束。
- 所有对用户媒体和输出树的副作用必须绑定不可变 `plan_hash` 和一次性审批。
- 除显式 `--live` smoke 对仓库根 `.env` 的受限单键只读外，不读取、修改或访问
  任何 `.env*` 文件。
- 测试默认离线，TMDB、模型和文件系统适配器都必须可替换。

## 计划中的技术方向

- Python 3.11+
- OpenAI Agents SDK 作为 Agent runtime
- Pydantic 类型化工具输入、输出和领域模型
- pytest 离线测试
- 单 Agent 起步；证明需要后才引入 specialist
- append-only run events、checkpoint、trace 和离线 eval
