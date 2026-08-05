# ADR 0007：M13 动漫字幕探测与获取边界

状态：Accepted

日期：2026-08-03

## 背景

Anime 入站目录可能没有外置字幕，但视频容器已有可用字幕；也可能需要从
ACG.RIP 字幕论坛选择一个匹配片源、季度和语言的字幕归档。论坛同一帖子可包含
多个版本，附件可位于回复中，原生附件地址含短期签名，因此搜索、语义选择和
文件副作用不能合并成一个 Agent tool。

项目按管理员已确认的决策直接访问固定用途的搜索、帖子和 native attachment
端点，不读取或执行 `robots.txt`，也不把它作为运行时配置或控制面提示。这不授权
登录、验证码/Cloudflare 规避、站外链接、任意 URL 或自定义网络配置。

## 决策

M13 只为 trusted `work_type=anime` 的 run 增加三个 Agent-facing contract：

- `check_sub_from_video(video_id, season_number)` 是单视频、snapshot-bound、
  no-follow 的只读探测。`season_number` 只占用每季一个的 advisory quota，不能
  决定 mapping 或路径；不确定结果不能当作无字幕。
- `search_sub(season_number, cursor)` 的标题别名只能来自已选 TMDB identity，
  provider 固定搜索 fid 37/46。Agent 只接收 bounded、去 URL 的不可信摘要和
  run-scoped release/archive-set ID。
- `select_subtitle_release` 是唯一语义选择动作。即使只有一个候选也必须由 Agent
  显式选择；Agent 不能选择 URL、归档 member、源路径或目标路径。

下载、归档检查、解压和发布永远不是 Agent tool。选择后由确定性 planner 生成
独立的 canonical `SubtitleAcquisitionPlan v1`。它绑定 run/config、授权源根、
folder identity、candidate snapshot、TMDB identity、provider/parser/policy
版本、稳定的 thread/post/attachment identity、完整归档卷和 subtitle member
的 size/SHA-256、manifest digest、拒绝原因以及固定资源限制。动态签名 URL 不
进入 plan。

归档能力固定为 ZIP、7z、RAR 和完整多卷 RAR。RAR 卷必须来自同一帖子楼层、
连续且由 header 确认；最多八卷、单卷 16 MiB、总压缩数据 64 MiB。最多检查
256 个条目，只允许发布 `.ass/.srt/.ssa/.sup/.vtt`；单字幕 32 MiB、总展开
128 MiB、压缩比上限 100。加密、嵌套归档、特殊文件、不安全路径、`.env*` 和
名字冲突均 fail closed。

真实发布必须使用独立 approval scope、journal 和 native no-replace directory
rename。发布后旧 snapshot 终止，通过 durable outbox 幂等建立唯一 successor；
每条 discovery lineage 最多一次自动获取。

## M13.0 边界

M13.0 只建立纯领域 schema、provider-neutral ports 和 canonical acquisition
plan/hash。它不注册 Agent tool，不访问论坛，不运行 ffprobe/7zz，不签发审批，
也不修改媒体目录。后续步骤必须分别建立离线失败测试后才能开放对应能力。

## M13.1 视频探测边界

M13.1 只为 Anime run 注册 `check_sub_from_video`。它接受当前 snapshot 的单个
opaque video ID 与已加载季号；snapshot 含任一独立字幕时 capability 不可用。
每季只记录一个完成探测，精确参数重放读取 event projection 中的缓存。

实现固定使用 Debian Trixie 的 FFmpeg 7.1.5；该版本包含 `fd:` seekable file
descriptor protocol。应用先 no-follow 打开并核对 snapshot identity，再通过固定
`prlimit → /usr/bin/ffprobe` argv 只读取 container/stream metadata，完成后再次
核对 identity。超时、输出超限、非零退出、解析漂移和不支持的零流容器均返回
`indeterminate`；只有受支持容器的完整零字幕流结果才返回 `absent`。本步骤不新增
网络访问、下载、解压、审批或媒体目录写入。

## M13.2 论坛搜索与选择边界

