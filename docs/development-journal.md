# Reeloom 开发学习日志

这份日志把每个实现切片对应到一个 Agent 概念。目标不只是“把功能写出来”，
还要能解释模型、工具、领域状态和副作用之间的边界。

## M0.1：先建立 Agent 不能绕过的领域契约

### 这一步实现了什么

- `CandidateId` 只接受 canonical 的 `video:N` 或 `subtitle:N`。
- candidate ordinal 使用有界正整数，超长 ID 只返回结构化领域错误。
- `Candidate` 的输入字段是封闭集合，额外的 `path` 等字段会被拒绝。
- 所有 `from_dict` 入口先验证顶层值确实是 object。
- `CandidateSnapshot` 由 frozen dataclass 和 tuple 组成，并拒绝重复 ID。
- 领域失败使用稳定的 `ErrorCode` 和 `ErrorCategory`，不要求调用方解析错误文本。

### 对应的 Agent 原理

Agent loop 可以概括为：

```text
模型选择动作
→ 工具把结构化输入交给确定性内核
→ 内核接受或返回结构化错误
→ 模型根据 observation 决定下一步
```

这里最重要的是：模型的“决定”不是权限。模型未来可以请求处理 `video:1`，
但它不能提交任意文件路径，也不能靠自然语言让非法状态变合法。

opaque ID 是 capability boundary 的第一部分。它隐藏真实路径，使模型只能引用
当前 run 已经授予的对象。M0.1 只定义 ID 的纯领域格式；ID 与具体 run 和扫描
快照的绑定会在 M2 完成。

文件名、TMDB 文本和工具 observation 都必须视为不可信数据。测试中特意使用
`[ignore previous instructions].mkv` 作为展示名：内核原样保存它，但从不解释
或执行它。

### 为什么还没有接 Agents SDK

如果先接模型再补约束，很容易把 prompt 当成安全边界。M0 先用纯函数和不可变
模型定义“无论模型说什么都成立”的规则。M1 才会把这些规则放进真实的 SDK
tool loop，并用 scripted fake model 离线观察工具调用、事件和停止条件。

### 如何验证

```bash
.venv/bin/python -m pytest -q
```

测试既覆盖正常创建，也覆盖非法 ID、extra keys、ID/kind 不一致和重复 ID。

## M0.2：模型提出 mapping，内核决定它是否成立

### 这一步实现了什么

- `EpisodeSpan` 表示一个视频对应的单集或连续多集范围。
- `EpisodeCatalog` 提供与 TMDB adapter 无关的季集边界。
- `MappingDraft` 严格拒绝 extra keys、未知 candidate ID 和错误 ID kind。
- 同一集不能被两个视频范围占用，同一视频和字幕也不能重复映射。
- 字幕只能指向当前 draft 中已经映射的视频。

### 对应的 Agent 原理

未来的 `submit_mapping` 工具不会把模型输出直接写入 run state。数据流是：

```text
LLM 生成 tool arguments
→ SDK 做工具参数解析
→ kernel 校验 snapshot、集数和关联关系
→ 成功：返回不可变 MappingDraft
→ 失败：返回有限、结构化的 DomainError observation
→ LLM 根据错误码修正后重试
```

这是 Agent 系统中常见的 validator feedback loop。LLM 擅长根据文件名和元数据
推断语义，但它不适合负责“是否越界”“是否重叠”这类必须始终一致的问题。
这些规则由确定性内核处理，测试可以穷举边界，而且不需要调用模型。

`EpisodeCatalog` 不包含 TMDB SDK 类型。以后 TMDB adapter 只负责把外部数据转成
这个领域模型，kernel 不知道数据来自网络、fixture 还是真实 API。这叫
dependency inversion：高价值的领域规则不依赖易变化的外部系统。

snapshot membership 校验则补全了 capability boundary：仅仅长得像 `video:99`
还不够，它必须确实存在于当前 run 的 candidate snapshot。字幕关联校验确保
Agent 不能借一个未映射视频把额外文件悄悄带入后续计划。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_mapping.py
```

测试覆盖单集、多集、不可变性、extra keys、伪造 ID、错误 kind、季集越界、
range overlap、重复映射和字幕归属。

## M0.3：destination 是编译结果，不是 Agent 建议

### 这一步实现了什么

- 剧集根目录固定为 `{series_zh_cn} ({year}) {tmdb-{tmdb_id}}`。
- 季目录固定为 `Sxx`。
- 视频固定使用 `动画名 SxxExx.ext` 或 `动画名 SxxExx-Eyy.ext`。
- 字幕复用视频 base，并加入 `.chs/.cht/.chi`。
- 标题经过 Unicode normalization、控制字符与路径字符清洗、UTF-8 长度限制和
  Windows 保留设备名处理。
- 视频和字幕扩展名分别使用有限白名单并 canonicalize 为小写。

### 对应的 Agent 原理

安全能力最好通过接口结构限制，而不是通过 prompt 请求模型自律。命名函数的
输入只有：

```text
SeriesIdentity + EpisodeSpan + file extension (+ subtitle variant)
```

它没有 `destination` 参数，也没有 `episode_title` 参数。因此模型即使生成了
诸如 `../../target` 的文本，也没有位置可以把它传入命名编译器。这叫
structural safety：非法能力在类型和 API 层面不可表达。

命名代码也不是 Agent tool。它没有需要模型判断的语义，只是一段确定性的
编译过程：

```text
validated domain objects
→ canonical path components
→ PurePosixPath relative destination
```

相同输入永远得到相同输出，且不访问文件系统。后续 `Plan Compiler` 会调用它，
Agent 只能查看最终 preview。

路径文本清洗与路径 containment 是两层不同防线。M0.3 只确保不可信标题能安全
进入单个路径组件；后续 path policy 和 Executor 仍必须验证目标位于授权根目录
内，并抵抗 symlink 与 TOCTOU。不能因为“名字已经清洗”就跳过 containment。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_naming.py
```

测试覆盖单集、多集、`S00`、三种字幕后缀、恶意 Unicode/路径文本、保留设备名、
长 UTF-8 标题、非法扩展名，以及拒绝 destination 和 episode title。

## M0.4：语义 hint 可以影响判断，不能扩大权限

### 这一步实现了什么

