# Reeloom

Reeloom 是一个从零设计的 **agent-native 动画剧集整理器**。它与
`aninamer` 追求相同的用户结果：识别动画剧集和外置中文字幕，结合
TMDB 元数据生成安全的重命名计划，并在用户批准后执行。

这个仓库不是 `aninamer` 的迁移分支。旧项目只作为需求说明、领域规则
参考和 golden test oracle；Reeloom 不在运行时依赖旧项目。

## 当前状态

**M0 / 领域契约与威胁模型（已完成）**

已经建立最小 Python 工程和第一批纯领域契约：

- immutable `CandidateId`、`Candidate` 和 `CandidateSnapshot`；
- `video:N` / `subtitle:N` opaque ID 的 canonical 校验；
- extra keys、ID/kind 不一致和重复 ID 的结构化错误；
- immutable episode mapping、provider-neutral 集数边界和 range overlap 校验；
- mapping ID 必须来自当前 snapshot，字幕只能关联已映射视频；
- 完整的 series/Sxx/单集/多集/字幕命名契约和跨平台路径组件清洗；
- destination 与 episode title 不属于命名输入，扩展名必须来自类型白名单；
- OVA/OAD hint 可跨 season 优先匹配、无 hint 只在 S00 稳定回退的 resolver；
- mapping-bound immutable plan draft、自动 candidate 分区、稳定字幕消歧和跨平台
  碰撞校验；
- 明确区分已实现与待实现控制的仓库威胁模型；
- 对正常输入、失败输入和 prompt-injection 风格文件名的离线测试。

尚未接入模型、Agents SDK、TMDB、文件扫描或真实文件操作。

详细路线见 [初步实施计划](docs/initial-plan.md)，M0 验收结论见
[M0 Definition of Done](docs/m0-review.md)。

## 本地验证

```bash
.venv/bin/python -m pytest -q
```

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
- 不读取、修改或访问任何 `.env` 文件。
- 测试默认离线，TMDB、模型和文件系统适配器都必须可替换。

## 计划中的技术方向

- Python 3.11+
- OpenAI Agents SDK 作为 Agent runtime
- Pydantic 类型化工具输入、输出和领域模型
- pytest 离线测试
- 单 Agent 起步；证明需要后才引入 specialist
- append-only run events、checkpoint、trace 和离线 eval
