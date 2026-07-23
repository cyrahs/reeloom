# Reeloom

Reeloom 是一个从零设计的 **agent-native 动画剧集整理器**。它与
`aninamer` 追求相同的用户结果：识别动画剧集和外置中文字幕，结合
TMDB 元数据生成安全的重命名计划，并在用户批准后执行。

这个仓库不是 `aninamer` 的迁移分支。旧项目只作为需求说明、领域规则
参考和 golden test oracle；Reeloom 不在运行时依赖旧项目。

## 当前状态

**M3 / 受控 TMDB 识别（已完成）**

M0-M2 的领域契约、真实 SDK tool loop 和安全候选快照之上，已经建立受控
TMDB 识别闭环：

- provider-neutral TMDB port 可以由真实 HTTP adapter 或离线 fake 实现；
- HTTP adapter 固定访问 TMDB API v3，不接受模型提供的 URL，并限制超时、响应体
  和缓存；
- TMDB 的 `media_type`（`tv/movie`）与本地 `work_type`
  （`anime/tv_series/movie`）分离；每个 run 必须显式绑定一个可信
  `work_type`；
- `search_tmdb`、`get_tmdb_series`、`get_tmdb_season` 和 `select_series` 都经过
  strict schema、phase、capability、预算和 observation 大小校验；
- 搜索 filter 必须匹配 run 的 `work_type`；搜索到的
  `(work_type, tmdb_id)` 才成为候选 capability；
- `anime` 使用 TV search 并严格保留 TMDB Animation genre 16，`movie` 使用
  Movie search；结果同时返回 `work_type` 与 `media_type`；
- Movie metadata adapter 返回有界标题、年份、语言、genre IDs 和严格布尔
  `adult` 标记；该能力尚未暴露为 Agent 工具；
- Agent 的 `search_tmdb` 不暴露 adult 开关，执行端固定启用
  `include_adult=true`；
- `SeriesSelected` typed event 是进入 `MAP_EPISODES` 的唯一成功路径；
- zh-CN 名称优先，年份必填；TMDB season episode 会提取有限的 OVA/OAD hint；
- 真实 Agents SDK Runner + scripted model + fake TMDB 的完整识别循环离线通过。
- HTTP parsing 还会回放从 TMDB 官方 OpenAPI 示例投影出的 TV search、Movie
  search、TV details 和 Season details 契约 fixture。

adapter 只接受调用方显式注入的凭据，不加载配置文件。自动化测试不访问真实
网络，也没有任何移动、重命名或删除能力。

M3 已支持 movie 搜索和类型化候选，但 `select_series` 刻意只允许
`anime/tv_series`。电影选择、单文件 mapping 和电影命名必须先建立独立领域
契约，不能伪装成 `SxxExx` 流程。

详细路线见 [初步实施计划](docs/initial-plan.md)，M0 验收结论见
[M0 Definition of Done](docs/m0-review.md)，M1 验收结论见
[M1 Definition of Done](docs/m1-review.md)，M2 验收结论见
[M2 Definition of Done](docs/m2-review.md)，M3 验收结论见
[M3 Definition of Done](docs/m3-review.md)。

## 本地验证

```bash
.venv/bin/python -m pytest -q
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
[开发学习日志](docs/development-journal.md)，安全边界见
[威胁模型](docs/threat-model.md)。

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
