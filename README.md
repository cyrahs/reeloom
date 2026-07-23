# Reeloom

Reeloom 是一个从零设计的 **agent-native 动画剧集整理器**。它与
`aninamer` 追求相同的用户结果：识别动画剧集和外置中文字幕，结合
TMDB 元数据生成安全的重命名计划，并在用户批准后执行。

这个仓库不是 `aninamer` 的迁移分支。旧项目只作为需求说明、领域规则
参考和 golden test oracle；Reeloom 不在运行时依赖旧项目。

## 当前状态

**M4 / 剧集与字幕映射（已完成）**

M0-M3 的领域契约、真实 SDK tool loop、安全候选快照和受控 TMDB 识别之上，
已经建立“模型做 mapping、代码做 enforcement”的反馈闭环：

- Agent 可调用 `get_existing_inventory`、`detect_subtitle_variant` 和
  `submit_mapping`，但工具只接受 run-scoped ID 和 strict schema；
- TMDB season observation 建立 episode boundary，纯 kernel 强制检查边界、
  multi-episode overlap、重复/未知 ID、字幕归属和已有库存冲突；
- 字幕 adapter 只 no-follow 读取 snapshot 内文件的 64 KiB 前缀，并复核扫描时
  identity 与摘要；Agent 只看到 `.chs/.cht/.chi`，看不到正文或路径；
- validation failure 返回有界结构化 issue，并保持 `MAP_EPISODES`；模型修正后
  重新提交；
- 只有 `MappingSubmitted` 领域事件能进入 `BUILD_PLAN`，普通 assistant 文本
  不能改变成功状态；
- run 同时限制模型轮数、工具数、失败数、累计 token 和 wall-clock time；
- 真实 Agents SDK Runner + scripted model 的离线集成测试覆盖首次错误、读取
  observation 后修正并成功提交。

adapter 只接受调用方显式注入的凭据，不加载配置文件。自动化测试不访问真实
网络，也没有任何移动、重命名或删除能力。

M3 已支持 movie 搜索和类型化候选，但 `select_series` 刻意只允许
`anime/tv_series`。电影选择、单文件 mapping 和电影命名必须先建立独立领域
契约，不能伪装成 `SxxExx` 流程。

详细路线见 [初步实施计划](docs/initial-plan.md)，M0 验收结论见
[M0 Definition of Done](docs/m0-review.md)，M1 验收结论见
[M1 Definition of Done](docs/m1-review.md)，M2 验收结论见
[M2 Definition of Done](docs/m2-review.md)，M3 验收结论见
[M3 Definition of Done](docs/m3-review.md)，M4 验收结论见
[M4 Definition of Done](docs/m4-review.md)。

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
