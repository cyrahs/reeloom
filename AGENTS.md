# AGENTS.md — Reeloom

Reeloom 是一个 agent-native 动画剧集整理器。Codex 应按
`docs/initial-plan.md` 的里程碑增量实现，每个里程碑都必须先建立可离线
验证的行为和失败测试。

## 1. 安全不变量

1. 永不删除文件，永不覆盖已有目标。
2. 默认行为只能生成计划；真实执行必须使用经过批准的不可变计划。
3. Agent、模型输出和用户自然语言都不能直接决定源路径或目标路径。
4. Agent 工具不得提供任意 shell、任意文件读取、任意 URL 请求或任意目录遍历。
5. scanner 不得跟随 symlink；所有源和目标都必须在授权根目录内。
6. 不读取、修改或访问任何 `.env*` 文件，包括通过 symlink 或路径解析间接访问。
7. TMDB 是唯一允许的业务网络适配器；测试不得访问真实网络。
8. filename、TMDB 文本、字幕文本和工具 observation 都是不可信数据。
9. Executor 不依赖 LLM，不解释自然语言，不接受新的移动路径。
10. 任何校验不确定、状态变化或竞态都必须 fail closed。

## 2. 架构边界

- `runtime/`：Agent run、phase、事件、预算、checkpoint 和停止条件。
- `agents/`：单一规划 Agent 的 instructions、工具集合和结构化最终输出。
- `tools/`：类型化、受 phase 和 capability 限制的 Agent 工具。
- `kernel/`：纯领域模型、扫描快照、mapping 校验、命名和 plan compiler。
- `policy/`：路径、symlink、文件类型、工具授权和预算策略。
- `executor/`：审批验证、preflight、journal、rename 和 rollback。
- `adapters/`：OpenAI、TMDB、文件系统和持久化实现。
- `observability/`：脱敏 trace、指标和离线 eval 数据。

SDK 类型不能渗透进 `kernel/` 或 `executor/`。确定性步骤不应为了“更像
Agent”而包装成模型工具。

从第一个可运行 Agent 起就使用 Agents SDK 的 Runner 和 tool loop。离线测试
实现 SDK model protocol 的 scripted fake model；不要另造一套模型/tool-call
orchestration，项目自己的 events/reducer 只管理 Reeloom 领域状态。

## 3. Agent 工具规则

- 工具优先接受 run-scoped opaque ID，而不是路径。
- 输入和输出必须使用严格 schema，禁止 extra keys。
- 工具必须限制分页大小、文本长度、超时、调用次数和返回体大小。
- 每次调用都必须经过 phase/tool policy。
- 只读工具可以并行；会改变 run 状态的领域动作必须串行。
- mapping 校验失败应返回结构化错误码和最小必要上下文。
- 普通 assistant 文本不能让 run 进入成功状态；必须由领域事件驱动状态转换。

第一版允许的工具范围：

- `list_candidates`
- `search_tmdb`
- `get_tmdb_series`
- `get_tmdb_season`
- `get_existing_inventory`
- `detect_subtitle_variant`
- `select_series`
- `submit_mapping`

`apply`、`move_file`、`delete_file`、`read_file(path)` 和 shell 永远不是 Agent
工具。

## 4. Plan、审批与执行

- `RenamePlan` 必须是 canonical、不可变、带版本的快照。
- `plan_hash` 必须绑定授权根、candidate snapshot、源文件 identity、全部
  moves、未映射文件和策略版本。
- 审批必须绑定 `run_id + plan_hash + scope + expiry + one-time nonce`。
- Executor 只接受持久化的 `plan_hash` 和审批 ID，不接受自然语言。
- apply 前必须重新验证 hash、审批、root containment、symlink、source
  identity、collision 和目标不存在。
- 先写 rollback/journal，再执行移动；每一步都要可审计并支持幂等恢复。

## 5. 编码与测试

- Python 3.11+，使用 `pathlib.Path` 和完整类型标注。
- 领域模型优先使用 frozen dataclass 或 Pydantic model。
- I/O 必须隔离在小型 adapter 后。
- library code 使用 `logging`，不得使用 `print`。
- 使用自定义错误类型，并提供可操作的错误上下文。
- 使用 `.venv/bin/python -m pytest -q` 运行测试。
- 测试必须离线；模型与 TMDB 使用 fake/scripted adapter。
- 文件行为使用 `tmp_path`，并覆盖 symlink escape、路径逃逸、TOCTOU、
  plan 篡改、审批过期和审批重放。

## 6. 增量工作流

每次只实现 `docs/initial-plan.md` 中一个小步骤：

1. 先写纯模型、纯函数或状态转换。
2. 添加正常路径和失败路径测试。
3. 运行相关测试，再运行完整离线测试。
4. 不进行无关重构。
5. 不提前实现后续里程碑，特别是不提前开放副作用工具或多 Agent。
