# M13 实施计划：动漫字幕探测与自动获取

状态：M13.0-M13.4 complete；Agent loop hardening complete

日期：2026-08-03

## 1. 固定流程

```text
Anime run 没有任何外置字幕候选
→ Agent 每季选择一个 video:N 调用 check_sub_from_video
→ 有明确中文内嵌字幕：继续现有 mapping
→ 明确没有中文内嵌字幕：search_sub
→ Agent 选择一个 run-scoped subarchive:N
→ deterministic planner 检查并编译 SubtitleAcquisitionPlan v1
→ exact subtitle_acquire approval
→ isolated executor 发布固定目录
→ 旧 run superseded，durable outbox 建立唯一 fresh run
```

探测不确定、论坛阻断、证据不足、内容漂移或任一执行校验不确定时停止并进入
attention；不得把不确定当作无字幕。

## 2. Agent-facing contract

- `check_sub_from_video(video_id, season_number)`：只接受当前 snapshot 的单个
  `video:N`。同一季只能占用一个 advisory probe slot；季号不参与 mapping 或路径。
- `search_sub(season_number, cursor)`：不接受 query、URL、fid 或裸页码。查询由已选
  TMDB identity 的最多三个别名确定，只访问 fid 37/46。
- `select_subtitle_release(selections)`：Agent 必须显式选择完整 archive set；即使
  只有一个候选也不自动选择。证据不足则提交固定 reason code 的 attention。

搜索 observation 只含 bounded、去 URL 的标题、512-byte 摘要、coverage/language/
release-group hint 和 opaque ID。帖子 HTML、完整正文、动态 attachment URL、字幕
文本及任意路径都不进入模型或 trace。

## 3. 固定网络和归档策略

- 唯一 origin：`https://bbs.acgrip.com`；固定公开 search/thread/native attachment
  endpoints；`trust_env=false`，禁止通用/自动 redirect、proxy、自定义 base URL、
  Cookie 登录、验证码/Cloudflare 绕过和站外链接。只保留站点当次签发且名称、域、
  path、Secure 属性和值长度均受限的匿名 Discuz 会话 Cookie；搜索 POST 只手工解析
  一次经结构化校验的精确同源结果跳转。
- 每 run 最多三个标题别名、20 个 HTTP response、8 MiB HTML、50 个 release、
  100 个 attachment；每页 1 MiB、每 tool 30 秒、每 request 5 秒、并发 1、全局
  至少一秒请求间隔；论坛搜索 POST 另设五秒冷却，不依赖中间 GET 或网络延迟。
- 动态 `aid` URL 永不持久化。capability/plan 只绑定 thread ID、post ID 和数字
  attachment ID；planning/apply 都重新解析当次签名链接。
- 允许 ZIP、7z、RAR 和完整多卷 RAR。多卷必须来自同一 post、序列连续、header
  确认，最多八卷；分卷 7z、缺卷和跨 post 拼卷拒绝。
- 单卷 16 MiB、总压缩数据 64 MiB、最多 256 条目、单字幕 32 MiB、总字幕
  128 MiB、压缩比 100。只发布 `.ass/.srt/.ssa/.sup/.vtt`。
- 加密、嵌套归档、symlink/hardlink/device、绝对/父级/Windows 路径、`.env*`、
  Unicode/casefold 冲突和超限均 fail closed。

## 4. Plan、审批和发布

`SubtitleAcquisitionPlan v1` 是独立 plan family，不属于 RenamePlan union。hash
绑定 run/config、authorized root、folder identity、candidate snapshot、TMDB
identity、provider/parser/policy 版本、每个 archive volume 和 subtitle member 的
identity/size/SHA-256、manifest digest、确定性目标名、拒绝记录与资源限制。

M13.3 planner 在媒体根之外受限检查选中 archive set 后才能生成计划。M13.4
executor 使用独立 `SUBTITLE_ACQUIRE` approval scope；automatic 只能由 immutable
config revision 在 plan durable 后签发一次性 exact approval，Agent 不能控制。

发布目录固定派生为 `reeloom-acquired-<full plan hash>`。executor 先写 journal，
在 source folder 下建立 scanner 明确忽略的 sibling staging，应用自行以
`O_EXCL|O_NOFOLLOW` 写入计划叶文件，完成 file/dir fsync 后使用 native
no-replace directory rename。文件系统不支持原生 primitive 时 feature unavailable，
不使用 FUSE checked-rename fallback。

发布结算通过 durable outbox 幂等注册 successor。`acquisition_plan_hash` 和 lineage
unique key 防止重复 successor 与自动获取循环；旧 run 永不 rescan 或复用 snapshot。

## 5. 增量步骤与验收