- local special 只有 `video_id` 和 `ova/oad/unknown` hint；稳定顺序来自 scanner
  分配的 candidate ID ordinal，Agent 不能提交 order。
- TMDB episode 使用 `season_number + episode_number` 和同一组有限 hint。
- OVA/OAD hint 可跨 season 优先匹配，未知剩余项只在 S00 按稳定顺序回退。
- hint 证据不足时 fail closed，不静默映射到其他类型。
- resolver 直接产出已有的 `VideoMapping`，不再维护重复的 special assignment
  类型。
- 所有目标经过 `EpisodeCatalog` 校验；多余本地视频和 TMDB episode 分别显式
  进入 unmapped 和 unused。
- 仓库新增威胁模型，区分 M0 已实现控制和后续里程碑待实现控制。

### 对应的 Agent 原理

Agent 系统经常同时包含“软判断”和“硬约束”。OVA/OAD 分类是软判断：模型可以
根据文件名和 TMDB 文本提出 hint。以下规则则是硬约束：

```text
hint 只能是有限枚举
→ ID 必须属于当前 snapshot
→ 同 kind 证据优先
→ 证据冲突时 fail closed
→ 未匹配资源保持显式可见
```

这体现了 semantic authority 与 operational authority 的分离。模型可以影响
“这更像 OVA 还是 OAD”的语义判断，但不能借此选择路径、读取新文件、访问新
URL 或执行移动。

resolver 也不读取原始 title 或 filename。上游将不可信文本转为有限结构后，
resolver 只对结构做确定性运算。这样既保留 Agent 的语义能力，也缩小 prompt
injection 能传播到的范围。

一个重要的非对称规则是：明确的 OVA/OAD hint 可以指向普通 season，因为 TMDB
数据确实可能这样归档；`unknown` fallback 却只能使用 S00。否则“没有证据”会
变成把本地文件塞进常规季的权限，风险远高于保留 unmapped。

显式 `unmapped` 和 `unused` 很重要。安全系统不应通过丢弃数据来假装成功；
无法证明的映射必须保留下来，供 Agent 获取更多证据或用户审查。

完整安全边界和后续控制见 [威胁模型](threat-model.md)。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_specials.py
```

测试覆盖跨 season hint、S00-only fallback、混合匹配、unmapped/unused、证据
冲突、重复 ID/episode、catalog 越界、拒绝 Agent order、snapshot membership、
错误 kind 和注入式额外文本。

## M0.5：Plan 是推理与副作用之间的协议

### 这一步实现了什么

- `PlannedMove` 没有公开 destination 构造入口，只能通过视频或字幕命名工厂创建。
- `PlanDraft` 是 frozen、带 schema/policy version，并绑定单一 `SeriesIdentity`
  与 validated `MappingDraft` 的纯领域快照。
- 每个 move 的 source、关联 video、span 和 series 必须与 mapping 一致；
  unmapped 由 `snapshot - mapping` 自动推导。
- 重复 source、伪造 ID、状态冲突、遗漏和 destination collision 都会失败。
- destination collision 使用 Unicode normalization + casefold，覆盖跨平台风险。
- 同一集的多个同语言同扩展名字幕按稳定 source ID 生成 `.chs.ass`、
  `.chs.1.ass` 等名称；其他碰撞不自动解决。
- fixture 记录了对应旧 `aninamer` plan 测试的 provenance，但不导入其 runtime。

### 对应的 Agent 原理

Agent 的工作应该结束在一个可审查协议，而不是直接触发副作用：

```text
LLM semantic mapping
→ deterministic validation
→ immutable PlanDraft
→ 后续 canonical RenamePlan + hash
→ independent approval
→ isolated executor
```

`PlanDraft` 的完整分区很重要。如果系统只记录 moves，消失的文件很难区分是
“有意不处理”还是“代码漏掉了”。它不接受调用方提供 unmapped 清单，而是从
validated mapping 与 snapshot 的差集推导；同时要求每个 mapped candidate 都有
且只有一个一致的 move，因此每个 candidate 的状态都能被审计。

字幕消歧展示了“可证明的自动修复”和“危险猜测”的区别。两个 subtitle source
拥有完全相同的 canonical destination 时，可以按 scanner ID 稳定编号；视频
碰撞或仅大小写不同的目标没有安全、业务等价的修复，因此继续 fail closed。

当前对象刻意叫 `PlanDraft`。它还没有绑定授权根、source identity、canonical
bytes 或 hash，不能被批准和执行。M5 才会把它编译成真正的 `RenamePlan`。

### Golden fixture provenance

`tests/fixtures/m0_plan_cases.json` 的行为来自旧项目以下测试：

- `test_build_rename_plan_basic_and_ignores_unmapped`
- `test_build_rename_plan_disambiguates_subtitles`
- `test_build_rename_plan_includes_s00_for_ova`

fixture 只复制用户可观察规则，不导入旧项目代码或绝对路径。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_plan.py
```

完整 M0 验收见 [M0 Definition of Done](m0-review.md)。

### 下一步

M1 将第一次引入 Agents SDK：实现 `RunState`、phase、typed events、reducer、
tool policy、预算和 scripted fake model，并使用 SDK Runner 的真实 tool loop。

## M1：让 SDK 管循环，让领域事件管业务

### 这一步实现了什么

- `RunStarted`、`ToolRequested`、`ToolSucceeded`、`ToolRejected`、
  `RunStopped` 和 `RunFailed` 是不可变 typed events。
- 纯 reducer 把事件折叠成 `RunState`；append-only 内存 store 只在 reduce
  成功后追加事件。
- `PhaseToolPolicy` 默认拒绝未知工具，`RunBudget` 同时限制模型轮数、工具调用
  和重复失败。
- `EpisodeOrganizerAgent` 使用 Agents SDK 的 `Agent`、`Runner` 和
  `function_tool`，没有自建 while/tool-call loop。
- `ScriptedModel` 实现 SDK `Model` protocol，以固定 transcript 离线驱动真正的
  Runner。
- M1 的 `list_candidates` 只从注入的 immutable snapshot 返回有界 opaque ID；
  它不扫描目录，也不接收或返回路径。

### Agent、tool 和 observation 分别是什么

这次真实运行的数据流是：

