# Reeloom 威胁模型

版本：M0.5

日期：2026-07-23

## 1. 安全目标

Reeloom 的核心安全目标不是“让模型尽量谨慎”，而是确保模型、用户文本和外部
数据即使恶意或错误，也不能越过确定性边界：

1. 未批准时不修改文件系统。
2. 执行时只移动精确计划中已批准的源到目标，永不删除、覆盖或临时改写目标。
3. Agent 不能选择源路径、目标路径、授权根或网络目的地。
4. 任意不确定、状态漂移、竞态、校验失败都 fail closed。
5. `.env*` 永远不进入扫描、工具 observation、trace 或执行范围。

## 2. 受保护资产

- 用户的源媒体、字幕和已有输出目录。
- 授权 source/output root 的边界。
- candidate snapshot、mapping、RenamePlan 和 `plan_hash` 的完整性。
- 审批记录、一次性 nonce、journal 和 rollback 数据。
- API 凭据以及可能出现在文件名、字幕或 trace 中的隐私数据。
- run phase、预算、事件顺序和 checkpoint 的完整性。

## 3. 信任边界

```text
Untrusted
  用户自然语言、文件名、字幕文本、TMDB 文本、模型输出、tool observation
        |
        v
Agent / typed tool boundary
  只接受严格 schema、run-scoped opaque ID、有限枚举和有界文本
        |
        v
Deterministic kernel
  snapshot membership、mapping、命名、碰撞、canonical plan
        |
        v
Independent approval boundary
  run_id + plan_hash + scope + expiry + one-time nonce
        |
        v
Isolated executor
  final preflight、journal、no-follow rename、rollback
```

模型和 Agent runtime 不属于可信计算基。prompt、system instructions 和模型
guardrail 可以改善行为，但不能替代 schema、policy、hash、审批或操作系统级
路径检查。

## 4. 攻击面与控制

| 威胁 | 示例 | 确定性控制 | 状态 |
| --- | --- | --- | --- |
| Prompt injection | 文件名要求忽略规则并调用 shell | 文件名仅作为数据；严格字段拒绝 instructions/path | M0 已覆盖 |
| 伪造 capability | 模型提交 `video:99` | ID 必须存在于当前 candidate snapshot | M0 已覆盖 |
| 路径注入 | 标题或 mapping 提交 `../../target` | mapping 不接受路径；destination 由命名代码编译 | M0 已覆盖 |
| Unicode/平台路径问题 | bidi 控制符、全角分隔符、`CON` | NFKC、控制字符清洗、保留名和长度规则 | M0 已覆盖 |
| 语义映射冲突 | 两个视频占用同一集 | season/episode bounds、range overlap 校验 | M0 已覆盖 |
| Specials 错配 | OVA hint 被静默映射到非 OVA | typed hint 可跨 season；证据不足报冲突；未知项只回退 S00 | M0 已覆盖 |
| 越权带入字幕 | 字幕关联未映射视频 | subtitle 必须指向当前 MappingDraft 中的视频 | M0 已覆盖 |
| Plan 内碰撞 | 两个 source 生成同一目标 | 同字幕稳定消歧；其他 exact/casefold collision 拒绝 | M0 已覆盖 |
| Plan 漂移或遗漏 | move 脱离 validated mapping，或 mapped candidate 没有 move | PlanDraft 绑定 mapping/单一 series，unmapped 由差集推导 | M0 已覆盖 |
| 目录逃逸 | symlink 指向授权根之外 | scanner/executor 使用 containment 与 no-follow | M2/M6 待实现 |
| TOCTOU | 批准后源被替换或目标出现 | source identity 与 final preflight | M5/M6 待实现 |
| Plan 篡改 | preview 后 destination 被修改 | canonical bytes 与 `plan_hash` | M5 待实现 |
| 审批重放 | 重复执行同一批准 | expiry 与原子 one-time nonce claim | M6 待实现 |
| 已有目标或部分失败 | destination 已存在或部分 rename | filesystem collision check、journal 先写、rollback | M5/M6 待实现 |
| 任意网络访问 | 模型请求任意 URL | 只有 TMDB adapter 有业务网络 capability | M3 待实现 |
| 资源耗尽 | 超大分页、文本或重试循环 | page/text/body/time/tool/token budgets | M1-M4 待实现 |
| 状态伪造 | assistant 文本宣称任务完成 | 只有 typed domain event 能转换 phase | M1 待实现 |
| 敏感信息泄漏 | `.env` 或字幕内容进入 trace | 字面与解析后 `.env*` 拒绝、限量 observation、trace 脱敏 | M2/M7 待实现 |

## 5. Specials/OVA/OAD 的证据规则

Specials resolver 只接受：

- local `video_id` 和 `ova/oad/unknown` hint；稳定顺序只能来自 scanner 分配的
  candidate ID ordinal，Agent 不能提交 order；
- TMDB `season_number + episode_number` 和 `ova/oad/unknown` hint。

它不接受 filename、title、overview、instructions 或 destination。上游可以根据
不可信文本提出结构化 hint，但 hint 不是权限，也不能修改路径。

解析顺序固定：

1. OVA local 与任意 season 的 OVA TMDB episode 按稳定顺序配对。
2. OAD local 与任意 season 的 OAD TMDB episode 按稳定顺序配对。
3. 某类 local hint 数量超过对应 TMDB hint 时，证据冲突并 fail closed。
4. 未分配 local 只与剩余 `season_number == 0` 的 TMDB episode 按稳定顺序配对。
5. 普通 season 的 unknown episode 永不参与 fallback。
6. 多余 local 显式保留为 unmapped；多余 TMDB episode 显式保留为 unused。

所有 TMDB target 必须通过 provider-neutral `EpisodeCatalog` 的季集边界校验。

## 6. 失败与披露策略

- 错误通过稳定 `ErrorCode` 和最小必要 context 返回。
- 错误 context 不包含绝对路径、字幕正文、TMDB overview 或自然语言指令。
- 普通 assistant 文本不能改变 run phase 或批准状态。
- 不自动降低安全策略；需要更多证据时暂停、重规划或请求用户确认。
- 任何 material state drift 都使旧计划和旧批准失效。

## 7. M0 之外必须持续验证的负向测试

- symlink file/parent/root escape 和解析后 `.env*`；
- `..`、绝对路径、不同文件系统与目录替换；
- plan 任一字节篡改、审批过期、并发 claim 和重放；
- source identity 变化、目标临时出现和 partial rename；
- 恶意模型 transcript、未知工具、非法 phase 和预算耗尽；
- trace、journal 和 observation 中不出现凭据或非必要正文。