### M13.0：纯契约（完成）

- strict frozen probe/search/selection/archive schema；
- provider-neutral inspector/search/plan-store ports；
- canonical acquisition plan、固定 limits、目标名 compiler 和 round-trip verifier；
- 正常、冲突、越界、URL、hash 与语义篡改离线测试；
- 不注册工具、联网、调用外部程序或改媒体文件。

### M13.1：视频探测（完成）

- 固定版本 ffprobe adapter、no-follow fd、identity before/after、timeout/output cap；
- anime-only tool、phase/capability、每季一个 probe record、event/reducer/codec；
- 覆盖中文/非中文/无字幕/unsupported/indeterminate、symlink 和 TOCTOU。
- 部署固定为 Debian Trixie `ffmpeg=7:7.1.5-0+deb13u1`；固定
  `prlimit → /usr/bin/ffprobe` argv 约束 CPU、地址空间、输出和 wall-clock，
  不使用 shell、可配置 executable 或多线程环境不安全的 `preexec_fn`。
- anime-only AgentDefinition 升至 v5；旧 anime episode v4 的未规划 run 安全
  终止并重建，TV 继续使用 v4，Movie 与二者工具集均不变。runtime projection
  升至 v6 并兼容读取 v1-v5。

### M13.2：论坛搜索与 Agent 选择（完成）

- 固定 origin、`trust_env=false`、无通用 redirect/登录 Cookie 的 Discuz parser/provider；
  支持有界匿名会话 Cookie 与一次手工验证的同源搜索结果跳转；
  search POST 固定 `fid 37/46`，只接受受限 search/thread path；
- synthetic Discuz fixtures 与 `httpx.MockTransport` 覆盖搜索分页、帖子回复页、
  多版本、多卷 grouping、动态 aid、挑战/登录、外链、20-response budget、限流与
  parser drift；所有 pytest 均离线；
- `search_sub` 只在季度样本明确 `chinese_status=absent` 时开放，observation 只含
  bounded 去 URL 证据和 opaque ID；stable capability 仅持久化数字
  `thread/post/attachment` identity；
- 每次成功的 provider 调用额外持久化有界、去 URL 的搜索诊断：实际使用的
  规范化标题别名及其命中帖子数，去重帖子、已读帖子/页面、楼层、原生附件、
  可选归档组与 release 数，并派生空结果停留阶段；这些诊断不包含原始 HTML、
  帖子正文、URL 或动态附件地址，也不进入 Agent observation；
- `select_subtitle_release` 是唯一接受事件；一个候选仍需 Agent 显式选择，证据不足
  只能提交固定 reason code 的 `needs_attention`；
- runtime event/state projection 升至 v7 并兼容 v1-v6；Anime AgentDefinition 升至
  v6，旧 Anime v5 未规划 run 安全终止并重建，TV v4/Movie v4 工具集不变；
- scripted fake model 已验证 `probe → search → explicit selection`，选择后只进入
  `build_subtitle_acquisition_plan`，本步骤不下载、解压、签发审批或写媒体目录。

生产环境的 ACG.RIP provider 仍保持未实例化；必须等后续显式
`acgrip.enabled=true` 配置与 acquisition policy 落地后才接入 worker，避免升级即
产生新的外部网络访问。

### M13.3：归档检查与计划编译（完成）

- planning fetcher 只接受 stable capability，并重新读取目标 thread/post 解析当次动态
  attachment URL；无通用 redirect/登录 Cookie/proxy，仅转发站点当次签发的有界匿名
  会话 Cookie，并以 `O_EXCL|O_NOFOLLOW` 写入媒体根外的
  0700 工作目录，实际 volume size/hash/identity 由下载结果确定；
- Docker 固定官方 `7zz 26.02` 的 amd64/arm64 artifact SHA-256；adapter 使用固定
  `prlimit → /usr/bin/7zz` argv、technical manifest 和逐 member stdout extraction，
  不允许归档工具决定输出路径；
- magic/header 双重确认 ZIP/7z/RAR，RAR 多卷还要由技术 manifest 确认总卷数；
  加密、缺卷、zip-slip、Windows/UNC、`.env*`、symlink/hardlink/device、嵌套包、
  压缩炸弹、Unicode/casefold 重名和 archive identity drift 全部 fail closed；
- 独立 write-once `subtitle-acquisition-v1-<hash>.json` store 只在所有选择已下载、
  检查和 member hash 完成后持久化 canonical plan；工作区与媒体根必须互不包含；
- 离线 fake runner/transport 覆盖 ZIP/7z/RAR/完整与缺失多卷、远端 capability 变化、
  redirect、超限、plan 篡改和 symlink。M13.3 不签发审批、不发布媒体文件、不建立
  successor，真实 provider 仍未接入生产 composition。