```text
ScriptedModel 选择 list_candidates
→ SDK Runner 校验并调度 function tool
→ phase / budget / input guardrail
→ 领域工具返回 JSON observation
→ SDK 把 observation 放入下一轮模型输入
→ 模型产生下一动作或最终文本
```

Agent 不是“一个模型对象”。它是 instructions、model、tools 和运行策略的组合；
Runner 才负责重复调用模型、执行工具并把结果送回模型。Reeloom 不复制这套循环，
只记录自己的领域事件。

tool 也不是模型拥有的函数权限。模型只能提出一个带 schema 的调用请求；调用
是否存在、当前 phase 是否允许、参数是否严格、预算是否足够，全部由本地代码
决定。observation 是工具返回给模型的有限数据，不是真实对象本身，更不是新的
权限。

### State 与 event 为什么要分开

event 表示已经发生的事实，state 是 reducer 对事实的投影：

```text
events: RunStarted → ToolRequested → ToolSucceeded → RunStopped
                               reducer ↓
state:  phase、status、tool_calls、failures、pending calls、stop reason
```

event store 没有时间戳等非确定字段，因此相同 transcript 可以逐项比较并稳定
replay。reducer 还会拒绝“没有 request 却声称 tool succeeded”和“仍有 pending
call 却停止”等非法序列。

SDK 的 run 结束与业务完成是两个不同事实。M1 中模型的最终文本只产生
`StopReason.MODEL_FINAL`，phase 仍是 `IDENTIFY_SERIES`。它不会进入
`COMPLETED`；以后必须由有效领域动作产生成功事件，assistant 文本没有这项
权限。

### 为什么 schema 之外还要 guardrail

SDK 会把 strict JSON Schema 提供给模型，但安全系统不能假设模型一定遵守。
M1 的执行端 input guardrail 会再次检查：

- JSON 必须是 object；
- 字段集合必须精确等于 `{limit}`；
- `limit` 必须是非 bool 的整数并位于 `1..50`；
- 原始参数文本有长度上限。

因此 extra key、错误类型、越界值和非法 JSON 都变成相同的有限结构化
observation。未知工具、phase 不允许和 adapter 失败也使用稳定错误码；错误文本
不会被当成控制指令。

### Scripted fake model 与 mock 的区别

`ScriptedModel` 不是另一套 Agent runtime。它只实现 SDK 要求的 model protocol，
按脚本返回 Responses API 形状的 tool call 或 assistant message。模型/tool
调度、observation 回填、最大轮数和异常包装仍由真正的 SDK Runner 完成。

测试还可以要求某一步输入必须包含前一工具 observation。例如第二轮只有看到
`video:1` 才返回最终文本，这证明多轮反馈闭环确实发生了。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/runtime
```

完整 M1 验收见 [M1 Definition of Done](m1-review.md)。

### 下一步

M2 会把当前注入的 immutable snapshot 替换为安全 scanner，并补全 no-follow
symlink、授权根 containment、`.env*` 拒绝、root identity 和真正的
`list_candidates` 分页。

## M2：把文件系统对象降级成 capability

### 这一步实现了什么

- `AuthorizedRoot` 只接受绝对、已存在、完整祖先链无 symlink 的目录，并记录
  授权时的 root device/inode。
- scanner 从根目录逐级使用 directory FD、`O_DIRECTORY` 和 `O_NOFOLLOW` 打开
  子目录；file/dir symlink 都不会被遍历。
- `.env*` component 在任何 `lstat`、open 或 stat 前按名称拒绝；unsupported
  extension、special file 和 symlink entry 不进入快照。
- 视频与字幕使用和 naming compiler 相同的 extension 白名单。
- scanner 产出的 `ScannedFile` 先稳定排序，再分别分配 `video:N` 和
  `subtitle:N`。
- immutable snapshot 包含公开 `CandidateSnapshot` 与内部 exact relative-path
  table；两者按 ID 一一绑定。
- `CandidateSnapshotCreated` event 把 snapshot ID 与 candidate count 写入
  `RunState`，并把 phase 从 `BOOTSTRAP` 推进到 `IDENTIFY_SERIES`。
- `list_candidates` 升级为 `kind + cursor + limit` 的严格分页工具。
- scanner 同时限制全部 directory entries、candidate 数、递归深度和相对路径
  字节数；unsupported 文件也会消耗 scan budget。
- candidate 工具只有在 `CandidateSnapshotCreated` 已进入当前 RunState 后才可用。

### Capability 为什么比“清洗路径”更重要

如果工具接受路径，模型始终可以尝试：

```text
../../outside
/absolute/secret
C:\other-root
linked-dir/secret
```

逐个清洗这些字符串只是补洞。M2 采用更小的接口：

```text
filesystem path
→ authorized scanner
→ immutable snapshot
→ run-scoped video:N / subtitle:N
→ Agent tool
```

模型的工具 schema 中根本没有 path 字段。它只能枚举当前 run 已经授予的
candidate。精确 relative path 留在内部 table，未来 kernel/executor 使用 ID
查表；展示名永远不能反向决定操作路径。

这就是 object capability 的基本思想：拿到一个不可伪造、作用域有限的引用，
而不是拿到一段可以被重新解释成任意资源位置的字符串。

### 为什么不能先 resolve 再判断

`Path.resolve()` 会解释 symlink，因此不适合作为第一道授权检查。M2 的顺序是：

```text
词法检查 absolute / .. / Windows path / .env*
→ 从 filesystem anchor 逐级 lstat
→ 任意 symlink 立即拒绝
→ 记录 root device/inode
→ scanner 用 openat-style directory FD 逐级 O_NOFOLLOW
```

扫描过程中始终持有父目录 FD；打开子目录时使用相对该 FD 的名字。即使目录项在
检查与打开之间被替换成 symlink，`O_NOFOLLOW` 也会使扫描失败，而不会跟随到
授权根之外。平台如果不能提供这些 flags，adapter 会 fail closed。

M6 的 Executor 仍要在真正移动前重新做 identity 与 containment preflight。
扫描安全不能替代执行时校验。

### Snapshot 为什么要同时有公开层和内部层

公开层只包含：

```text
opaque id + kind + bounded display_name
```

内部层另外保存 exact relative path 和扫描时 size。Agent 的 observation 会把
控制字符、换行和 bidi 控制符替换为可见占位符，并限制 UTF-8 字节数；内部文件
名保持原样，不会用展示副本寻找文件。

snapshot ID 是当前候选记录的 deterministic digest，用于事件关联和 replay。
它还不是 M5 的 `plan_hash`，也不能被批准或执行。

### 分页也是安全边界

`list_candidates` 的输入字段集合必须精确等于：

```text
kind: video | subtitle
cursor: 0..2^31-1
limit: 1..50
```

cursor 只能移动到同 kind snapshot 的下一页，越过末尾会得到
`invalid_cursor`。source 返回的 item 数、kind、ID uniqueness、next cursor 和
最终 observation bytes 还会在工具层重新验证。

即使有人绕过正常的 context factory 直接构造 `ToolRuntime`，缺少 snapshot
event 时也只会得到 `capability_not_available`，不会访问传入的 source。

这不只是性能优化。分页和文本上限防止模型一次调用把整个大目录塞入上下文，
也让工具预算能够衡量调查成本。

### 如何验证

```bash
.venv/bin/python -m pytest -q \
  tests/policy tests/kernel/test_scanner.py \
  tests/adapters/test_filesystem.py tests/tools/test_candidate_pagination.py \
  tests/integration/test_candidate_agent.py