M13.2 实现固定 `https://bbs.acgrip.com` origin 的只读 Discuz provider。HTTP client
固定 `trust_env=false`、禁止通用/自动 redirect，仅保留站点当次响应签发且名称、域、
path、Secure 属性和值长度均受限的匿名 Discuz 会话 Cookie；搜索 POST 只手工接受一次
经结构化校验的精确同源 search-result 跳转。其余请求仅接受
`/search.php?mod=forum`、受约束的 search result path 与 numeric thread path；POST
字段固定为当前 formhash、最多三个 TMDB 标题别名和 fid 37/46。provider 不接受 URL、
fid、裸页码、调用方 Cookie、proxy 或自定义 base URL。

parser 可读取受限搜索页、帖子分页和回复楼层原生附件；动态签名 href 仅存在于单次
provider/fetch 调用的私有解析对象，不能进入 capability、event、state、plan 或 trace，
持久层只形成 run-scoped opaque ID 与稳定 numeric `tid/pid/aid` capability。Agent observation
仅含去 HTML/URL 的 bounded 证据摘要；站外链接与分卷 7z 不形成 capability，缺卷
RAR 不可选择。挑战/登录页、非上述精确搜索结果跳转、429/5xx、UTF-8/HTML 漂移、单页或累计响应
超限均 fail closed，不尝试登录、验证码或 Cloudflare 绕过。

`search_sub` 只在 Anime snapshot 完全无外置字幕且该季代表样本明确没有可识别中文
字幕时开放。`select_subtitle_release` 产生唯一语义接受事件；即使候选唯一也不能由
确定性代码自动选中。选择后仅进入 acquisition planning phase，M13.2 不下载附件、
检查归档、签发审批或写媒体目录。真实 worker provider 要等显式 opt-in 配置落地后
才实例化，避免升级自动访问新 origin。

## M13.3 归档检查与计划编译边界

M13.3 在媒体根之外的 `AuthorizedRoot` 工作区实现 restricted fetcher。fetcher 只接受
当前 run 的 `SubtitleArchiveSetCapability`，重新读取固定 thread pages，要求完全相同的
post、attachment ID 顺序和 archive grouping，再使用当次签名 URL 顺序下载。HTTP 仍为
固定 origin、`trust_env=false`、无通用 redirect/登录 Cookie/proxy；仅转发站点当次
签发的有界匿名会话 Cookie。文件以随机 attempt 目录和
固定 volume 名通过 `O_EXCL|O_NOFOLLOW` 创建，失败残留不覆盖也不删除。

归档 inspector 固定 checksum-pinned 官方 `7zz 26.02`，以固定资源限制 argv 获取
technical manifest；应用校验 magic/header、路径、条目类型、加密状态、资源上限和
Unicode/casefold 唯一性，再把每个允许字幕 member 单独输出到 stdout 计算 size/hash。
7zz 永远不控制应用输出路径。完整多卷 RAR 还必须由 manifest 确认卷数；分卷 7z、
缺卷、危险条目和内容/identity 漂移全部拒绝。

planner 要求工作区与 source root 互不包含，串行处理 Agent 已选 archive set，并在所有
卷和 member 均检查成功后创建 canonical `SubtitleAcquisitionPlan v1`，写入独立的
content-addressed、no-follow、write-once store。M13.3 不签发审批、不写媒体目录、
不建立 successor，production composition 仍不实例化真实 provider。

## M13.4a 配置、审批与 journal 边界

配置 schema v5 将 ACG.RIP opt-in 与现有 media apply policy 分离：升级 v1-v4 配置时
`acgrip.enabled=false`，`subtitle_acquisition_policy=automatic` 仅作为未启用时无效的
默认值。配置不接受 ACG.RIP base URL、Cookie、proxy 或登录信息。

字幕获取使用 `ApprovalScope.SUBTITLE_ACQUIRE`；其 canonical approval 仍精确绑定
`run_id + plan_hash + scope + expiry + nonce`，但不能被现有 media `APPLY` executor
消费。transaction ID、隐藏 staging 名和最终发布目录全部由 plan/approval hash
确定性派生。独立 write-once journal 记录 approval、download verification、staging、
逐 member、publish 和 terminal crash window，并以 no-follow 文件和进程内/文件锁
防止同一 transaction 并发执行。M13.4a 尚不下载或发布文件。

