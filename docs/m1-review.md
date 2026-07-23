# M1 Definition of Done

审查日期：2026-07-23

结论：M1 最小 Agents SDK Run 已完成，可以进入 M2。

## 交付检查

| 要求 | 实现 | 结果 |
| --- | --- | --- |
| RunState 与 phase | 固定 phase、status、pending calls、stop reason | 通过 |
| typed events 与 reducer | frozen events、纯 reducer、非法序列 fail closed | 通过 |
| append-only event store | reduce 成功后追加、稳定 sequence、deterministic replay | 通过 |
| 单 Agent + Runner | `EpisodeOrganizerAgent` 使用 Agents SDK `Runner.run` | 通过 |
| scripted fake model | 实现 SDK `Model` protocol，Responses 形状输出 | 通过 |
| SDK function tool | `function_tool` + execution-side input guardrail | 通过 |
| phase policy | deny by default，`list_candidates` 仅 identification phase | 通过 |
| 总预算 | model turns、tool calls、repeated failures | 通过 |
| 离线执行 | tracing disabled，无 API key、无网络、无文件副作用 | 通过 |

## 负向测试检查

- 未知工具返回有限、结构化 observation；
- phase 不允许的已注册工具不进入领域实现；
- extra key、错误类型、越界值和非法 JSON 被执行端 guardrail 拒绝；
- 可重试错误返回 observation，致命错误停止领域 run；
- 重复失败、工具调用和 SDK 最大轮数触发预算停止；
- tool result 必须匹配 pending request，pending call 存在时不能停止；
- assistant 最终文本只停止 SDK run，不会把业务 phase 改为 `COMPLETED`；
- 相同 scripted transcript 产生相同 event transcript 和最终 state。

## 架构边界检查

- SDK model/tool loop 只存在于 Agents SDK，项目没有复制 orchestration loop。
- `runtime/` 不导入 Agents SDK；SDK 类型只停留在 `agents/` 集成层。
- `kernel/` 与 `executor/` 未引入 SDK 类型。
- M1 工具只返回 snapshot 中的 opaque ID，不接受路径、URL 或 shell。
- 当前 snapshot 是测试注入的只读对象；没有提前实现 M2 scanner。
- 未提供 apply、move、delete、任意 read 或任意 request 工具。

## 刻意留给后续里程碑

- M2：安全 scanner、真实 candidate snapshot、分页与文件/父目录 symlink 防护。
- M3：受控 TMDB HTTP adapter、search/detail tools 与 `select_series`。
- M4：mapping feedback loop，以及 token、时间和 observation body 预算。
- M5/M6：canonical `RenamePlan`、审批、preflight、journal、rename 与 rollback。