```

测试覆盖稳定 ID、kind 分页、page 上限、absolute/`..`/Windows path、
root/file/parent symlink、指向 `.env*` 的 dangling symlink、scanner candidate
上限和 prompt-injection filename。测试不会创建或读取真实 `.env*` 文件。

完整 M2 验收见 [M2 Definition of Done](m2-review.md)。

### 下一步

M3 将接入唯一允许的业务网络 adapter：TMDB HTTP adapter。Agent 获得的仍是
受 phase、schema、超时、缓存、调用数和 response-size 限制的 typed tools，
而不是任意 URL 请求能力。

## M3：让 Agent 选择调查路径，不选择网络权限

### 这一步实现了什么

- `TmdbProvider` port 定义 search、series details 和 season details 三种受限操作；
  fake 与 HTTP adapter 实现相同协议。
- HTTP adapter 的 host 固定为 TMDB API v3，只暴露固定 endpoint，不接受 base
  URL、完整 URL、method 或任意参数。
- adapter 限制 5 秒请求时间、1 MB response body、返回条目数、文本长度，以及
  600 秒 / 128 项的 TTL/LRU cache。
- `search_tmdb` 固定使用 `zh-CN`，并把有界
  `(work_type, tmdb_id)` 搜索结果追加为 `TmdbCandidatesObserved`。
- `get_tmdb_series` 只能检查已观察候选或已选剧集；`get_tmdb_season` 只能在
  mapping phase 检查已选剧集。
- `select_series` 只能选择当前 run 搜索过的 ID，并重新取得 zh-CN details；
  有效的 `SeriesSelected` 事件才会进入 `MAP_EPISODES`。
- zh-CN localized name 优先，缺失时回退 original name；缺失首播年份则
  fail closed。
- season episode 从中英文 name/overview 中只提取明确的 OVA/OAD 证据，season
  number 不影响 hint，因此兼容 TMDB 将 OVA/OAD 放入常规 season 的情况。

### HTTP adapter 为什么仍然是 Agent 的受控工具

这里没有把 TMDB 变成固定 pipeline，也没有给 Agent 一个通用 HTTP client。
实际控制流是：

```text
Agent 决定搜索词和调查顺序
→ typed tool 校验 phase/schema/capability/budget
→ provider port
→ 固定 TMDB endpoint 的 HTTP adapter
→ 有界领域模型
→ 有界 observation
→ Agent 决定下一步
```

因此 Agent 可以主动调用 TMDB，但只能使用 Reeloom 预先定义的四项业务能力。
它不能改变 host、path、HTTP method，也不能把 TMDB 返回的 URL 当作新权限。
这与接 MCP 的核心安全目标相同，但当前只有一个进程内 provider，HTTP adapter
更小、更容易离线替换和审计；没有必要为了协议层而引入 MCP。

### 搜索结果为什么要变成 capability

只限制 `tmdb_id` 是正整数还不够，模型仍可猜测任意剧集 ID。M3 把 search 的
结果转成领域事件：

```text
typed search result references
→ TmdbCandidatesObserved
→ RunState.tmdb_candidates
→ inspect/select membership check
```

这与 M2 的 `video:N` 思路相同：数据被观察到之后，才获得当前 run 内的有限
引用权。`select_series` 在调用 provider 前先做 membership check，所以伪造 ID
不会产生网络请求。

`SeriesSelected` 进一步展示 event-driven state：tool 返回的 assistant 文本或
普通 observation 不能推进 phase；只有 reducer 接受的 typed event 可以。选中
后，后续 season 查询又收窄到这一个 TMDB ID。

### 外部数据和故障为什么也要降级

TMDB title、overview 和错误响应都属于不可信输入。adapter 先做 Unicode
normalization、控制字符替换、字段类型和唯一性校验，再转换成 frozen 领域模型；
tool 只输出白名单字段并再次限制总字节数。

网络异常不会把底层 request URL 或异常 cause 传给 Agent，只返回稳定错误码和
`retryable`。401/403、404、429、5xx、timeout 和超大 body 都有确定映射，模型
不需要解析供应商错误文本。

凭据只通过构造器显式注入 `TmdbHttpAdapter`。Reeloom 不读取配置文件，凭据也
不进入 cache key、`repr`、领域模型或 observation。自动化测试全部使用 fake
provider 或 `httpx.MockTransport`，不访问真实 TMDB。

### 如何验证

```bash
.venv/bin/python -m pytest -q \
  tests/kernel/test_tmdb.py tests/adapters/test_tmdb_http.py \
  tests/tools/test_tmdb_tools.py tests/integration/test_tmdb_agent.py \
  tests/runtime
