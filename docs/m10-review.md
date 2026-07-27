# M10 实现评审

状态：Complete

日期：2026-07-27

## 结论

M10 已通过独立 Movie 领域类型和 canonical plan family 打通现有同源 Web UI
闭环，没有改变 Episode plan bytes/hash，也没有引入通用媒体框架。Movie Agent
只能选择已观察的 Movie capability 并提交单正片 mapping；路径和执行继续完全
由 deterministic kernel、exact approval 与无 LLM Executor 控制。

## 验收证据

- offline pytest：Movie domain、plan tamper、runtime codec、scripted SDK Agent、
  executor apply/reapply 与全部 M0-M9 回归。
- PostgreSQL：真实 application builder 的 Movie automatic journey 移动正片、
  保留额外视频；completed-layout reapply 为 no-op，后来新增文件保持不变。
- Web：movie config enum 与统一中文标签通过 lint、typecheck、Vitest、production
  build；现有 generic preview/interaction/approval/recovery 页面无需 Movie 分叉。
- Playwright：真实 API/static server + PostgreSQL 在 Chromium、Firefox、WebKit
  共 12 个 project tests 通过；Movie case 覆盖 exact relative preview、
  unmapped extra 和 completed reapply no-op。

验收结果：offline pytest `507 passed, 1 skipped`；PostgreSQL
`20 passed, 1 skipped`；Vitest `12 passed`；Playwright `12 passed`。

本机临时 PostgreSQL 未安装 production `reeloom_app` role，因此 role privilege
专属 case 跳过；其余 PostgreSQL tests 全部通过。生产 compose 初始化该 role。
