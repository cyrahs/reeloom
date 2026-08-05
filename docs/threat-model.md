# Reeloom 威胁模型

版本：M13

日期：2026-08-04

## 1. 安全目标

Reeloom 的核心安全目标不是“让模型尽量谨慎”，而是确保模型、用户文本和外部
数据即使恶意或错误，也不能越过确定性边界：

1. 未批准时不修改文件系统。
2. 执行时只移动精确计划中已批准的源到目标，永不删除、覆盖或临时改写目标。
3. Agent 不能选择源路径、目标路径、授权根或网络目的地。
4. 任意不确定、状态漂移、竞态、校验失败都 fail closed。
5. `.env*` 永远不进入扫描、工具 observation、trace 或执行范围；仅显式 live
   smoke 可 no-follow 读取固定 `.env` 中 allowlist 的 TMDB/OpenAI 配置项。

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
| 目录逃逸 | symlink 指向授权根之外 | scanner/executor 使用 containment 与 no-follow | scanner M2、Executor preflight M6.2 已覆盖 |
| TOCTOU | 批准后源被替换或目标出现 | final preflight 与每步重检；atomic no-replace rename；post-rename identity | M6 已覆盖 |
| Plan 篡改 | preview 后 destination 被修改 | canonical bytes 与 `plan_hash` | M5 已覆盖 |
| 审批重放 | 重复执行同一批准 | canonical binding、expiry 与 no-replace 原子 one-time claim | M6.1 已覆盖 |
| 已有目标或部分失败 | destination 已存在或部分 rename | rollback manifest 先写；`RENAME_NOREPLACE`；append-only events；逆序 rollback/recovery | M6 已覆盖 |
| 执行/恢复竞争 | live apply 期间另一进程启动 recover | transaction-scoped process lease；竞争立即 fail closed | M6 已覆盖 |
| Journal 终态不确定 | completed 已写出但目录 fsync 报错 | terminal commit 后不自动 rollback；双终态拒绝；恢复复核文件 identity | M6 已覆盖 |
| 任意网络访问 | 模型请求任意 URL | 只有固定 host/path 的 TMDB business adapter 与显式 OpenAI model adapter 可联网；tool schema 不接受 URL，模型没有 hosted/MCP/shell tool | M3、M7 已覆盖 |
| 伪造 TMDB capability | 模型直接选择猜测的 TMDB ID | 查询与选择只允许本 run 搜索 observation 已记录的候选 ID | M3 已覆盖 |
| TMDB 类型混淆 | Movie 100 被当作 TV 100，或 anime 搜索回退到真人剧 | run 显式绑定 work_type；capability 使用 `(work_type, id)`；anime 要求 genre 16 且 details 再验证 | M3 已覆盖 |
| 归档根越权 | 模型尝试借 work type 或文本选择另一个 dst | run revision 直接绑定 watch 的 source/library root；work type 不参与选路，目标不从模型文本推导 | filter M3；watch binding M5 |
| TMDB 响应注入 | title/overview 包含指令、控制字符或超长文本 | adapter 转换为有限领域字段，控制字符中和，tool observation 再限长 | M3 已覆盖 |
| 网络资源耗尽 | 慢响应、超大 body、过量结果或缓存增长 | HTTP timeout、streaming body 上限、结果/文本/observation 上限、TTL/LRU cache | M3 已覆盖 |
| 资源耗尽 | 超大分页或工具/模型重试循环 | scan/page/display/tool/turn/failure budgets | M1-M2 已覆盖；token/time M4 |
| 状态伪造 | assistant 文本宣称任务完成 | 只有 typed domain event 能转换 phase | M1 已覆盖 |
| 敏感信息泄漏 | `.env` 或字幕内容进入 trace | scanner/Agent/executor 拒绝 `.env*`；live smoke 仅 no-follow 读取固定 allowlist；限量 observation、trace 脱敏 | scanner M2、smoke M3/M13；trace M7 |
| TMDB 凭据泄漏 | 凭据进入模型输入、缓存 key 或错误链 | 凭据仅注入 HTTP adapter；live loader 有文件/大小/单键限制；不进入 observation/cache key/repr，网络异常去除 cause | M3 已覆盖 |
| Checkpoint 篡改 | 替换 event/session record、暴露半写记录或制造 sequence gap | 授权 root、no-follow、匿名 inode 完整写入后 no-replace 原子发布、canonical schema、run binding、连续 sequence 与 digest chain；append 前完整重验和 reducer replay | M7 已覆盖 |
| 对话记录越权 | SDK session 文本伪造领域成功或批准 | session 与 domain event store 分离；只有 reducer event 能改变 phase，Executor 仍只接受 plan hash 与 approval ID | M7 已覆盖 |
| Trace 泄漏 | Plan、prompt、tool observation 或未知字符串进入观测系统 | trace 从 event replay 生成显式 allowlist projection；路径、标题、正文、prompt 和未知 token 不复制 | M7 已覆盖 |
| OpenAI 配置劫持 | base URL、custom header/body/query 改写模型请求 | adapter 只接受显式 HTTPS base URL，拒绝 URL 凭据/query/fragment 和环境 custom headers；live script 仅在 `--live` 后 no-follow 读取固定 `.env` allowlist；SDK trace 和 response store 关闭 | M7/M13 已覆盖 |

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