```

测试覆盖无/单/歧义候选、伪造 ID、zh-CN identity、OVA/OAD 中英文数据、
固定 endpoint、cache、timeout、body 上限、HTTP 状态映射、extra URL 字段拒绝，
以及真实 SDK Runner 驱动的 search → inspect → select → season tool loop。

完整 M3 验收见 [M3 Definition of Done](m3-review.md)。

### 下一步

M4 会实现 `get_existing_inventory`、`detect_subtitle_variant` 和
`submit_mapping`。模型提出语义 mapping，内核用候选 membership、TMDB 集数
边界、range overlap、已有库存和字幕归属规则验证；只有成功的领域事件才能进入
`BUILD_PLAN`。

## M3 补全：作品类型不是一个字符串标签

### 从 aninamer 保留了什么

旧项目的 `search_tv_anime()` 使用 `/search/tv`，读取 `genre_ids` 并用 TMDB
Animation genre 16 过滤；worker 则通过 trusted `watch_root.key` 将一个
`input_root` 固定映射到一个 `output_root`。这两点都值得保留：

```text
source parent / watch root
→ trusted archive category
→ bounded TMDB filter
→ fixed archive destination capability
```

Reeloom 没有保留旧实现“没有 anime 结果就返回所有 TV”的 fallback。显式
`anime` filter 没有动画证据时返回空，因为类型不确定应该补充证据，而不是静默
降级。

### 为什么分成 media_type 与 work_type

TMDB 的对象 namespace 是：

```text
media_type = tv | movie
```

用户的归档分类是：

```text
work_type = anime | tv_series | movie
```

`anime` 不是 TMDB 的第三种 media type，而是：

```text
media_type == tv AND genre_ids contains 16
```

因此 M3 同时返回两个字段。`anime` 和 `tv_series` 都走 TV endpoint；
`movie` 走 Movie endpoint，并使用 `title/original_title/release_date` 字段。
这避免把供应商数据模型和本地目录分类混为一谈，也为以后修改归档分类而不改
TMDB port 留出空间。

### 类型为什么必须进入 capability

TMDB 的 TV 与 Movie 是不同 namespace，同一个整数可能分别代表两个对象。
候选权限不能再写成：

```text
tmdb_id = 100
```

而必须是：

```text
(work_type, tmdb_id) = (anime, 100)
```

`RunStarted` 还必须显式携带 trusted `work_type`。`search_tmdb` 虽然公开 filter
字段，但 filter 必须与 run 类型一致；它不能让 Agent 把位于 `anime/` source
root 的内容改送到未来的 `movie` destination。Organizer instructions 会直接
告诉模型本 run 被授权的类型，但真正 enforcement 仍在 tool 和 reducer。

### 为什么目前只搜索 movie，不允许 select_series

电影没有 season/episode，也不应该生成 `SxxExx`。当前确定性 kernel 只有
`SeriesIdentity`、`EpisodeSpan` 和 episode plan contract。把 movie 直接推进
`MAP_EPISODES` 会制造一个类型上说谎的状态。

因此 movie search 已支持并返回完整类型信息，但 `get_tmdb_series`、
`get_tmdb_season` 和 `select_series` 对 movie 返回结构化
`unsupported_work_type`。后续需要先定义 movie identity、单视频约束、命名和
plan compiler 分支，再开放 movie selection。归档父目录到 dst 的具体路径映射
也必须由 trusted bootstrap/root capability 完成，而不是由 Agent 的
`work_type` 字符串构造。

### Fake 为什么还需要官方 contract fixture

手写 fake 只能证明“代码和自己想象的 API 一致”，不能证明想象正确。M3 因此
增加 `tests/fixtures/tmdb_api_v3_contract.json`，从 TMDB 官方 OpenAPI 示例投影
出 adapter 实际使用的字段，并保留来源 URL 与抓取日期。

这组 fixture 特意保留真实响应的结构差异：

- TV search 使用 `name/original_name/first_air_date`；
- Movie search 使用 `title/original_title/release_date`；
- release date 可能是空字符串；
- TV details 的 `genres` 是对象数组，`seasons` 含大量未使用字段；
- Season details 顶层 `id` 是 season ID，episode 内另有 `show_id`、
  `season_number` 和 `episode_number`；
- adapter 必须忽略白名单之外的真实字段，而不是因多余字段失败。

它仍是离线 contract test，不声称替代线上 smoke test。官方 schema 变化可以通过
重新投影 fixture 和 code review 显式吸收；生产连通性、凭据有效性和未文档化
服务端变化仍需要单独、显式 opt-in 的 smoke check。

### 为什么 live smoke 不放进 pytest

线上 smoke 的用途是验证“当前凭据 + 当前 TMDB 服务 + 当前 adapter”能够一起
工作，它不是确定性的单元测试。把它放进 pytest 会让默认测试受网络、限流和服务
状态影响，也可能让 CI 在不知情时使用真实凭据。

因此 `scripts/tmdb_live_smoke.py` 同时要求：

```text
显式 --live
AND
进程环境或仓库根固定 .env 提供 TMDB_API_KEY
```

dotenv 读取是专门授权的 diagnostic 例外：仅在 `--live` 之后、仅固定仓库根
`.env`、no-follow、64 KiB 上限、只解析唯一 `TMDB_API_KEY`，不执行变量展开。
脚本不允许改变配置路径、host、endpoint、query 或预期 ID，也不输出 TMDB 返回
的标题和简介。它用固定样本覆盖 Anime/TV/Movie search、TV details、Season
details 和 adult 内容能力。这里体现的是 adapter 的两层验证策略：

```text
离线 fixture/MockTransport：稳定、详尽、覆盖失败路径
显式 live smoke：稀疏、真实、发现服务端契约漂移
```

### adult capability 检查的准确语义

TMDB 的 Movie/TV search 将 `include_adult` 定义为请求参数，供应商默认值是
false；Reeloom adapter 的产品策略则默认 true。Movie details 返回一个 `adult`
boolean。API key 本身没有可由 v3 key-only 请求读取的独立 “adult enabled”
权限字段。

因此 live smoke 不会把一次 HTTP 200 当作成功，而是验证：

```text
显式 include_adult=false → 固定 adult ID 不出现
默认/显式 true           → 固定 adult ID 出现
/movie/{id}                → metadata.adult is true
```

生产 Agent 的 `search_tmdb` schema 没有 `include_adult` 字段，工具执行端固定
传 true。模型既不需要主动开启，也不能关闭 adult 搜索；false 只用于 live
diagnostic 的对照请求。

## M4：模型提出答案，领域事件决定答案是否成立

M4 的核心不是“让模型输出一段 JSON”，而是建立一个可纠错的闭环：

```text
Agent 提交 mapping
        ↓