### M13.4：审批、发布与 successor

- M13.4a（完成）：config schema v5 新增显式 `acgrip.enabled`（旧配置固定迁移为
  `false`）与独立 `subtitle_acquisition_policy`（默认 `automatic`，但 provider
  未启用时无效）；新增 `ApprovalScope.SUBTITLE_ACQUIRE`，并建立 plan/approval
  绑定的 deterministic transaction、逐 member write-once journal 和 transaction
  mutex。该 scope 不能作为 media `APPLY` 被消费；
- M13.4b（完成）：独立 executor 在 exact approval claim 后重新获取并核对全部卷、
  manifest、member/rejected set 与 source identity；应用只用 `O_EXCL|O_NOFOLLOW`
  逐 member 写入隐藏 staging，并且只接受 native no-replace directory rename。journal
  恢复覆盖 staging 建立、member 写入、rename 后 fsync 与 terminal 记录窗口；
- M13.4c（完成）：schema 27 durable successor outbox 将 acquisition settlement、
  原 run `superseded`、job terminal、lineage 单次获取记录和 enqueue 原子提交；worker
  只接受 watch/folder capability，fresh no-follow scan 通过 source/destination identity、
  新 snapshot 和完整计划字幕集合校验后，原子注册唯一 discovery/run/job。retryable
  scan 进入有界 retry，确定性漂移进入 blocked/needs-attention；successor 继承已消费
  lineage，不能再次自动获取；
- M13.4d（完成）：显式 opt-in 后才构造 production search/planning/execution lease；
  config revision、source/snapshot identity、lineage 与独立 policy gate 在副作用前再次
  校验。schema 28 持久化 acquisition request，automatic/manual/plan-only 分支互不
  借用 media apply；人工 API 绑定 exact plan hash 与幂等键，bounded read model/UI
  显示 request 与 successor blocked attention 状态。离线跨层验收覆盖 plan、approval、
  publish、fresh scan、successor 普通 `subtitle:N` 与防循环；
- 独立 config policy、approval scope、plan store、journal、executor 和 effect mutex；
- native no-replace staging publish、crash recovery、collision 和 fsync failure；
- durable successor outbox、lineage 单次自动获取闸门和 fresh snapshot 集成测试。

### M13.5：Agent loop hardening（完成）

- `acgrip.enabled=false` 时 Anime definition 使用基础 episode prompt/tool surface，
  不注册或注入 M13 inspector/search capability；revision/reapply 同样保持纯 mapping；
- runtime projection v8 持久化 subtitle-acquisition capability 与按季 search failure，
  tool discovery、tool implementation、reducer 和 `submit_mapping` 共享确定性 workflow
  projection；未探测、不确定、缺中文字幕、未翻完分页或漏季时均不能绕过 M13；
- `indeterminate/unknown`、完整空结果、完整但歧义结果和 provider failure 分别绑定
  可验证的结构化 attention reason，普通模型文本不能替代领域事件；
- 所有 Agent tools 提供非空 description；M13 prompt 使用显式状态机并说明分页、
  selection XOR payload、attention 条件和不可信论坛证据；
- archive-set observation 新增每组独立的 bounded label/coverage/language/group/warning
  证据，避免同一楼层多个附件组只能按格式和大小猜测；
- Anime AgentDefinition v7 与实际 capability tool list 绑定；新增 offline capturing-model
  surface/loop 测试，以及 `scripts/openai_m13_live_smoke.py --live` 的 opt-in 真实模型
  smoke（TMDB、探测和论坛 provider 均为 fake，只有 OpenAI 模型访问网络）；该脚本
  可从仓库根固定 `.env` 受限读取 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、
  `OPENAI_MODEL` 和 `OPENAI_REASONING_EFFORT`。真实模型矩阵覆盖分页候选与恶意
  摘要的多季完整选择、内嵌中文后 mapping、indeterminate attention、完整空结果
  attention 和 provider failure attention；每个场景同时断言对应领域状态证据，
  而非只检查 assistant 文本。内嵌中文 mapping 场景注入 completed-empty fake
  archive browser，以满足生产 `archive_search_required` 闸门而不放宽该不变量；
  smoke 显式使用 `finalize_plan=False`，把真实模型测试停在已验证 MappingDraft，
  不伪造文件系统 PlanCompiler/PlanStore。

每步先跑相关测试，再跑 `.venv/bin/python -m pytest -q`；pytest/CI 永不访问真实
论坛。安全决策见 [ADR 0007](adr/0007-m13-subtitle-acquisition-boundary.md)。