## M13.4b 获取 executor 与发布边界

executor 只能消费持久化的 `SubtitleAcquisitionPlan` 和精确绑定的
`SUBTITLE_ACQUIRE` claim。它在媒体目录出现任何新文件前重新获取所有卷，并要求
volume size/hash、technical manifest、member/rejected set 和 source folder identity
与 plan 完全一致。每个计划 member 由 inspector 单独输出，应用使用
`openat(O_EXCL|O_NOFOLLOW)` 写入 plan 派生文件名并完成文件与 staging fsync。

发布只调用平台原生 `renameat2(RENAME_NOREPLACE)` 或 `renamex_np(RENAME_EXCL)`；
不支持时返回 `ATOMIC_MOVE_UNSUPPORTED`，即使文件系统存在 checked-rename 兼容路径
也不得退化使用。write-once journal 绑定 staging inode/device，并允许在 mkdir、member
fsync、directory rename 和 parent fsync 后的 crash window 中验证实际状态后幂等继续；
不匹配的残留、symlink、大小写等价冲突和未知目录 identity 一律停止。M13.4b 不终止
旧 run，也不创建 successor；这些状态转换留给 durable outbox 增量。

## M13.4c successor outbox 与 lineage 边界

schema 27 为每个根 discovery 确定性派生唯一 `subtitle-lineage-v1-*`。发布完成后的
settlement、原 run `superseded`、原 job terminal、lineage 消费记录和 successor
outbox enqueue 必须在同一数据库事务中提交；精确重放幂等，不同 plan 对同一 lineage
的再次获取被拒绝。

outbox worker 只获得 `watch_id + source_folder` capability 和 immutable settlement，
不接受路径。worker 在事务外执行 fresh no-follow folder scan，随后在单一事务内核对
source folder inode/device、不同于旧 run 的 snapshot ID、已发布目录 inode/device、
全部计划字幕名称/size 以及仍存在的视频候选。通过后建立唯一 discovery/run/pending
job，并将 watch observation 切到 fresh snapshot；这条路径不依赖 watcher 恰好再次
触发。successor run 继承已消费 lineage，因此 automatic acquisition gate 永久关闭。
临时 scan 失败进入 lease/retry，确定性 snapshot 漂移进入 blocked，等待 M13.4d
projection 暴露为 needs-attention。

## M13.4d production composition 与控制面边界

production worker 只有在 immutable config revision 明确
`acgrip.enabled=true`、Anime work type 且 lineage 尚未消费时才构造 ACG.RIP search
lease。Agent 选择后，planning lease 在媒体根外生成并持久化 exact acquisition plan；
schema 28 的独立 request 记录固定 plan-only/manual/automatic policy。字幕 coordinator
使用独立 filesystem approval store、`SUBTITLE_ACQUIRE` scope、effect mutex 和 executor，
永不调用 media apply coordinator。

人工 API 只接受 run ID、`If-Match` exact plan hash 和 idempotency key；自动路径只由保存
计划时绑定的 config policy 驱动。read model 只公开 plan hash、policy、request 状态、
failure code 与 successor outbox 状态。论坛 HTML、附件 URL、完整帖子、工作区和文件系统
capability 均不进入 API/UI。successor `blocked` 显示为 attention 状态，但控制面不能
绕过 fresh-scan 校验或手工指定 successor 路径。

## 后果

- 论坛文本和附件名仍可影响 Agent 的业务选择，但不能扩大网络或文件能力。
- `SubtitleAcquisitionPlan` 不加入现有 RenamePlan union/store，防止误交给 media
  executor。
- 论坛阻断、HTML 漂移、签名失效或内容 hash 变化都会停止流程，而不是尝试绕过。
- M13 需要版本锁定的 ffprobe/7zz 和独立 runtime/event/config/executor 工作，
  但这些不属于 M13.0。