确定性 kernel 校验
        ├─ 失败 → bounded validation observation → Agent 修正
        └─ 成功 → MappingSubmitted event → BUILD_PLAN
```

Pydantic strict schema 负责挡住错误形状和 extra keys；`MappingDraft` 负责
candidate ID、episode boundary、range overlap 和字幕归属；`ExistingInventory`
再拒绝目标库已占用的 episode。分层的意义是：模型可以犯语义错误，但不能绕过
确定性不变量。

### Observation 不是权限

`get_existing_inventory` 返回 season/episode 元组，不返回 archive path。
`detect_subtitle_variant` 接受 `subtitle:N`，filesystem adapter 根据内部
capability table 定位文件，逐级 no-follow 打开，并最多读取 64 KiB。字幕正文
被视为 prompt injection 输入，只在纯分类函数中使用；Agent observation 只有
`chs/cht/chi`。

因此模型看到的信息足以完成任务，但不足以把工具转化成任意文件读取器。

这里还有两个容易被 `None` 掩盖的 capability 细节：

- “没有 inventory provider”不是“已确认库存为空”；空库存也要显式构造；
- `CandidateSnapshot` 对象本身不够，mapping 必须同时绑定当前 run 的
  `snapshot_id` 和 candidate ID 集合。

字幕文件在扫描时记录 device、inode、时间戳和 bounded prefix digest；检测时
前后复核 identity 与 digest。同长度原地改写因此也会 fail closed，而不是只靠
`st_size` 猜测文件没变。

### 为什么成功必须是 event，而不是 final answer

模型输出 “mapping 已完成” 只是文本。Reducer 只在收到经过工具校验构造的
`MappingSubmitted` 时将 `MAP_EPISODES` 改为 `BUILD_PLAN`。这把控制面的自然
语言与数据面的状态转换分开，也使相同 event transcript 可以确定性 replay。

### 四种预算约束的职责

- turn budget 限制模型推理轮数；
- tool/failure budget 限制调查和反复试错；
- token budget 限制累计模型消耗；
- wall-clock budget 限制模型或 adapter 卡住的总时间。

时间预算可能在工具执行中途取消 coroutine。此时 event log 中已经有
`ToolRequested`，却没有 `ToolSucceeded/ToolRejected`。M4 允许
`BUDGET_EXHAUSTED` stop event 明确清空这类已取消 pending call；其他 stop reason
仍然携带 pending call 时 fail closed。

`asyncio.timeout` 的取消是协作式的，外部 model 可能捕获 `CancelledError`。
因此 runner 在 context manager 正常退出后仍检查绝对 deadline；上游自己抛出的
`TimeoutError` 只有在本地 deadline 确实过期时才会被归类为预算耗尽。Token
usage 只能在一次 provider response 返回后获知，所以 M4 在 response 后记账，
并在下一次 LLM 调用前阻止已达到额度的 run；单次输出另有 `max_tokens` 上限。

完整 M4 验收见 [M4 Definition of Done](m4-review.md)。下一步 M5 会把已验证的
mapping 编译成 canonical、不可变且带 `plan_hash` 的 `RenamePlan`，仍不会让
Agent 直接决定路径。

## M5：从语义结果到可审批事务输入

M4 的 `MappingDraft` 回答“哪个候选对应哪一集”，但它还不能被执行。M5 增加
一条不经过模型的确定性链路：

```text
MappingSubmitted
→ compile PlanDraft destinations
→ read-only destination preflight
→ canonical RenamePlan
→ PlanBuilt(plan_hash)
→ ApprovalRequested(exact plan_hash)
→ AWAITING_APPROVAL
```

### 为什么 compiler 不是 Agent 工具

工具调用属于 Agent 的选择空间。如果把 `build_plan` 暴露成工具，模型就可以
决定是否编译、漏掉哪些输入，或者在错误 phase 反复调用。计划编译没有语义
不确定性，因此 `submit_mapping` 成功进入 `BUILD_PLAN` 后，SDK 立即结束 tool
loop 并由代码强制执行。Agent 提交的 schema 里始终没有 source path、
destination path、root 或 policy version。

### canonical bytes 与 hash 绑定什么

`RenamePlan` 显式序列化 schema/policy version、run ID、trusted work type、UTC
创建时间、两端 root path 和 directory identity、candidate snapshot ID、全部
source relative path 和 stat identity、mapping、字幕变体、moves、目标检查
结果与未映射清单。
JSON 固定 `sort_keys`、separator 和 ASCII escaping，然后计算：

```text
plan_hash = sha256(canonical_bytes)
```

这里不依赖 Python 对 dataclass 或 dict 的隐式序列化，因此字段新增必须成为
显式的 schema/policy 变更。相同领域输入与相同注入时钟会得到完全相同的 bytes。

### 为什么 M5 preflight 之后 M6 还要再检查

M5 只读确认目标当时不存在、已有父目录不是 symlink，并把结果写进 plan。检查
结束后文件系统仍可能变化，所以这不是执行许可。M6 必须在消费一次性审批后、
任何移动前重新验证 plan hash、roots、source identity、symlink 和 collision。
这就是 TOCTOU 防御中的“计划时观察 + 执行时最终验证”。

M5 的 scripted integration 已从错误 mapping 修正一路运行到
`AWAITING_APPROVAL`，同时确认输出目录为空。完整验收见
[M5 Definition of Done](m5-review.md)。

## M6.1：批准不是一句“可以”，而是一项一次性 capability

M5 把 run 暂停在 `AWAITING_APPROVAL`，但自然语言中的“同意”不能直接成为
执行权限。M6.1 将批准建模为 canonical `ApprovalRecord`，精确绑定：

```text
run_id + plan_hash + scope + expiry + nonce
                    ↓ canonical JSON + SHA-256
                 approval_id
