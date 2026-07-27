# ADR 0003：M10 Movie 领域边界

状态：Accepted

日期：2026-07-26

## 决策

Movie 使用独立的 `MovieIdentity`、`MovieMappingDraft`、`MovieRenamePlan v1`
和 `MovieAmendmentPlan v1`，不伪装成 Episode，也不引入通用媒体抽象。
每个 run 只选择一个正片视频和零到多个已检测中文变体的字幕；其余候选保持
unmapped。缺少可靠上映年份时停止规划。

Movie Agent 仍使用一个 Agents SDK Runner/tool loop，只开放候选、TMDB Movie、
字幕检测和完整 mapping 工具。模型不接收路径能力；目标路径完全由确定性 kernel
从 identity、源扩展名和字幕变体推导。

初始 Movie 计划要求整个 canonical 目录不存在。amendment 只覆盖当前 durable
completed layout，并同时绑定 parent hash 与 completed transaction；执行前再次
核对当前 completed head。Episode canonical bytes、hash 和三层 manifest 规则不变。

## 后果

- TV 与 Movie 的同号 TMDB ID 不能互换 capability。
- Movie manifest 只允许两层相对路径；Episode 继续要求 `Sxx` 三层路径。
- automatic policy 可以移动选中的正片并保留额外视频。
- identity correction 可能留下旧的空目录；系统不删除目录或媒体。
- 新出现的文件不进入既有 completed-layout reapply。