```

其中 `scope` 第一版只有 `apply`。`expiry` 使用规范 UTC 时间，nonce 有固定的
安全格式；字段缺失、多余字段、非 canonical bytes 或任何绑定内容被改动都会
fail closed。这里的 hash 用来给不可变记录建立稳定 identity，而不是让模型
自行签发权限；记录只能由独立的审批入口创建。

### 为什么“检查未使用，再写 used=true”仍然不安全

两个 Executor 可能同时读到 `used=false`，然后都开始移动。M6.1 不做
read-modify-write，而是在批准 store 内创建唯一的 claim 文件：

```text
read + validate exact approval
→ reject wrong binding / expiry
→ open(claim, O_CREAT | O_EXCL | O_NOFOLLOW)
→ fsync claim and directory
→ only one caller may continue
```

`O_EXCL` 把竞争交给文件系统原子裁决：一个调用成功，其他并发调用和之后的
重放都得到 `already_claimed`。claim 必须在未来任何媒体移动之前持久化；若写入
过程中发生不确定错误，残留 claim 也不会被自动删除，因为“可能已经消费”必须
按已消费处理。

批准文件通过授权目录句柄、`O_NOFOLLOW` 和有界读取打开，并在读取前后核对
device、inode、size、mtime 和 ctime。错误 hash、run 或 scope 不会抢先消耗
正确批准，过期批准也不会建立 claim。

M6.1 仍然不会移动媒体。M6.2 将接入不依赖 LLM 的 Executor，并在 claim 后、
任何 rename 前对 plan、root、source identity、symlink、目标不存在和同一文件
系统做最终 preflight。

## M6.2：Executor 只接收 capability，不接收新的路径

M6.2 增加 content-addressed plan store。写入端只接受已经由 M5 compiler
构造且能复核 hash 的 `RenamePlan`；读取端只有 `load(plan_hash)`，文件名由
严格 hash 格式确定。Executor 的公开输入因此保持为：

```text
persisted plan_hash + approval_id
```

它不接收 `source`、`destination`、move 列表或自然语言。canonical bytes 在
no-follow、有界读取前后核对文件 identity，并再次计算 hash；随后严格解析出只含
执行所需字段的 manifest。plan bytes 篡改会在 claim 前失败，不会消耗正确批准。

### 为什么 transaction intent 位于 claim 之前

动态 preflight 读到的是某一瞬间的文件系统状态。如果两个 Executor 都先完成
检查、再竞争批准，它们会重复做昂贵工作，也会给后续副作用留下更复杂的状态。
最终 apply 在验证静态 plan 后先持久化 transaction/rollback intent，再原子
claim，最后执行动态检查：

```text
load + verify persisted plan
→ acquire transaction lease
→ persist rollback manifest
→ claim exact approval
→ reopen authorized roots with no-follow
→ verify every mapped and unmapped source identity
→ verify subtitle bounded-prefix digest
→ verify destination absent and parents are directories
→ verify each rename remains on one filesystem
```

source 使用目录句柄逐级打开；最终文件在 `lstat → O_NOFOLLOW open → fstat →
bounded read → fstat` 前后核对 device、inode、size、mtime 和 ctime。即使文件
在检查与 open 之间被换成 symlink，也只会得到结构化失败。目标路径同样逐级
拒绝 symlink 和非目录，任何已出现的目标都会成为 collision，绝不覆盖。

MVP 明确拒绝跨文件系统 move。这里没有退化为 copy + unlink，因为那会引入
部分复制、源删除和更复杂的崩溃恢复语义。

### 本步骤为什么仍不 rename

M6.2 只建立可离线验证的只读 advisory preflight，不消费 approval、不创建归档
目录、不移动媒体。独立 preflight 返回后，文件系统仍可能再次变化，所以它
不能单独成为长期有效的“检查凭证”。M6.3 在同一个 Executor apply 流程中重新
执行这些检查，并为部分失败提供幂等恢复。

## M6.3：副作用是一项事务，不是一个 move 工具

M6.3 没有给 Agent 增加 `apply` 或 `move_file`。唯一执行入口
`FilesystemExecutor.apply` 仍只接受持久化 `plan_hash + approval_id`，然后在
无模型、无网络的 effect plane 中完成：

```text
load exact plan
→ acquire transaction lease
→ persist rollback manifest
→ atomically claim approval
→ final preflight
→ rename one move with no-replace
→ fsync directories
→ append immutable move event
→ persist completed result
```

rollback manifest 在任何归档目录创建和 rename 之前写入，包含反向顺序的
destination、source 和 source identity。journal 不维护一个反复覆盖的
`status.json`；header、move、failure、rollback 和 terminal result 都是独立的
`O_EXCL` canonical 文件。相同事件重试时只有 exact bytes 相同才视为幂等，
内容冲突、symlink 或部分记录都会 fail closed。

apply 与 recover 必须持有同一个 transaction lease。approval claim 只表达
“该 capability 已被消费”，不能阻止 recovery 与仍在运行的 apply 并发。lease
由打开的文件描述符持有，进程退出时自动释放；竞争者得到结构化
`transaction_busy`，不会观察并改写半完成事务。

completed event 的持久化一旦开始，apply 不再自动 rollback，因为目录 fsync
报错不能证明 event 没有落盘。current move 的 post-rename identity 或即时恢复
不确定时也不会写 `rolled-back`；恢复必须观察 exact source/destination identity。
`completed` 与 `rolled-back` 同时存在属于冲突终态，直接 fail closed。

### 为什么“检查目标不存在，然后 os.rename”仍会覆盖

普通 `os.rename` 在 POSIX 上可以替换已存在目标。即使前一行刚检查过 absent，
另一进程也可能在两行之间创建目标。M6.3 使用：

```text
renameat2(..., RENAME_NOREPLACE)
```

不存在安全原语时不会降级成普通 rename，也不会使用 copy + unlink。目标在
最后时刻出现会得到 collision，外部目标保持原样。每次 forward/rollback rename
后都会 fsync 两端目录，并核对移动后仍是预期 inode；source 在移动前继续使用
完整 device/inode/size/mtime/ctime identity。

rename 本身可能合法改变 ctime，因此移动后的核对使用稳定的
device/inode/size/mtime；这不同于放松执行前校验。执行前仍要求完整 ctime，
rollback intent 也必须先持久化，才允许恢复逻辑把 ctime 改变解释为已完成的
反向 rename。

### 崩溃恢复为什么观察文件系统，而不只相信最后一条 event

进程可能在 rename 已成功、但 move event 尚未持久化时退出。恢复只依赖 event
会漏掉这个 move。`recover(plan_hash, approval_id)` 会重新验证 claimed approval
和 exact rollback manifest，然后对每个 move 判定：

```text
source expected + destination absent → 尚未移动或已安全回滚
source absent + destination expected → 已移动，需要反向 rename
其他任何组合                     → recovery_required
```

rollback 前先 append `rollback_started`，所以即使反向 rename 后、rollback
event 前再次崩溃，下一次恢复也能安全识别。若 source 被外部重新创建，恢复绝不
覆盖它；source 与 destination 同时存在时保留两者并要求人工处理。

### 暂停/恢复为什么仍然不属于 Agent

Plan Compiler 在发布 `ApprovalRequested` 前先保存 canonical plan。独立的
`ApprovalResumeService` 接收结构化 `ApprovalRecord`，而不是用户自然语言，并
驱动 `PlanApproved → ApplyStarted → MoveApplied/ApplyFailed →
RunCompleted/RollbackCompleted`。它调用不含模型的 Executor；Agent 的工具集合
仍然没有 `apply`、`move_file` 或任何路径参数。

完整验收见 [M6 Definition of Done](m6-review.md)。下一步 M7 将处理真实模型
配置、持久 checkpoint、脱敏 trace 与离线 eval；这些能力不会改变 Executor
已经建立的确定性权限边界。

## M7.1：Checkpoint 是领域事件日志，不是模型聊天记录

M1 的 `InMemoryEventStore` 已证明 typed events 可以确定性 replay，但进程退出后
状态会丢失。M7.1 先持久化 Reeloom 自己拥有的领域事实，不把 Agents SDK 的内部
对象、自然语言或 provider response 当作权威状态。

所有 runtime event 使用显式、版本化 schema 编解码。复杂事件也不会绕过领域
构造器：`MappingDraft` 重新执行结构校验；`PlanBuilt` 从 source identity 重建
candidate snapshot，经现有 mapping validator 和 plan compiler 重建
`RenamePlan`，最后要求 canonical bytes 与 `plan_hash` 完全一致。

filesystem event store 绑定一个授权 root 和固定 `run_id`。事件先经过 reducer，
再写入独立的 `event-NNNNNNNN.json`；记录先在匿名 inode 中完整写入并 fsync，
再用 no-replace link 原子发布，不会暴露半写 checkpoint 或覆盖旧记录。每次
append 也先重验既有日志。记录包含连续 sequence、前序 digest 和自身 digest。
重启加载时出现 gap、symlink、非 canonical event、run 不匹配、digest 断链或
非法状态转换都会停止恢复。

`EventStore` protocol 把领域 runtime 与存储方式解耦：

```text
typed event
→ reducer validates transition
→ immutable record commit
→ in-memory projection advances

restart
→ verify ordered immutable records
→ decode typed events
→ reducer replay
→ recover exact RunState
```

这里恢复的是确定性领域 checkpoint，不等于恢复模型的隐式思考。后续 M7.2 会用
固定 scripted transcript 描述可重放的模型输入/工具调用/输出，并建立 eval task
和指标；两者共享领域事件结果，但职责保持分离。

## M7.2：Eval 固定任务，不伪造第二套 Agent loop

`ScriptedTranscript` 把 model step 编码成严格版本化的 canonical artifact。tool
arguments 保存为固定 JSON 字符串，因此既能重放正常调用，也能重放 malformed
arguments 验证 guardrail；创建 transcript 后修改原始 Python dict 不会改变基线。

固定 dataset 将 scenario、prompt、trusted work type、transcript 和 terminal
expectation 绑定在一起。离线 runner 仍调用 Agents SDK `Runner.run` 和生产工具；
scripted model 只实现 SDK `Model` protocol。当前 mapping-correction task 覆盖：

```text
list candidates → identify TMDB series → inspect season/inventory/subtitle
→ submit out-of-bounds mapping → receive structured rejection
→ correct mapping → build exact plan → stop awaiting approval
```

因此以后替换真实 model 时，scenario、工具、validator 和评分器都不变，只替换
model factory。dataset hash 标识本次比较使用的精确任务版本。

## M7.3：Trace 是最小投影，不是 event dump

`PlanBuilt` 包含 source relative path、系列标题和完整 mapping；SDK session 还
包含 prompt 与 tool observation。它们都不能被直接复制到 trace。M7.3 从已验证
event replay 生成 allowlist record，只保留 phase/status、计数、枚举、plan hash
和已知工具/错误类别；未知模型字符串统一映射为 `unknown/other`。

mapping 成功必须精确匹配 dataset 标注的 TMDB ID、episode spans、字幕关联和
unmapped partition，而不是仅看是否生成 plan。scripted replay 额外核对固定工具
调用和 `kind + call_id + code` 拒绝标签；live model 不因采用更短的正确流程被
扣分。任务指标包括 validator 首次/最终通过、validator/tool rejection、工具
调用、input/output/total tokens、延迟、人工澄清、未映射保留率和标注安全拒绝
误报/漏报。成本不硬编码会过期的价格；调用方显式提供 input/output 每百万
token USD rate，报告输出稳定的 micro-USD 估算。

## M7.4：真实模型只替换 Model，不改变权限边界

真实 adapter 显式构造 official OpenAI Responses client，不使用 SDK 全局 provider
或可变 base URL。library 接受显式 API key、model、timeout/retry、可选
organization/project、reasoning effort 和 verbosity；它不读取任何配置文件。
Agent 侧覆盖任何调用方设置，强制：

```text
store = false
parallel_tool_calls = false
max_tokens <= run budget
SDK tracing disabled + sensitive tracing disabled
```

这意味着真实模型得到的仍只有八个受控 Reeloom function tools，不会获得 hosted
web、MCP、shell、文件或 apply capability。SDK conversation 使用独立
`FilesystemAgentSession`：`add/pop/clear` 都原子发布新的 no-follow immutable
record，从不删除旧会话文件；它可恢复模型历史，但不能改变领域 `RunState`。

`scripts/openai_live_smoke.py` 必须显式传 `--live`。key/base URL/model/reasoning
可以从进程环境或仓库根固定 `.env` 的受限 allowlist 读取，CLI model/reasoning
优先。它在与离线基线相同的 dataset/scenario 上运行真实 model，输出 dataset
hash、model settings 和脱敏指标；pytest 永远不会调用它的网络路径。模型选择与
工具调用遵循 OpenAI 当前的
[model guidance](https://developers.openai.com/api/docs/guides/latest-model)
和 [Responses tools guidance](https://developers.openai.com/api/docs/guides/tools)。
